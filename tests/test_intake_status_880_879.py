"""#880 (project-scan timeout status, resume telemetry) and #879 (intake without a consumer)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain import IngestStatus, MemorySession
from menhir.services import ingest_queue
from menhir.services.ingest_intake import IngestIntakeMixin
from menhir.services.ingest_service import IngestService
from menhir.services.project_ingest import ProjectEpisodeStatus, execute_project_ingest

pytestmark = pytest.mark.unit


def _session() -> MemorySession:
    return MemorySession(
        session_id="s1", user_id="u1", started_at=datetime.now(timezone.utc), client_name="c"
    )


# ---- #880.1 ------------------------------------------------------------------------------------


def _scan_backend(queue):
    return SimpleNamespace(
        scan_and_write_project=AsyncMock(
            return_value={"counts": {}, "meta": {}, "narrative": "Narrative"}
        ),
        queue_episode=queue,
    )


def _run(backend, path, **kw):
    return asyncio.run(
        execute_project_ingest(
            backend, path=path, name=None, force=False, session_id="s", user_id="u", **kw
        )
    )


def test_timeout_is_outcome_unknown_not_deferred(tmp_path) -> None:
    async def slow(*a, **k):
        await asyncio.sleep(0.05)
        return {"episode_id": "x"}

    outcome = _run(_scan_backend(AsyncMock(side_effect=slow)), str(tmp_path), queue_timeout_s=0.001)
    assert outcome.episode.status is ProjectEpisodeStatus.OUTCOME_UNKNOWN


def test_scan_passes_stable_idempotency_key_derived_from_narrative(tmp_path) -> None:
    queue = AsyncMock(return_value={"episode_id": "e"})
    backend = _scan_backend(queue)
    _run(backend, str(tmp_path))
    _run(backend, str(tmp_path))
    keys = [c.kwargs["idempotency_key"] for c in queue.await_args_list]
    assert keys[0] == keys[1] and keys[0].startswith("project-scan:")
    backend.scan_and_write_project.return_value = {"narrative": "Different"}
    _run(backend, str(tmp_path))
    assert queue.await_args.kwargs["idempotency_key"] != keys[0]


def test_mcp_formatter_renders_outcome_unknown() -> None:
    from menhir.mcp.tools.ingest import ingest_project as tool_module
    from menhir.services.project_ingest import ProjectEpisodeOutcome, ProjectIngestOutcome

    text = tool_module._format_project_ingest_outcome(
        ProjectIngestOutcome(
            project_name="p",
            episode=ProjectEpisodeOutcome(ProjectEpisodeStatus.OUTCOME_UNKNOWN),
        )
    )
    assert "outcome unknown" in text and "deferred" not in text


# ---- #880.2 ------------------------------------------------------------------------------------


def _service(**kw) -> IngestService:
    return IngestService(
        graphiti_client=MagicMock(), graph_adapter=MagicMock(), llm=MagicMock(), **kw
    )


@pytest.mark.asyncio
async def test_resume_records_skipped_without_lease(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: False)
    events = []
    monkeypatch.setattr(
        ingest_queue, "record_lifecycle_event", lambda **kw: events.append(kw)
    )
    assert await service.resume_pending_episodes() == 0
    final = events[-1]
    assert final["state"] == "skipped"
    assert final["details"]["reason"] == "no_scheduler_lease"
    assert all(e["state"] != "completed" for e in events)


@pytest.mark.asyncio
async def test_resume_queued_counts_only_ids_actually_enqueued(monkeypatch) -> None:
    service = _service()
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: True)
    a = service.graph_adapter
    a.fail_exhausted_pending_episodes = MagicMock(return_value=0)
    a.fail_transient_exhausted_pending_episodes = MagicMock(return_value=0)
    a.reset_orphaned_enriching_episodes = MagicMock(return_value=0)
    a.reset_stale_enriching_episodes = MagicMock(return_value=0)
    a.reenable_verbatim_skips = None
    a.list_pending_episode_uuids = MagicMock(return_value=["a", "b", "c"])
    service._queued_episode_ids.add("a")
    try:
        assert await service.resume_pending_episodes() == 2
    finally:
        for t in service._worker_tasks:
            t.cancel()
        await asyncio.gather(*service._worker_tasks, return_exceptions=True)


# ---- #879 --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_worker_reports_consumer(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: False)
    assert await service._ensure_enrichment_worker() is False
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: True)
    try:
        assert await service._ensure_enrichment_worker() is True
    finally:
        for t in service._worker_tasks:
            t.cancel()
        await asyncio.gather(*service._worker_tasks, return_exceptions=True)


class _Intake(IngestIntakeMixin):
    def __init__(self, has_consumer: bool):
        self._enrichment_enabled = True
        self._ensure_enrichment_worker = AsyncMock(return_value=has_consumer)
        self._enqueue_pending_episode = AsyncMock(return_value=True)
        self._record_episode_for_enrichment = lambda *a: ("ep-1", "ev-1")


@pytest.mark.asyncio
async def test_intake_without_consumer_skips_enqueue_and_warns() -> None:
    intake = _Intake(False)
    result = await intake.queue_episode_for_enrichment("t", _session(), "mcp")
    intake._enqueue_pending_episode.assert_not_awaited()
    assert result.status is IngestStatus.QUEUED
    assert result.episode_id == "ep-1"
    assert result.processing_state == "PENDING"
    assert "pending queue recovery" in (result.warning or "")


@pytest.mark.asyncio
async def test_intake_with_consumer_still_enqueues_without_warning() -> None:
    intake = _Intake(True)
    result = await intake.queue_episode_for_enrichment("t", _session(), "mcp")
    assert intake._enqueue_pending_episode.await_count == 2
    assert result.warning is None


@pytest.mark.asyncio
async def test_follower_that_later_gains_lease_recovers_skipped_pending(monkeypatch) -> None:
    service = _service(require_scheduler_lease=True)
    allowed = {"v": False}
    monkeypatch.setattr(service, "_enrichment_allowed", lambda: allowed["v"])
    a = service.graph_adapter
    a.fail_exhausted_pending_episodes = MagicMock(return_value=0)
    a.fail_transient_exhausted_pending_episodes = MagicMock(return_value=0)
    a.reset_orphaned_enriching_episodes = MagicMock(return_value=0)
    a.reset_stale_enriching_episodes = MagicMock(return_value=0)
    a.reenable_verbatim_skips = None
    a.list_pending_episode_uuids = MagicMock(return_value=["skipped-1"])
    service._enrichment_enabled = True
    service._record_episode_for_enrichment = lambda *a_: ("skipped-1", None)

    result = await service.queue_episode_for_enrichment("t", _session(), "mcp")
    assert result.warning
    assert "skipped-1" not in service._queued_episode_ids
    assert service.get_queue_depth() == 0

    allowed["v"] = True  # lease acquired later; the scheduler's recover_stale_leases job runs
    try:
        _, queued = await service.recover_stale_enrichment_leases()
        assert queued == 1
        assert "skipped-1" in service._queued_episode_ids
    finally:
        for t in service._worker_tasks:
            t.cancel()
        await asyncio.gather(*service._worker_tasks, return_exceptions=True)


# ---- enqueue / requeue without a consumer ----------------------------------------------------


@pytest.mark.parametrize(
    ("method", "state"),
    [("enqueue_pending_episode", "PENDING"), ("requeue_failed_episode", "FAILED")],
)
@pytest.mark.asyncio
async def test_enqueue_and_requeue_return_false_without_consumer(monkeypatch, method, state) -> None:
    svc = _service(require_scheduler_lease=True)
    monkeypatch.setattr(svc, "_enrichment_allowed", lambda: False)
    svc._enrichment_enabled = True
    svc.graph_adapter.fetch_episode_processing = MagicMock(
        return_value={"processing_state": state}
    )
    assert await getattr(svc, method)("ep-1") is False
    assert "ep-1" not in svc._queued_episode_ids
    assert svc.get_queue_depth() == 0


@pytest.mark.parametrize(
    ("method", "state"),
    [("enqueue_pending_episode", "PENDING"), ("requeue_failed_episode", "FAILED")],
)
@pytest.mark.asyncio
async def test_enqueue_and_requeue_return_true_with_consumer(monkeypatch, method, state) -> None:
    svc = _service(require_scheduler_lease=True)
    monkeypatch.setattr(svc, "_enrichment_allowed", lambda: True)
    svc._enrichment_enabled = True
    svc.graph_adapter.fetch_episode_processing = MagicMock(
        return_value={"processing_state": state}
    )
    try:
        assert await getattr(svc, method)("ep-1") is True
        assert "ep-1" in svc._queued_episode_ids
    finally:
        for t in svc._worker_tasks:
            t.cancel()
        await asyncio.gather(*svc._worker_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_force_reenrich_reports_not_queued_without_consumer(monkeypatch) -> None:
    from menhir.mcp.tools.ops.force_reenrich import ForceReenrichTool

    backend = SimpleNamespace(
        force_reset_failed_episode=AsyncMock(return_value=True),
        enqueue_pending_episode=AsyncMock(return_value=False),
    )
    tool = ForceReenrichTool()
    monkeypatch.setattr(tool, "get_backend", lambda: backend)
    out = await tool.endpoint(episode_uuid="11111111-1111-4111-8111-111111111111", wait=False)
    assert "queued: False" in out
    assert "pending queue recovery" in out
    assert "result: submitted" not in out


# ---- scan replaying a FAILED episode -----------------------------------------------------------


def _replay(state):
    return {
        "episode_id": "ep-old",
        "status": "queued",
        "idempotent_replay": True,
        "processing_state": state,
    }


def test_scan_replaying_failed_episode_reports_failed(tmp_path) -> None:
    outcome = _run(_scan_backend(AsyncMock(return_value=_replay("FAILED"))), str(tmp_path))
    assert outcome.episode.status is ProjectEpisodeStatus.FAILED
    assert outcome.episode.episode_id == "ep-old"
    assert "force_reenrich" in (outcome.episode.error or "")

    from menhir.mcp.tools.ingest import ingest_project as tool_module

    text = tool_module._format_project_ingest_outcome(outcome)
    assert "ep-old" in text and "force_reenrich" in text


@pytest.mark.parametrize("state", ["READY", "PENDING"])
def test_scan_replaying_other_states_stays_queued(tmp_path, state) -> None:
    outcome = _run(_scan_backend(AsyncMock(return_value=_replay(state))), str(tmp_path))
    assert outcome.episode.status is ProjectEpisodeStatus.QUEUED
    assert outcome.episode.episode_id == "ep-old"
