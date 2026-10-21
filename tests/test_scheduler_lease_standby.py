"""A scheduler that could not take the lease keeps standing by for it (#170).

The gap: a backend restarted within the lease TTL after a crash found the dead owner's row still
in date, `start()` returned False, and nothing ever retried. Consolidation stayed stopped in that
process until an operator resumed it.

Safety property: a standby process acquires the lease only through the store's own admission rule
(free, expired, or provably dead local owner), and never after `stop()` has withdrawn the request.
Each test below builds one interleaving that would break it.
"""

from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import pytest

from menhir.services import MaintenanceScheduler
from menhir.services.scheduler_lease import SchedulerLeaseStore
from tests._temp_cleanup import remove_test_dir

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

LEASE = "maintenance_scheduler"


class _Ingest:
    async def recover_stale_enrichment_leases(self, limit: int = 100):
        return 0, 0


class _Graph:
    pass


class _ScriptedStore:
    """The real store's admission rule over an in-memory row the test controls."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.acquire_errors = 0

    def try_acquire(self, *, lease_name, owner_id, owner_pid, lease_duration_s) -> bool:
        if self.acquire_errors:
            self.acquire_errors -= 1
            raise RuntimeError("database is locked")
        row = self.rows.get(lease_name)
        if row is None or row["owner_id"] == owner_id:
            self.rows[lease_name] = {"owner_id": owner_id, "owner_pid": owner_pid}
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
        row = self.rows.get(lease_name)
        if row is not None and row["owner_id"] == owner_id:
            del self.rows[lease_name]

    def fetch(self, *, lease_name):
        return self.rows.get(lease_name)


def _scheduler(store, *, lease_duration_s=0.3, heartbeat_s=0.05) -> MaintenanceScheduler:
    sched = MaintenanceScheduler(
        ingest_service=_Ingest(), graph_adapter=_Graph(), lease_store=store,
        structure_watcher_enabled=False, experience_counter_enabled=False,
        tick_interval_s=0.01, lease_duration_s=lease_duration_s, lease_heartbeat_s=heartbeat_s)
    sched._jobs.clear()  # lifecycle only; no job touches a graph
    return sched


async def _until(predicate, timeout_s=5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_unclean_restart_acquires_once_the_dead_owners_lease_expires(tmp_path):
    """The reported gap, on the real SQLite store: a crashed owner's row is still in date."""
    run_dir = tmp_path / f"standby-{uuid4()}"
    run_dir.mkdir()
    try:
        store = SchedulerLeaseStore(db_path=run_dir / "lease.db")
        # The crashed process: it acquired, then never renewed or released.
        assert store.try_acquire(lease_name=LEASE, owner_id="crashed:1:x", owner_pid=1,
                                 lease_duration_s=1.0)
        sched = _scheduler(store, lease_duration_s=30.0, heartbeat_s=0.1)
        assert await sched.start() is False
        assert sched.status_snapshot()["lease"]["standby"] is True
        assert sched.is_running() is False
        assert await _until(sched.is_running), "standby never took the expired lease"
        assert store.fetch(lease_name=LEASE)["owner_id"] == sched._owner_id
        assert sched.status_snapshot()["lease"]["standby"] is False
        await sched.stop()
        assert store.fetch(lease_name=LEASE) is None
    finally:
        remove_test_dir(run_dir)


async def test_standby_never_displaces_a_live_owner():
    store = _ScriptedStore()
    owner, waiting = _scheduler(store), _scheduler(store)
    assert await owner.start() is True
    assert await waiting.start() is False
    # Many standby intervals and several lease durations: the owner renews, so its row stays.
    await asyncio.sleep(1.0)
    assert owner.is_running() and not waiting.is_running()
    assert store.rows[LEASE]["owner_id"] == owner._owner_id
    await owner.stop()  # releases the row
    assert await _until(waiting.is_running), "standby did not take the released lease"
    assert store.rows[LEASE]["owner_id"] == waiting._owner_id
    await waiting.stop()


async def test_stop_queued_before_the_standby_wakes_withdraws_it():
    """stop() is waiting on the state lock when the lease frees and the standby wakes behind it.
    Without the withdrawal, the standby acquires right after stop() returns: a scheduler running
    that its operator just stopped."""
    store = _ScriptedStore()
    store.rows[LEASE] = {"owner_id": "other", "owner_pid": 7}
    sched = _scheduler(store)
    assert await sched.start() is False
    async with sched._state_lock:
        stopping = asyncio.create_task(sched.stop())
        await asyncio.sleep(0)            # stop() queues on the lock first
        del store.rows[LEASE]             # the other owner goes away
        await asyncio.sleep(0.2)          # the standby wakes and queues behind stop()
    await asyncio.wait_for(stopping, timeout=5)
    await asyncio.sleep(0.3)              # several standby intervals
    assert sched.is_running() is False
    assert LEASE not in store.rows
    assert sched.status_snapshot()["lease"]["standby"] is False


async def test_stop_after_the_standby_acquired_leaves_nothing_running():
    """The opposite order: the standby holds the lock first and acquires; stop() then stops it."""
    store = _ScriptedStore()
    store.rows[LEASE] = {"owner_id": "other", "owner_pid": 7}
    sched = _scheduler(store)
    assert await sched.start() is False
    del store.rows[LEASE]
    assert await _until(sched.is_running)
    await sched.stop()
    assert sched.is_running() is False and LEASE not in store.rows


async def test_pause_then_resume_rearms_the_standby():
    store = _ScriptedStore()
    store.rows[LEASE] = {"owner_id": "other", "owner_pid": 7}
    sched = _scheduler(store)
    assert await sched.start() is False
    await sched.stop()                    # scheduler_pause
    assert sched.status_snapshot()["lease"]["standby"] is False
    assert await sched.start() is False   # scheduler_resume, lease still held
    assert sched.status_snapshot()["lease"]["standby"] is True
    del store.rows[LEASE]
    assert await _until(sched.is_running)
    await sched.stop()


async def test_store_errors_do_not_end_the_standby():
    store = _ScriptedStore()
    store.rows[LEASE] = {"owner_id": "other", "owner_pid": 7}
    sched = _scheduler(store)
    assert await sched.start() is False
    store.acquire_errors = 3
    del store.rows[LEASE]
    assert await _until(sched.is_running), "a transient store error killed the standby"
    assert store.acquire_errors == 0
    await sched.stop()


async def test_a_displaced_owner_does_not_stand_by():
    """After losing a lease it held, a job thread from that ownership may still be running, so
    re-acquiring stays an operator action. The displaced process must not take the lease back
    on its own when the new owner leaves."""
    store = _ScriptedStore()
    first, second = _scheduler(store), _scheduler(store)
    assert await first.start() is True
    assert await second.force_takeover(reason="test") is True
    assert await _until(lambda: not first.is_running())
    assert await first.start() is False   # e.g. an operator resume while second still owns it
    assert first.status_snapshot()["lease"]["standby"] is False
    await second.stop()
    await asyncio.sleep(0.5)
    assert first.is_running() is False and LEASE not in store.rows
    await first.stop()
