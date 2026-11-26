"""#1008: a failed contradiction search must not promote the candidate unchecked, and merge_errors
must be reported on scheduled runs (log line, MCP event, scheduler result dict)."""

from __future__ import annotations

import logging

import pytest

from menhir.services import lifecycle_consolidation
from menhir.services.lifecycle_service import ConsolidationResult, LifecycleService
from menhir.services.maintenance_scheduler import MaintenanceScheduler

pytestmark = pytest.mark.unit


class _Correlation:
    async def classify_pair(self, uuid, other_uuid, score):
        return "none", None


def _svc(adapter, graphiti, monkeypatch, failing: set[str]) -> LifecycleService:
    adapter.session_entities = [
        {
            "uuid": u, "name": f"entity-{u}", "content": f"content {u}", "summary": None,
            "type": "SEMANTIC", "sharpness": 0.0, "user_flagged": False,
            "session_id": "session-1", "created_at": "2026-03-01T00:00:00Z",
        }
        for u in ("a", "b")
    ]
    graphiti.search_scored_results = [("p1", "P1", 0.9)]
    original = graphiti.search_scored

    async def flaky(query, *, num_results=50, group_ids=None):
        if query.removeprefix("content ") in failing:
            raise RuntimeError("search down")
        return await original(query, num_results=num_results, group_ids=group_ids)

    monkeypatch.setattr(graphiti, "search_scored", flaky)
    svc = LifecycleService(graph_adapter=adapter, graphiti_client=graphiti)
    monkeypatch.setattr(svc, "_build_correlation_service", lambda: _Correlation())
    return svc


@pytest.mark.asyncio
async def test_search_failure_is_not_promoted_nor_ttl_deleted(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch, caplog,
):
    stub_memory_graph_adapter.ttl_expired_uuids = [{"uuid": "a"}, {"uuid": "z"}]
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, monkeypatch, {"a"})
    events: list[dict] = []
    monkeypatch.setattr(lifecycle_consolidation, "record_mcp_event", lambda **kw: events.append(kw))

    with caplog.at_level(logging.INFO):
        result = await svc.consolidate_session("session-1")

    assert result.merge_errors == 1
    assert "a" not in stub_memory_graph_adapter.promoted_nodes
    assert "a" not in stub_memory_graph_adapter.deleted_session_nodes
    assert "b" in stub_memory_graph_adapter.promoted_nodes
    assert "z" in stub_memory_graph_adapter.deleted_session_nodes
    assert any("merge_errors=1" in r.getMessage() for r in caplog.records)
    assert events[-1]["result"]["merge_errors"] == 1


@pytest.mark.asyncio
async def test_callers_without_error_list_are_unchanged(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
):
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, monkeypatch, {"a"})

    conflicts = await svc._check_contradictions_batch(
        [{"uuid": "a", "name": "entity-a", "content": "content a"}]
    )

    assert conflicts == 0


class _Ingest:
    def get_queue_depth(self) -> int:
        return 0


class _FakeLifecycle:
    async def recover_orphans(self, max_age_hours: float = 4.0) -> ConsolidationResult:
        return ConsolidationResult(
            promoted=1, deleted=0, conflicts_detected=0, skipped_pending=0,
            orphan_episodes_cleaned=0, merge_errors=3,
        )


@pytest.mark.asyncio
async def test_scheduler_result_dict_includes_merge_errors() -> None:
    scheduler = MaintenanceScheduler(
        ingest_service=_Ingest(), graph_adapter=object(), lifecycle_service=_FakeLifecycle()
    )

    result = await scheduler._make_consolidate_lifecycle()

    assert result["merge_errors"] == 3
