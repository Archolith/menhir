"""Offline counterexample tests for erasure audit remediation R0.1, R0.2 and R0.4.

Plan: menhir-erasure-audit-remediation-plan-2026-11-11 (CF-324 scheduler wiring, CF-325, CF-328).
Fake graph adapter / real SQLite sidecar and lease store under ``tmp_path``; no Neo4j, no LLM.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services import MaintenanceScheduler
from menhir.services.erasure_coordinator import ErasureCoordinator
from menhir.services.scheduler_lease import SchedulerLeaseStore, enrichment_allowed

pytestmark = [pytest.mark.unit]


# ------------------------------------------------------------------ R0.1: scheduler wiring
class _Ingest:
    async def recover_stale_enrichment_leases(self, limit: int = 100) -> tuple[int, int]:
        return 0, 0


class _Graph:
    pass


async def test_scheduler_registers_and_dispatches_the_fence_orphan_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """CF-324 change 1: job ``erasure_fence_orphans`` exists at a 300s interval and its dispatch
    calls ``erasure_fence_orphans(graph_adapter)``.

    Fails if the job is not registered, has another interval, or the dispatch branch is dropped.
    """
    graph = _Graph()
    sched = MaintenanceScheduler(
        ingest_service=_Ingest(),
        graph_adapter=graph,
        lease_store=SchedulerLeaseStore(db_path=tmp_path / "lease.db"),
        structure_watcher_enabled=False,
        experience_counter_enabled=False,
    )
    assert "erasure_fence_orphans" in sched._jobs
    assert sched._jobs["erasure_fence_orphans"].interval_s == 300.0

    calls: list[object] = []

    async def fake_job(adapter: object) -> dict[str, object]:
        calls.append(adapter)
        return {"released": ["op-x"]}

    monkeypatch.setattr("menhir.services.maintenance_scheduler.erasure_fence_orphans", fake_job)
    only = sched._jobs["erasure_fence_orphans"]
    sched._jobs.clear()
    sched._jobs["erasure_fence_orphans"] = only
    await sched._run_due_jobs()

    assert calls == [graph]
    assert only.runs == 1 and only.last_success is True, only
    assert only.last_result == {"released": ["op-x"]}, only.last_result


# ------------------------------------------------------------------ R0.2 (CF-325)
class _LateTwinAdapter:
    """erasure_compensation_pending is True once, like a tombstone flagged by a racing enrichment."""

    def __init__(self) -> None:
        self.pending_calls = 0
        self.cleared: list[str] = []

    def erasure_compensation_pending(self, node_uuid: str) -> bool:
        self.pending_calls += 1
        return self.pending_calls == 1

    def clear_erasure_compensation(self, node_uuid: str) -> None:
        self.cleared.append(node_uuid)


def _late_twin_outcome(tmp_path, monkeypatch, follow: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    db = tmp_path / "r2.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    adapter = _LateTwinAdapter()
    coord = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    replies = [{"reason": "erased", "op_id": "outer"}, dict(follow)]

    def fake_run(**_kw: Any) -> dict[str, Any]:
        return replies.pop(0)

    monkeypatch.setattr(coord, "_run", fake_run)
    out = coord.erase_memory("q-1")
    assert replies == [], "the follow-up erase did not run"
    return out, adapter


def test_incomplete_late_twin_follow_up_makes_the_outer_reply_incomplete(
    tmp_path, monkeypatch
) -> None:
    """CF-325: a follow-up that is itself erased_incomplete must not leave the outer reply a
    success class; the outer carries late_twin_erase_failed AND the follow-up's reasons.

    Fails if the check goes back to accepting ``erased_incomplete`` as success, or the follow-up's
    ``incomplete_reasons`` are not copied.
    """
    out, adapter = _late_twin_outcome(
        tmp_path,
        monkeypatch,
        {"reason": "erased_incomplete", "incomplete_reasons": ["x_residue"], "op_id": "late"},
    )
    assert out["reason"] == "erased_incomplete", out
    assert "late_twin_erase_failed" in out["incomplete_reasons"], out
    assert "x_residue" in out["incomplete_reasons"], out
    assert out["late_twin_erased"] == "erased_incomplete", out
    assert adapter.cleared == ["q-1"]


@pytest.mark.parametrize("reason", ["erased", "graph_already_absent"])
def test_clean_late_twin_follow_up_keeps_the_outer_success(
    reason: str, tmp_path, monkeypatch
) -> None:
    """Control for CF-325: a complete follow-up does not downgrade the outer reply."""
    out, _ = _late_twin_outcome(tmp_path, monkeypatch, {"reason": reason, "op_id": "late"})
    assert out["reason"] == "erased", out
    assert "incomplete_reasons" not in out, out
    assert out["late_twin_erased"] == reason


def test_failed_late_twin_follow_up_is_incomplete_even_without_reasons(
    tmp_path, monkeypatch
) -> None:
    """CF-325: any non-success follow-up (here prepare_failed, no reasons) still downgrades."""
    out, _ = _late_twin_outcome(
        tmp_path, monkeypatch, {"reason": "prepare_failed", "diagnostics": {"error": "x"}}
    )
    assert out["reason"] == "erased_incomplete", out
    assert out["incomplete_reasons"] == ["late_twin_erase_failed"], out


# ------------------------------------------------------------------ R0.4 (CF-328)
class _FlakyRenewStore(SchedulerLeaseStore):
    """The real SQLite store, except ``renew`` raises while ``fail_renew`` is set."""

    fail_renew = False
    renew_errors = 0

    def renew(self, **kwargs: Any) -> bool:
        if self.fail_renew:
            self.renew_errors += 1
            raise sqlite3.OperationalError("database is locked")
        return super().renew(**kwargs)


async def _until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_a_lease_renewal_store_error_does_not_kill_the_run_loop(tmp_path) -> None:
    """CF-328: a sqlite error from ``lease_store.renew`` inside ``_run_loop`` is "not renewed this
    tick". The task stays alive, the strict gate is closed while the provable deadline has lapsed
    (the lease row itself is still live, so only unprovability can be the cause), and it reopens
    after the next successful renewal.

    Fails if ``_run_loop`` stops catching the renewal exception: the task dies on the first error.
    """
    lease_name = f"r0-{uuid.uuid4().hex[:8]}"
    store = _FlakyRenewStore(db_path=tmp_path / "lease.db")
    sched = MaintenanceScheduler(
        ingest_service=_Ingest(),
        graph_adapter=_Graph(),
        lease_store=store,
        lease_name=lease_name,
        structure_watcher_enabled=False,
        experience_counter_enabled=False,
        tick_interval_s=0.01,
        # Provable window 0.2s, but the store floors the row's TTL at 1.0s: during an outage the
        # deadline lapses long before the row does.
        lease_duration_s=0.2,
        lease_heartbeat_s=0.05,
    )
    sched._jobs.clear()  # lifecycle only

    def allowed() -> bool:
        return enrichment_allowed(strict=True, store=store, lease_name=lease_name)

    assert await sched.start() is True
    try:
        assert await _until(allowed), "gate never opened for a healthy scheduler"
        task = sched._task
        assert task is not None

        store.fail_renew = True
        assert await _until(lambda: not allowed()), "gate stayed open through a renewal outage"
        row = store.fetch(lease_name=lease_name)
        assert row is not None and row["expired"] is False, row  # unprovable, not expired
        assert store.renew_errors >= 1
        assert not task.done(), f"run loop died: {task.exception() if task.done() else ''}"
        assert sched.is_running()
        assert sched._lease_lost is False
        assert allowed() is False

        store.fail_renew = False
        assert await _until(allowed, timeout_s=2.0), "gate never reopened after renewal recovered"
        assert not task.done() and sched.is_running()
    finally:
        store.fail_renew = False
        await sched.stop()
    assert not sched.is_running()
