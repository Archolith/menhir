"""CF-241: a forced takeover displaced a live owner with nothing to stop it mutating.

`force_acquire` overwrites the lease row unconditionally. That is what an operator escape hatch is
for and it stays. What it cannot do is tell the displaced process anything: that process gates its
own mutations on `_lease_is_provable()`, a MONOTONIC deadline stamped at its last successful
renewal, so it keeps running jobs until its next heartbeat renewal fails. Between the takeover and
that heartbeat, two loops run maintenance against the same graph.

THE DURABLE FACT THAT BOUNDS THE DISPLACED OWNER is the row that was replaced: `lease_expires_at`
is the latest moment it could still consider itself provable, because its own deadline was stamped
from a renewal no later than that. Waiting past it is a proof rather than an estimate -- which is
why the gate is built from the replaced row and not from a guess about how fast the peer reacts.
Nothing the taking process does can make a stalled or suspended peer notice sooner.

The lease is still claimed immediately, so no third process can take it during the wait, and the
run loop still starts. Only job EXECUTION is deferred.
"""

from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.unit


class _Store:
    """Durable lease facts, with force_acquire returning the row it replaced."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}

    def seed_live_owner(self, lease_name: str, *, owner_id: str, ttl_s: float) -> None:
        self.rows[lease_name] = {
            "owner_id": owner_id,
            "owner_pid": 999,
            "lease_expires_at": time.time() + ttl_s,
        }

    def try_acquire(self, *, lease_name, owner_id, owner_pid, lease_duration_s) -> bool:
        row = self.rows.get(lease_name)
        if row is None or row["owner_id"] == owner_id:
            self.rows[lease_name] = {
                "owner_id": owner_id,
                "owner_pid": owner_pid,
                "lease_expires_at": time.time() + lease_duration_s,
            }
            return True
        return False

    def force_acquire(self, *, lease_name, owner_id, owner_pid, lease_duration_s):
        previous = self.rows.get(lease_name)
        self.rows[lease_name] = {
            "owner_id": owner_id,
            "owner_pid": owner_pid,
            "lease_expires_at": time.time() + lease_duration_s,
        }
        return previous

    def renew(self, *, lease_name, owner_id, owner_pid, lease_duration_s) -> bool:
        row = self.rows.get(lease_name)
        return row is not None and row["owner_id"] == owner_id

    def release(self, *, lease_name, owner_id) -> None:
        row = self.rows.get(lease_name)
        if row is not None and row["owner_id"] == owner_id:
            del self.rows[lease_name]

    def fetch(self, *, lease_name):
        return self.rows.get(lease_name)


def _scheduler(store: _Store, *, lease_duration_s: float = 60.0):
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    s = MaintenanceScheduler.__new__(MaintenanceScheduler)
    for name, value in {
        "lease_store": store,
        "lease_name": "maintenance",
        "lease_duration_s": lease_duration_s,
        "tick_interval_s": 0.01,
        "_owner_id": "owner-B",
        "_owner_pid": 4242,
        "_jobs": {},
        "_task": None,
        "_state_lock": asyncio.Lock(),
        "_stop_event": asyncio.Event(),
        "_lease_lost_event": asyncio.Event(),
        "_lease_acquired": False,
        "_lease_lost": False,
        "_lease_valid_until": 0.0,
        "_lease_blocked_reason": None,
        "_last_force_takeover_at": None,
        "_last_force_takeover_reason": None,
        "_last_force_takeover_from": None,
        "_jobs_blocked_until": 0.0,
    }.items():
        object.__setattr__(s, name, value)
    return s


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_forced_takeover_defers_jobs_until_the_displaced_lease_expires() -> None:
    """THE FINDING. Owner A holds a lease with 40s left; B force-takes it. B must not run a job
    while A could still believe it owns the lease."""
    store = _Store()
    store.seed_live_owner("maintenance", owner_id="owner-A", ttl_s=40.0)
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert sched._jobs_are_deferred() is True
        remaining = sched._jobs_blocked_until - time.monotonic()
        assert 35.0 < remaining <= 40.5, remaining
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_the_lease_is_still_taken_immediately() -> None:
    """The capability is NOT weakened. The row changes hands at once, so no third process can
    acquire during the wait -- only this owner's job execution is deferred."""
    store = _Store()
    store.seed_live_owner("maintenance", owner_id="owner-A", ttl_s=40.0)
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert store.rows["maintenance"]["owner_id"] == "owner-B"
        assert sched.is_running() is True
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_the_deferral_is_capped_at_one_lease_duration() -> None:
    """A corrupt or absurd expiry must not park the scheduler forever. The longest any owner's
    provable window can be is one lease duration."""
    store = _Store()
    store.rows["maintenance"] = {
        "owner_id": "owner-A",
        "owner_pid": 1,
        "lease_expires_at": time.time() + 86_400.0,
    }
    sched = _scheduler(store, lease_duration_s=60.0)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        # Both halves: the gate must be ARMED (a bare `<= 60.5` is satisfied by 0.0, so the
        # first version of this assertion passed with the gate removed entirely), and capped.
        assert sched._jobs_are_deferred() is True
        assert 55.0 < sched._jobs_blocked_until - time.monotonic() <= 60.5
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_an_unreadable_expiry_waits_a_full_lease_duration() -> None:
    """Fail closed: an expiry we cannot read is not evidence the peer is finished."""
    store = _Store()
    store.rows["maintenance"] = {"owner_id": "owner-A", "owner_pid": 1, "lease_expires_at": "junk"}
    sched = _scheduler(store, lease_duration_s=60.0)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert 55.0 < sched._jobs_blocked_until - time.monotonic() <= 60.5
    finally:
        await sched.stop()


# ---------------------------------------------------------------------------
# positive controls -- the gate must not fire when there is nobody to outlive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_expired_previous_owner_is_not_waited_for() -> None:
    """POSITIVE CONTROL, the one that matters most: a gate that always deferred would satisfy
    every test above and park maintenance after any takeover."""
    store = _Store()
    store.rows["maintenance"] = {
        "owner_id": "owner-A",
        "owner_pid": 1,
        "lease_expires_at": time.time() - 5.0,
    }
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert sched._jobs_are_deferred() is False
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_taking_an_unheld_lease_defers_nothing() -> None:
    """POSITIVE CONTROL: force-taking a lease nobody holds waits for nobody."""
    store = _Store()
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert sched._jobs_are_deferred() is False
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_retaking_our_own_lease_defers_nothing() -> None:
    """POSITIVE CONTROL: the same process re-taking its own lease has no peer to outlive.
    Without this check a restart-in-place would park its own maintenance."""
    store = _Store()
    store.seed_live_owner("maintenance", owner_id="owner-B", ttl_s=40.0)
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert sched._jobs_are_deferred() is False
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_an_ordinary_start_never_defers() -> None:
    """POSITIVE CONTROL: the gate belongs to forced takeover alone. A normal start already
    refuses when someone else holds the lease, so it has nobody to outlive."""
    store = _Store()
    sched = _scheduler(store)

    assert await sched.start()
    try:
        assert sched._jobs_are_deferred() is False
        assert sched._jobs_blocked_until == 0.0
    finally:
        await sched.stop()


async def _run_jobs_once(sched) -> list[str]:
    """Drive `_run_due_jobs` with ONE real due job registered, returning what executed.

    An empty `_jobs` dict makes this test vacuous -- verified by mutation: with the gate removed
    the first version still asserted `ran == []`, because there was nothing to run either way.
    """
    from menhir.services.maintenance_scheduler import _JobState

    ran: list[str] = []

    async def _fake_run_job(job, operation, coro_factory):
        ran.append(operation)

    object.__setattr__(sched, "_jobs", {"recover_stale_leases": _JobState(interval_s=0.0)})
    object.__setattr__(sched, "_run_job", _fake_run_job)
    object.__setattr__(sched, "_make_recover_stale_leases", lambda: None)
    await sched._run_due_jobs()
    return ran


@pytest.mark.asyncio
async def test_deferred_jobs_do_not_run() -> None:
    """The behavioural end of it: while deferred, a due job does not execute."""
    store = _Store()
    store.seed_live_owner("maintenance", owner_id="owner-A", ttl_s=40.0)
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        assert await _run_jobs_once(sched) == []
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_the_same_job_runs_once_the_deferral_lapses() -> None:
    """POSITIVE CONTROL for the test instrument AND the fix: the identical setup must execute the
    job when the gate is not armed. Without this, `ran == []` above proves nothing."""
    store = _Store()
    store.seed_live_owner("maintenance", owner_id="owner-A", ttl_s=40.0)
    sched = _scheduler(store)

    assert await sched.start(force_takeover=True, takeover_reason="test")
    try:
        object.__setattr__(sched, "_jobs_blocked_until", 0.0)  # the deferral has lapsed
        assert await _run_jobs_once(sched) == ["scheduler_recover_stale_leases"]
    finally:
        await sched.stop()
