"""#938: one failing classify_pair must not abort the rest of the consolidation pass.

The candidate whose check raised is not promoted (the next pass retries it); other candidates are
still checked and promoted, and callers that pass no error list keep the old raising behaviour."""

from __future__ import annotations

import pytest

from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.merge_coordinator import MergeDrift

pytestmark = pytest.mark.unit

HITS = [("p1", "P1", 0.9), ("p2", "P2", 0.9), ("p3", "P3", 0.9)]


class _Correlation:
    def __init__(self, failures: dict[tuple[str, str], BaseException]) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    async def classify_pair(self, uuid, other_uuid, score):
        self.calls.append((uuid, other_uuid))
        if (uuid, other_uuid) in self.failures:
            raise self.failures[(uuid, other_uuid)]
        return "none", None


def _svc(adapter, graphiti, correlation, monkeypatch) -> LifecycleService:
    adapter.session_entities = [
        {
            "uuid": u, "name": f"entity-{u}", "content": f"content {u}", "summary": None,
            "type": "SEMANTIC", "sharpness": 0.0, "user_flagged": False,
            "session_id": "session-1", "created_at": "2026-03-01T00:00:00Z",
        }
        for u in ("a", "b")
    ]
    graphiti.search_scored_results = HITS
    svc = LifecycleService(graph_adapter=adapter, graphiti_client=graphiti)
    monkeypatch.setattr(svc, "_build_correlation_service", lambda: correlation)
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [MergeDrift("drift"), RuntimeError("neo4j transient")], ids=["merge_drift", "runtime_error"],
)
async def test_failing_pair_is_skipped_and_later_phases_run(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch, exc,
):
    correlation = _Correlation({("a", "p2"): exc})
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, correlation, monkeypatch)

    result = await svc.consolidate_session("session-1")

    assert [c for c in correlation.calls if c[0] == "a"] == [("a", "p1"), ("a", "p2")]
    assert [c for c in correlation.calls if c[0] == "b"] == [("b", "p1"), ("b", "p2"), ("b", "p3")]
    assert result.merge_errors == 1
    assert "b" in stub_memory_graph_adapter.promoted_nodes
    assert "a" not in stub_memory_graph_adapter.promoted_nodes


@pytest.mark.asyncio
async def test_usage_control_signal_still_propagates(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
):
    correlation = _Correlation({("a", "p1"): LlmUsageControlSignal("budget")})
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, correlation, monkeypatch)

    with pytest.raises(LlmUsageControlSignal):
        await svc.consolidate_session("session-1")

    assert correlation.calls == [("a", "p1")]


@pytest.mark.asyncio
async def test_callers_without_error_list_still_raise(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
):
    correlation = _Correlation({("a", "p1"): RuntimeError("neo4j transient")})
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, correlation, monkeypatch)

    with pytest.raises(RuntimeError):
        await svc._check_contradictions_batch([{"uuid": "a", "name": "entity-a", "content": "content a"}])


@pytest.mark.asyncio
async def test_a_candidate_whose_check_raised_is_not_ttl_deleted_in_the_same_pass(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
):
    # "a" was demoted earlier and its TTL has expired; this pass makes it a promotion candidate,
    # but its check raises. It must survive the sweep; an unrelated expired node is still deleted.
    stub_memory_graph_adapter.ttl_expired_uuids = [{"uuid": "a"}, {"uuid": "z"}]
    correlation = _Correlation({("a", "p1"): RuntimeError("neo4j transient")})
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, correlation, monkeypatch)

    result = await svc.consolidate_session("session-1")

    assert result.merge_errors == 1
    assert "a" not in stub_memory_graph_adapter.deleted_session_nodes
    assert "a" not in stub_memory_graph_adapter.promoted_nodes
    assert "z" in stub_memory_graph_adapter.deleted_session_nodes


@pytest.mark.asyncio
async def test_saga_pause_still_propagates(
    stub_memory_graph_adapter, stub_graphiti_client, monkeypatch,
):
    from menhir.infrastructure.graph_operations_model import SagaWritesPausedError

    correlation = _Correlation({("a", "p1"): SagaWritesPausedError("paused")})
    svc = _svc(stub_memory_graph_adapter, stub_graphiti_client, correlation, monkeypatch)

    with pytest.raises(SagaWritesPausedError):
        await svc.consolidate_session("session-1")
