"""Behavioral guards for CF-166 (revision retention window).

The source-scan guards in test_high_wave3_retention_scans.py pass even if production forwards a
constant 14 instead of the configured window. These tests build the scheduler through its real
constructors, run the registered job through the real dispatch path (`_run_due_jobs` ->
`_run_job` -> task -> store), and assert the exact configured value arrives at the store.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.services.maintenance_scheduler import MaintenanceScheduler

pytestmark = pytest.mark.unit

JOB = "prune_telemetry_revisions"
# Deliberately not the default (14) and not 1 (the task clamps to >= 1).
WINDOW = 37


class _Ingest:
    def get_queue_depth(self) -> int:
        return 0


def _scheduler(**kwargs) -> MaintenanceScheduler:
    return MaintenanceScheduler(ingest_service=_Ingest(), graph_adapter=object(), **kwargs)


def _only_run(scheduler: MaintenanceScheduler, name: str) -> None:
    """Mark every other job as just-started so `_run_due_jobs` skips it by interval."""
    for key, job in scheduler._jobs.items():
        if key != name:
            job.last_started_at = "2999-01-01T00:00:00+00:00"


def _record_store(monkeypatch) -> list[int]:
    from menhir.infrastructure.telemetry import telemetry_store

    calls: list[int] = []

    def _recording_prune(*, retention_days: int) -> int:
        calls.append(retention_days)
        return 5

    monkeypatch.setattr(telemetry_store, "prune_old_revisions", _recording_prune)
    return calls


def test_configured_window_reaches_task_and_store_through_dispatch(monkeypatch) -> None:
    import menhir.services.maintenance_scheduler as ms

    store_calls = _record_store(monkeypatch)
    task_calls: list[int] = []
    real_task = ms.prune_telemetry_revisions

    async def _recording_task(*, retention_days: int):
        task_calls.append(retention_days)
        return await real_task(retention_days=retention_days)

    monkeypatch.setattr(ms, "prune_telemetry_revisions", _recording_task)

    scheduler = _scheduler(revision_retention_days=WINDOW)
    assert JOB in scheduler._jobs
    _only_run(scheduler, JOB)

    asyncio.run(scheduler._run_due_jobs())

    assert task_calls == [WINDOW]
    assert store_calls == [WINDOW]
    state = scheduler._jobs[JOB]
    assert state.runs == 1
    assert state.last_success is True
    # The result is computed by the task from what the store reported, not echoed from a fixture.
    assert state.last_result == {"retention_days": WINDOW, "rows_deleted": 5}


def test_disabled_window_registers_nothing_and_dispatch_never_prunes(monkeypatch) -> None:
    store_calls = _record_store(monkeypatch)

    scheduler = _scheduler(revision_retention_days=0)
    assert JOB not in scheduler._jobs

    asyncio.run(scheduler._run_due_jobs())
    assert store_calls == []


def test_enabled_job_is_skipped_until_due_then_runs_once_per_interval(monkeypatch) -> None:
    """Dispatch really gates on the interval: a second immediate pass must not re-prune."""
    store_calls = _record_store(monkeypatch)
    scheduler = _scheduler(revision_retention_days=WINDOW)
    _only_run(scheduler, JOB)

    asyncio.run(scheduler._run_due_jobs())
    asyncio.run(scheduler._run_due_jobs())

    assert store_calls == [WINDOW]


def _build_via_runtime(monkeypatch, env_value: str) -> MaintenanceScheduler:
    """Real settings from env -> real `_start_scheduler` -> real MaintenanceScheduler."""
    import menhir.core.runtime as runtime

    monkeypatch.setenv("MENHIR_REVISION_RETENTION_DAYS", env_value)
    # Keep startup side tasks (initial structure scan, artifact reconcile) out of the test.
    monkeypatch.setenv("MENHIR_STRUCTURE_WATCHER_ENABLED", "false")
    monkeypatch.setenv("MENHIR_ARTIFACT_RECONCILE_MODE", "off")
    settings = MemorySettings.from_env()

    async def _start_without_lease(_scheduler):
        return True

    monkeypatch.setattr(MaintenanceScheduler, "start", _start_without_lease)
    monkeypatch.setattr(MaintenanceScheduler, "status_snapshot", lambda self: {"running": True})
    monkeypatch.setattr(runtime, "_state", SimpleNamespace(scheduler=None))
    monkeypatch.setattr(runtime, "make_view_embedder", lambda s: None)
    monkeypatch.setattr(runtime, "view_embedder_version", lambda s: "test")
    built = SimpleNamespace(
        settings=settings,
        ingest_service=_Ingest(),
        graph_adapter=SimpleNamespace(neo4j=None),
        lifecycle_service=None,
    )
    return asyncio.run(runtime._start_scheduler(built))


def test_env_setting_flows_through_runtime_construction_to_the_store(monkeypatch) -> None:
    store_calls = _record_store(monkeypatch)

    scheduler = _build_via_runtime(monkeypatch, str(WINDOW))

    assert scheduler.revision_retention_days == WINDOW
    assert JOB in scheduler._jobs
    _only_run(scheduler, JOB)
    asyncio.run(scheduler._run_due_jobs())
    assert store_calls == [WINDOW]


def test_env_zero_disables_the_job_through_runtime_construction(monkeypatch) -> None:
    store_calls = _record_store(monkeypatch)

    scheduler = _build_via_runtime(monkeypatch, "0")

    assert JOB not in scheduler._jobs
    asyncio.run(scheduler._run_due_jobs())
    assert store_calls == []
