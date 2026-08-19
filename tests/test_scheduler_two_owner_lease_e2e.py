"""CF-100: two real schedulers, one real lease store, mutation in flight.

**The gap this closes.** The existing CF-100 tests drive ONE scheduler and simulate the loss --
they patch a renewal to return False, or call `_mark_lease_lost` directly. That proves the guard
reacts to a signal. It does not prove the property the guard exists for, which is about TWO
processes: *no two owners mutate concurrently*. A single-scheduler test cannot observe that,
because there is no second owner in it.

So this stands up two independent `MaintenanceScheduler` instances over ONE SQLite lease store on
disk -- the real `SchedulerLeaseStore`, the real acquire/renew/force-acquire SQL -- and asks what
happens to work already in flight when ownership moves.

**Both loss shapes, and the second is the one that matters.** A force takeover is loud: the
displaced owner's next renewal returns False and it finds out. Lease EXPIRY is silent -- nothing
fails, no call returns an error, the deadline simply passes while the owner is still inside a
job. CF-100's filed defect was that the only guard ran BETWEEN jobs, so a loss during `await
coro` stopped the next job and let the current one run to completion against a graph a new owner
had already begun maintaining.

**The guarantee is a BOUND, not an eviction, and the tests say so.** Cancellation is cooperative:
it lands at the job's next await point, so a job blocked inside `asyncio.to_thread` keeps that one
worker-thread call running -- nothing can interrupt a thread from outside. What is provable is
that no FURTHER step of the job begins. `test_..._bounds_rather_than_evicts` pins exactly that,
because a test asserting instant eviction would be asserting something the design does not claim
and would fail for the wrong reason.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytestmark = [pytest.mark.timing]


class _Ingest:
    def get_queue_depth(self) -> int:
        return 0


class _Graph:
    pass


@pytest.fixture
def lease_db(tmp_path):
    """One lease file, shared by both schedulers -- the point of the whole exercise."""
    from menhir.services.scheduler_lease import SchedulerLeaseStore

    store = SchedulerLeaseStore(db_path=tmp_path / "scheduler-lease.db")
    store._ensure_ready()
    return tmp_path / "scheduler-lease.db"


def _scheduler(lease_db, *, name: str, **overrides: Any):
    from menhir.services.maintenance_scheduler import MaintenanceScheduler
    from menhir.services.scheduler_lease import SchedulerLeaseStore

    kwargs: dict[str, Any] = {
        "ingest_service": _Ingest(),
        "graph_adapter": _Graph(),
        "lease_store": SchedulerLeaseStore(db_path=lease_db),
        "lease_name": "maintenance",
    }
    kwargs.update(overrides)
    sched = MaintenanceScheduler(**kwargs)
    sched._owner_id = name  # distinguishable in the lease row and in assertions
    return sched


# ---------------------------------------------------------------------------
# The mutual-exclusion property, over the real store
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_one_of_two_schedulers_acquires_the_shared_lease(lease_db) -> None:
    """The baseline the rest depends on. If both could acquire, every later assertion about
    displacement would be meaningless."""
    a = _scheduler(lease_db, name="A")
    b = _scheduler(lease_db, name="B")

    assert a.lease_store.try_acquire(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=60
    ) is True
    assert b.lease_store.try_acquire(
        lease_name="maintenance", owner_id="B", owner_pid=2222, lease_duration_s=60
    ) is False, "two schedulers both hold the lease"

    row = b.lease_store.fetch(lease_name="maintenance")
    assert row is not None and str(row["owner_id"]) == "A"


@pytest.mark.asyncio
async def test_a_force_takeover_displaces_the_recorded_owner(lease_db) -> None:
    """Takeover is the LOUD loss: it rewrites the row, so the displaced owner's next renewal
    returns False and it has a definite answer."""
    a = _scheduler(lease_db, name="A")
    b = _scheduler(lease_db, name="B")

    a.lease_store.try_acquire(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=60
    )
    previous = b.lease_store.force_acquire(
        lease_name="maintenance", owner_id="B", owner_pid=2222, lease_duration_s=60
    )

    assert previous is not None and str(previous["owner_id"]) == "A"
    assert str(b.lease_store.fetch(lease_name="maintenance")["owner_id"]) == "B"
    # A's renewal now fails against the shared store: that is how it learns.
    assert a.lease_store.renew(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=60
    ) is False


# ---------------------------------------------------------------------------
# Displacement DURING an active mutation -- the filed defect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_job_in_flight_is_abandoned_when_a_peer_takes_the_lease(lease_db) -> None:
    """The defect, with a real second owner rather than a simulated signal.

    A runs a multi-step job. B force-takes the lease from the shared store mid-flight. A must
    stop at its next await point and raise, rather than finishing the job against a graph B has
    already started maintaining.
    """
    from menhir.services.maintenance_scheduler import _LeaseLostDuringJob

    a = _scheduler(lease_db, name="A", lease_duration_s=60.0)
    b = _scheduler(lease_db, name="B")
    a.lease_store.try_acquire(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=60
    )
    a._lease_acquired = True
    a._stamp_lease_deadline(asyncio.get_running_loop().time())

    steps: list[int] = []

    async def long_job() -> dict[str, object]:
        for i in range(20):
            steps.append(i)
            await asyncio.sleep(0.02)
        return {"completed": True}

    async def takeover_after_a_moment() -> None:
        await asyncio.sleep(0.06)
        b.lease_store.force_acquire(
            lease_name="maintenance", owner_id="B", owner_pid=2222, lease_duration_s=60
        )
        # A learns through its own heartbeat path; drive one renewal to model that tick.
        if not a.lease_store.renew(
            lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=60
        ):
            a._mark_lease_lost()

    with pytest.raises(_LeaseLostDuringJob):
        await asyncio.gather(
            a._await_job_under_lease(long_job(), "long_job"),
            takeover_after_a_moment(),
        )

    assert len(steps) < 20, "the displaced owner ran the job to completion"
    before = len(steps)
    await asyncio.sleep(0.1)
    assert len(steps) == before, "the abandoned job kept running after the raise"


@pytest.mark.asyncio
async def test_a_SILENTLY_EXPIRED_lease_also_abandons_the_job(lease_db) -> None:
    """The loss shape with no signal at all, and the reason CF-100's fix could not be built on
    renewal failures alone.

    Nothing returns an error here. No peer appears. The deadline simply passes while the owner is
    inside a job -- and before the fix the owner had no way to notice, because the only evidence
    it consulted was a renewal that came back False.
    """
    from menhir.services.maintenance_scheduler import _LeaseLostDuringJob

    a = _scheduler(lease_db, name="A", lease_duration_s=0.15)
    a.lease_store.try_acquire(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=0.15
    )
    a._lease_acquired = True
    a._stamp_lease_deadline(asyncio.get_running_loop().time())

    steps: list[int] = []

    async def long_job() -> dict[str, object]:
        for i in range(40):
            steps.append(i)
            await asyncio.sleep(0.02)
        return {"completed": True}

    with pytest.raises(_LeaseLostDuringJob):
        await a._await_job_under_lease(long_job(), "long_job")

    assert len(steps) < 40, "an expired-lease owner ran the job to completion"


@pytest.mark.asyncio
async def test_the_guard_bounds_rather_than_evicts(lease_db) -> None:
    """The stated limit, pinned so nobody later reads the guard as stronger than it is.

    Cancellation is cooperative. A job blocked inside `asyncio.to_thread` keeps that ONE
    worker-thread call running to completion, because a thread cannot be interrupted from
    outside. What is guaranteed is that no FURTHER step begins -- a bound, not an eviction.

    Asserting instant eviction would be asserting something the design does not claim, and would
    fail for a reason unrelated to any defect.
    """
    from menhir.services.maintenance_scheduler import _LeaseLostDuringJob

    a = _scheduler(lease_db, name="A", lease_duration_s=0.12)
    a.lease_store.try_acquire(
        lease_name="maintenance", owner_id="A", owner_pid=1111, lease_duration_s=0.12
    )
    a._lease_acquired = True
    a._stamp_lease_deadline(asyncio.get_running_loop().time())

    dispatched: list[str] = []

    def blocking_call(tag: str) -> None:
        dispatched.append(f"start:{tag}")
        import time as _t

        _t.sleep(0.25)  # outlives the lease, and cannot be cancelled from outside
        dispatched.append(f"end:{tag}")

    async def job() -> dict[str, object]:
        await asyncio.to_thread(blocking_call, "one")
        await asyncio.to_thread(blocking_call, "two")
        return {"completed": True}

    with pytest.raises(_LeaseLostDuringJob):
        await a._await_job_under_lease(job(), "blocking_job")

    await asyncio.sleep(0.4)  # let any already-dispatched thread finish
    assert "start:one" in dispatched
    assert "start:two" not in dispatched, (
        "a second blocking call was dispatched after the lease was lost -- the bound failed"
    )


# ---------------------------------------------------------------------------
# The supervision predicate itself
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unsupervised_scheduler_runs_its_job_normally(lease_db) -> None:
    """A scheduler that never acquired is not a DISPLACED owner -- there is no second owner it
    could be racing -- so the deadline has nothing to protect and the job must run.

    This is the direct-invocation shape, and getting it wrong is not theoretical: four tests
    failed on it during the original CF-100 work, because a scheduler that never held the lease
    was being supervised and its jobs were abandoned instantly.
    """
    a = _scheduler(lease_db, name="A")
    assert a._lease_supervision_active() is False

    async def job() -> dict[str, object]:
        await asyncio.sleep(0.01)
        return {"completed": True}

    assert await a._await_job_under_lease(job(), "job") == {"completed": True}


@pytest.mark.asyncio
async def test_a_lease_that_was_lost_still_reads_as_supervised(lease_db) -> None:
    """`_mark_lease_lost` zeroes the deadline, so without the `_lease_lost` term a scheduler that
    had JUST lost the lease would be indistinguishable from one that never held it -- and would
    therefore stop being supervised at the exact moment supervision matters most."""
    a = _scheduler(lease_db, name="A", lease_duration_s=60.0)
    a._lease_acquired = True
    a._stamp_lease_deadline(asyncio.get_running_loop().time())
    assert a._lease_supervision_active() is True

    a._mark_lease_lost()
    assert a._lease_valid_until == 0.0
    assert a._lease_supervision_active() is True, (
        "a scheduler that just lost its lease read as one that never held it"
    )
