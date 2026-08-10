"""D0 view-reachability — unit tests for the entropy probe and its recall seams.

Covers the three surfaces added by the D0-into-menhir integration:
- probe_view_reachability — rank/footprint of each current View's self-recall,
  namespace threading, and the pure-read contract (update_access=False).
- RecallService.recall(update_access=False) — a probe recall must not touch
  access stats (the measurement must not reinforce what it measures).
- RetrievalTrace.view_reachability — where the first current View landed in the
  shipped results; superseded View versions never count.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.recall import QueryPreset, RecallResult, ScoredMemory
from menhir.domain.retrieval_trace_models import RelevanceBreakdown, ViewReachability
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from menhir.services.view_entropy import (
    estimate_footprint_tokens,
    probe_view_reachability,
    walk_to_target,
)

_BREAKDOWN = RelevanceBreakdown(
    semantic_similarity=0.9, adjacency_bonus=0.0, recency_bonus=0.0,
    prominence_bonus=0.0, conflict_bonus=0.0, type_boost=0.0,
    preset="knowledge", alpha=0.2, beta=0.1, gamma=0.1, delta=0.0,
)


def _scored(uuid: str, content: str = "x" * 40) -> ScoredMemory:
    return ScoredMemory(
        uuid=uuid, name=uuid, content=content, scope="PERSISTENT",
        memory_type="SEMANTIC", final_score=0.9, breakdown=_BREAKDOWN,
    )


class FakeRecallService:
    """Returns canned ranked results and records every recall call."""

    def __init__(self, results: list[ScoredMemory]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    async def recall(self, query: str, **kwargs: object) -> RecallResult:
        self.calls.append({"query": query, **kwargs})
        return RecallResult(
            query=query, preset="knowledge", results=self.results,
            candidates_evaluated=len(self.results), nodes_touched=0,
        )


class FakeViewAdapter:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, object]] = []

    def list_views(self, *, kind=None, namespace=None, limit=100):
        self.calls.append({"kind": kind, "namespace": namespace, "limit": limit})
        return self.rows


def _view_row(uuid: str = "view-1", **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "uuid": uuid, "kind": "counter", "subject": "enrichment",
        "value": 4.0, "name": f"surface for {uuid}", "namespace": "",
        "valid_at": "2026-07-02T00:00:00Z",
    }
    row.update(overrides)
    return row


# --- walk_to_target ----------------------------------------------------------


@pytest.mark.unit
def test_walk_to_target_footprint_is_cumulative() -> None:
    results = [_scored("a", "x" * 40), _scored("view-1", "y" * 20), _scored("b")]
    rank, memories, tokens = walk_to_target(results, "view-1")
    assert (rank, memories) == (2, 2)
    assert tokens == estimate_footprint_tokens("x" * 40) + estimate_footprint_tokens("y" * 20)


@pytest.mark.unit
def test_walk_to_target_censored_when_absent() -> None:
    results = [_scored("a"), _scored("b")]
    rank, memories, tokens = walk_to_target(results, "view-1")
    assert rank is None
    assert memories == 2
    assert tokens == sum(estimate_footprint_tokens("x" * 40) for _ in results)


# --- probe_view_reachability -------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_records_rank_and_is_a_pure_read() -> None:
    recall = FakeRecallService([_scored("other"), _scored("view-1")])
    adapter = FakeViewAdapter([_view_row("view-1")])

    out = await probe_view_reachability(
        recall_service=recall, graph_adapter=adapter, top_k=20, max_views=50,
    )

    row = out["views"][0]
    assert row["reached"] is True
    assert row["rank"] == 2
    assert row["memories_to_view"] == 2
    assert row["view_kind"] == "counter"
    summary = out["summary"]
    assert summary["views_probed"] == 1
    assert summary["reached"] == 1
    assert summary["unreached"] == 0
    assert summary["median_rank"] == 2
    assert summary["top_k"] == 20
    # The probe recalls the View's own canonical surface, in-namespace, WITHOUT
    # touching access stats — a measurement must not reinforce what it measures.
    call = recall.calls[0]
    assert call["query"] == "surface for view-1"
    assert call["update_access"] is False
    assert call["namespace"] is None  # group_id "" = default namespace = no filter
    assert call["limit"] == 20
    assert call["preset"] == QueryPreset.KNOWLEDGE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_reports_unreached_views() -> None:
    recall = FakeRecallService([_scored("other-1"), _scored("other-2")])
    adapter = FakeViewAdapter([_view_row("view-1")])

    out = await probe_view_reachability(recall_service=recall, graph_adapter=adapter)

    row = out["views"][0]
    assert row["reached"] is False
    assert row["rank"] is None
    assert out["summary"]["unreached"] == 1
    assert out["summary"]["median_rank"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_threads_the_views_own_namespace() -> None:
    recall = FakeRecallService([_scored("view-1")])
    adapter = FakeViewAdapter([_view_row("view-1", namespace="lme-89941a93")])

    await probe_view_reachability(
        recall_service=recall, graph_adapter=adapter, namespace="lme-89941a93",
    )

    assert adapter.calls[0]["namespace"] == "lme-89941a93"
    assert recall.calls[0]["namespace"] == "lme-89941a93"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_skips_rows_without_surface_or_uuid() -> None:
    recall = FakeRecallService([_scored("view-1")])
    adapter = FakeViewAdapter([
        _view_row("view-1", name=""), _view_row(""), _view_row("view-1"),
    ])

    out = await probe_view_reachability(recall_service=recall, graph_adapter=adapter)

    assert len(out["views"]) == 1
    assert len(recall.calls) == 1


# --- RecallService: update_access gate + trace view_reachability -------------


def _setup(stub_graphiti_client, stub_memory_graph_adapter, *, view_meta=None) -> None:
    last_accessed = datetime.now(timezone.utc) - timedelta(hours=1)
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Test Entity", 0.85),
        ("entity-2", "Another Entity", 0.65),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Test Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "test content", "summary": None,
            "last_accessed": last_accessed, "edge_count": 5, "sharpness": 0.5,
            "freshness": "ACTIVE", "user_flagged": False,
            **(view_meta or {}),
        },
        {
            "uuid": "entity-2", "name": "Another Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "other content", "summary": None,
            "last_accessed": last_accessed, "edge_count": 2, "sharpness": 0.3,
            "freshness": "ACTIVE", "user_flagged": False,
        },
    ]


def _service(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_update_access_false_touches_nothing(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", update_access=False)

    assert result.results  # still a full recall
    assert result.nodes_touched == 0
    assert stub_memory_graph_adapter.touch_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_default_still_touches_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert result.nodes_touched > 0
    assert stub_memory_graph_adapter.touch_count > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trace_records_view_reachability(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(
        stub_graphiti_client, stub_memory_graph_adapter,
        view_meta={"view_kind": "counter", "view_current": True},
    )
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", trace=True)

    vr = result.trace.view_reachability
    assert isinstance(vr, ViewReachability)
    assert vr.uuid == "entity-1"
    assert vr.view_kind == "counter"
    assert vr.rank == 1  # entity-1 has the higher similarity -> shipped first
    assert vr.tokens_to_view == estimate_footprint_tokens("test content")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trace_view_reachability_none_without_views(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", trace=True)

    assert result.trace.view_reachability is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trace_view_reachability_ignores_superseded_views(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """A stale View version surfaced via include_superseded must not count as reached."""
    _setup(
        stub_graphiti_client, stub_memory_graph_adapter,
        view_meta={"view_kind": "counter", "view_current": False},
    )
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", trace=True, include_superseded=True)

    assert result.trace.view_reachability is None
