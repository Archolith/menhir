"""CF-240: `stop()` released the lease after a concurrent `start()` had already re-taken it.

The old sequence held `_state_lock` only long enough to set `_stop_event` and clear `_task`, then
awaited the task and released the lease OUTSIDE the lock. A `start()` arriving in that window:

  1. saw `is_running()` False, because `_task` was already None;
  2. called `try_acquire`, which SUCCEEDS -- it admits `existing_owner_id == owner_id`, and this is
     the same instance, so the same `_owner_id`. The guard is not a guard here;
  3. cleared `_stop_event` and created a second `_run_loop` task.

Then `stop()` resumed and called `release()`, deleting the row the new loop believed it held.

The damaging outcome is not the duplicate loop, it is that release: a live run loop executing
maintenance jobs with `_lease_acquired = True` and NO lease row behind it, so any other process's
`try_acquire` succeeds immediately and two schedulers mutate the graph at once. That matters
beyond this class -- `saga_reconcile_gate` runs CF-20's startup recovery on this same store, whose
Hazard 2 is "two instances both replay operation X".

WHY A CODE-SHAPE TEST WOULD NOT DO. Asserting that `stop()` holds a lock, or that `release` is
called once, passes against implementations that still interleave. These tests construct the
interleaving: `start()` is invoked from inside the stopping task's await window, which is the only
moment the bug is reachable.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

pytestmark = pytest.mark.unit


class _FakeLeaseStore:
    """Records the durable facts the real SQLite store keeps, with the same admission rule."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.releases: list[str] = []
        self.acquires: list[str] = []

    def try_acquire(self, *, lease_name, owner_id, owner_pid, lease_duration_s) -> bool:
        row = self.rows.get(lease_name)
        # The real store's rule: a row owned by this same owner_id is re-acquirable.
        if row is None or row["owner_id"] == owner_id:
            self.rows[lease_name] = {"owner_id": owner_id, "owner_pid": owner_pid}
            self.acquires.append(owner_id)
            return True
        return False

    def force_acquire(self, *, lease_name, owner_id, owner_pid, lease_duration_s):
        previous = self.rows.get(lease_name)
        self.rows[lease_name] = {"owner_id": owner_id, "owner_pid": owner_pid}
        return previous

    def renew(self, *, lease_name, owner_id, owner_pid, lease_duration_s) -> bool:
        row = self.rows.get(lease_name)
        return row is not None and row["owner_id"] == owner_id

    def release(self, *, lease_name, owner_id) -> None:
        self.releases.append(owner_id)
        row = self.rows.get(lease_name)
        if row is not None and row["owner_id"] == owner_id:
            del self.rows[lease_name]

    def fetch(self, *, lease_name):
        return self.rows.get(lease_name)


def _scheduler(store: _FakeLeaseStore):
    from menhir.services.maintenance_scheduler import MaintenanceScheduler

    sched = MaintenanceScheduler.__new__(MaintenanceScheduler)
    # Only the lifecycle attributes are exercised; no jobs are registered, so the loop ticks
    # without touching a graph.
    object.__setattr__(sched, "lease_store", store)
    object.__setattr__(sched, "lease_name", "maintenance")
    object.__setattr__(sched, "lease_duration_s", 60.0)
    object.__setattr__(sched, "tick_interval_s", 0.01)
    object.__setattr__(sched, "_owner_id", "owner-A")
    object.__setattr__(sched, "_owner_pid", 4242)
    object.__setattr__(sched, "_jobs", {})
    object.__setattr__(sched, "_task", None)
    object.__setattr__(sched, "_state_lock", asyncio.Lock())
    object.__setattr__(sched, "_stop_event", asyncio.Event())
    object.__setattr__(sched, "_lease_lost_event", asyncio.Event())
    object.__setattr__(sched, "_lease_acquired", False)
    object.__setattr__(sched, "_lease_lost", False)
    object.__setattr__(sched, "_lease_valid_until", 0.0)
    object.__setattr__(sched, "_lease_blocked_reason", None)
    object.__setattr__(sched, "_last_force_takeover_at", None)
    object.__setattr__(sched, "_last_force_takeover_reason", None)
    object.__setattr__(sched, "_last_force_takeover_from", None)
    return sched


async def _park_inside_the_stop_window(sched):
    """Replace the live loop task with one WE finish, so `stop()`'s await is held open.

    Yielding with `asyncio.sleep(0)` is not enough to construct this race, and a test that relies
    on it is vacuous -- verified by mutation: restoring the unguarded tail left every such
    assertion passing, because `stop()` ran to completion before `start()` was ever scheduled.
    The window exists only while `stop()` is suspended on `await task`, so the test has to own
    when that await resolves.
    """
    gate = asyncio.Event()
    real = sched._task
    real.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await real
    sched._task = asyncio.create_task(gate.wait())
    return gate


async def _settle(times: int = 8) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_start_during_stop_does_not_end_with_a_running_loop_and_no_lease() -> None:
    """THE COUNTEREXAMPLE. `start()` is driven while `stop()` sits in its await window, which is
    the only moment the defect is reachable. The asserted invariant is the one that matters: a
    running loop must always have a lease row behind it."""
    store = _FakeLeaseStore()
    sched = _scheduler(store)

    assert await sched.start()
    gate = await _park_inside_the_stop_window(sched)

    stopping = asyncio.create_task(sched.stop())
    await _settle()                      # stop() clears _task and suspends on the gate task
    restarting = asyncio.create_task(sched.start())
    await _settle()                      # unfixed: start() re-acquires and builds a second loop
    gate.set()                           # let stop() resume into its release
    await asyncio.wait_for(asyncio.gather(stopping, restarting), timeout=5)

    running = sched.is_running()
    has_lease = store.rows.get("maintenance") is not None
    assert running == has_lease, (
        f"invariant violated: running={running} but lease row present={has_lease}; "
        "a loop executing maintenance with no lease lets a second process acquire and run"
    )

    await sched.stop()


@pytest.mark.asyncio
async def test_the_release_cannot_land_after_a_later_acquire() -> None:
    """The mechanism, stated directly: `owner_id` cannot tell one acquire from the next, so a
    release that outlives its own acquire deletes a row it did not create."""
    store = _FakeLeaseStore()
    sched = _scheduler(store)

    assert await sched.start()
    gate = await _park_inside_the_stop_window(sched)

    stopping = asyncio.create_task(sched.stop())
    await _settle()
    restarting = asyncio.create_task(sched.start())
    await _settle()
    gate.set()
    await asyncio.wait_for(asyncio.gather(stopping, restarting), timeout=5)

    expected_open = 1 if store.rows.get("maintenance") is not None else 0
    assert len(store.acquires) - len(store.releases) == expected_open, (
        f"acquires={store.acquires} releases={store.releases} "
        f"row={store.rows.get('maintenance')}"
    )

    await sched.stop()


@pytest.mark.asyncio
async def test_a_plain_stop_still_releases_the_lease() -> None:
    """POSITIVE CONTROL. Serializing the sequence must not turn `stop()` into a no-op -- a fix
    that simply never released would satisfy the invariant test above."""
    store = _FakeLeaseStore()
    sched = _scheduler(store)

    assert await sched.start()
    await sched.stop()

    assert not sched.is_running()
    assert store.rows.get("maintenance") is None
    assert store.releases == ["owner-A"]


@pytest.mark.asyncio
async def test_a_plain_start_stop_start_cycle_still_works() -> None:
    """POSITIVE CONTROL: ordinary sequential use is unaffected."""
    store = _FakeLeaseStore()
    sched = _scheduler(store)

    assert await sched.start()
    await sched.stop()
    assert await sched.start()

    assert sched.is_running()
    assert store.rows.get("maintenance") is not None

    await sched.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    """POSITIVE CONTROL: a second stop must not raise or release someone else's lease."""
    store = _FakeLeaseStore()
    sched = _scheduler(store)

    assert await sched.start()
    await sched.stop()
    await sched.stop()

    assert store.rows.get("maintenance") is None
