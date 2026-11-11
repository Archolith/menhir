"""Single-process enforcement for enrichment workers (erasure rebuild P4, plan 2.6). Offline."""

from __future__ import annotations

import asyncio
import functools
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.services import IngestService
from menhir.services import ingest_queue
from menhir.services.scheduler_lease import (
    SchedulerLeaseStore,
    clear_local_scheduler,
    enrichment_allowed,
    register_local_scheduler,
)

LEASE = "maintenance_scheduler"
ME = "owner-me"
OTHER = "owner-other"


@pytest.fixture
def store(tmp_path) -> SchedulerLeaseStore:
    clear_local_scheduler(LEASE, ME)
    yield SchedulerLeaseStore(db_path=tmp_path / "leases.sqlite3")
    clear_local_scheduler(LEASE, ME)


def _take(store: SchedulerLeaseStore, owner: str, duration_s: float = 300.0) -> None:
    assert store.try_acquire(
        lease_name=LEASE,
        owner_id=owner,
        owner_pid=os.getpid(),
        lease_duration_s=duration_s,
    )


def test_own_provable_lease_is_allowed(store) -> None:
    _take(store, ME)
    register_local_scheduler(LEASE, ME, lambda: True)
    assert enrichment_allowed(strict=True, store=store) is True
    assert enrichment_allowed(strict=False, store=store) is True


def test_own_unprovable_lease_is_refused(store) -> None:
    _take(store, ME)
    register_local_scheduler(LEASE, ME, lambda: False)
    assert enrichment_allowed(strict=True, store=store) is False
    assert enrichment_allowed(strict=False, store=store) is False


def test_live_lease_of_another_owner_is_refused(store) -> None:
    _take(store, OTHER)
    assert enrichment_allowed(strict=True, store=store) is False
    assert enrichment_allowed(strict=False, store=store) is False
    # Registered locally under a different owner id: still not ours.
    register_local_scheduler(LEASE, ME, lambda: True)
    assert enrichment_allowed(strict=False, store=store) is False


def test_no_lease_follows_strictness(store) -> None:
    assert enrichment_allowed(strict=True, store=store) is False
    assert enrichment_allowed(strict=False, store=store) is True


def test_expired_lease_counts_as_none(store) -> None:
    # lease_duration_s is floored at 1.0 by the store, so expire by rewriting the row.
    _take(store, OTHER)
    import sqlite3

    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE scheduler_leases SET lease_expires_at = 1.0 WHERE lease_name = ?",
        (LEASE,),
    )
    conn.commit()
    conn.close()
    assert store.fetch(lease_name=LEASE)["expired"] is True
    assert enrichment_allowed(strict=True, store=store) is False
    assert enrichment_allowed(strict=False, store=store) is True


def test_registration_cleared_on_release(store) -> None:
    _take(store, ME)
    register_local_scheduler(LEASE, ME, lambda: True)
    assert enrichment_allowed(strict=True, store=store) is True
    clear_local_scheduler(LEASE, ME)
    # Lease row still live but no local registration -> not ours.
    assert enrichment_allowed(strict=True, store=store) is False
    store.release(lease_name=LEASE, owner_id=ME)
    assert enrichment_allowed(strict=True, store=store) is False
    assert enrichment_allowed(strict=False, store=store) is True


def test_clear_ignores_a_different_owner(store) -> None:
    _take(store, ME)
    register_local_scheduler(LEASE, ME, lambda: True)
    clear_local_scheduler(LEASE, "someone-else")
    assert enrichment_allowed(strict=True, store=store) is True


def test_store_error_follows_strictness() -> None:
    broken = MagicMock()
    broken.fetch.side_effect = RuntimeError("db locked")
    assert enrichment_allowed(strict=True, store=broken) is False
    assert enrichment_allowed(strict=False, store=broken) is True


def _service(**kwargs) -> IngestService:
    return IngestService(
        graphiti_client=MagicMock(),
        graph_adapter=MagicMock(),
        llm=MagicMock(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_ensure_worker_starts_nothing_when_not_allowed(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: False)
    await service._ensure_enrichment_worker()
    await service._ensure_enrichment_worker()
    assert service._worker_tasks == []


@pytest.mark.asyncio
async def test_ensure_worker_starts_tasks_when_allowed(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: True)
    try:
        await service._ensure_enrichment_worker()
        assert len(service._worker_tasks) == 1
    finally:
        for task in service._worker_tasks:
            task.cancel()
        await asyncio.gather(*service._worker_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_gate_reads_the_lease_at_call_time(monkeypatch, store) -> None:
    monkeypatch.setattr(
        ingest_queue,
        "enrichment_allowed",
        functools.partial(enrichment_allowed, store=store),
    )
    service = _service(require_scheduler_lease=True)
    assert service._enrichment_allowed() is False  # strict, no lease
    _take(store, ME)
    register_local_scheduler(LEASE, ME, lambda: True)
    assert service._enrichment_allowed() is True
    assert _service()._enrichment_allowed() is True  # non-strict default


@pytest.mark.asyncio
async def test_recover_stale_leases_is_a_noop_when_not_allowed(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: False)
    service.graph_adapter.fail_exhausted_pending_episodes = MagicMock()
    result = await service._recover_stale_episode_leases()
    assert result == (0, 0)
    service.graph_adapter.fail_exhausted_pending_episodes.assert_not_called()
    service.graph_adapter.reset_stale_enriching_episodes.assert_not_called()
    service.graph_adapter.list_pending_episode_uuids.assert_not_called()


@pytest.mark.asyncio
async def test_gate_transition_logs_once(monkeypatch, caplog) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: False)
    with caplog.at_level("WARNING", logger=ingest_queue.logger.name):
        for _ in range(3):
            await service._ensure_enrichment_worker()
    assert sum("not started" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_recover_runs_when_allowed(monkeypatch) -> None:
    service = _service()
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: True)
    adapter = service.graph_adapter
    adapter.fail_exhausted_pending_episodes = MagicMock(return_value=0)
    adapter.fail_transient_exhausted_pending_episodes = MagicMock(return_value=0)
    adapter.reset_orphaned_enriching_episodes = MagicMock(return_value=0)
    adapter.reset_stale_enriching_episodes = MagicMock(return_value=2)
    adapter.reenable_verbatim_skips = None
    adapter.list_pending_episode_uuids = MagicMock(return_value=[])
    service._enqueue_pending_episode = AsyncMock()
    assert await service._recover_stale_episode_leases() == (2, 0)
