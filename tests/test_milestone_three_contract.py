"""Milestone 3 completion contract tests.

These tests verify that all M3 acceptance criteria are met at the code level.
They assert structural contracts — types, methods, fields, and wiring.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from menhir.domain.recall import (
    CandidateData,
    PRESET_WEIGHTS,
    QueryPreset,
    RecallResult,
    ScoredMemory,
)
from menhir.domain.retrieval_trace_models import RelevanceBreakdown
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


@pytest.mark.unit
def test_recall_service_is_async() -> None:
    """RecallService.recall must be async."""
    assert asyncio.iscoroutinefunction(RecallService.recall)


@pytest.mark.unit
def test_recall_result_contains_explainability_breakdown() -> None:
    """RecallResult and ScoredMemory must carry the explainability types."""
    breakdown = RelevanceBreakdown(
        semantic_similarity=0.8,
        adjacency_bonus=0.2,
        recency_bonus=0.9,
        prominence_bonus=0.5,
        conflict_bonus=0.0,
        type_boost=0.0,
        preset="knowledge",
        alpha=0.2,
        beta=0.1,
        gamma=0.5,
        delta=0.0,
    )
    scored = ScoredMemory(
        uuid="test",
        name="test",
        content="content",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        final_score=1.5,
        breakdown=breakdown,
    )
    result = RecallResult(
        query="q",
        preset="knowledge",
        results=[scored],
        candidates_evaluated=1,
        nodes_touched=1,
    )

    assert result.results[0].breakdown.semantic_similarity == 0.8
    assert result.results[0].breakdown.preset == "knowledge"


@pytest.mark.unit
def test_all_five_presets_are_defined() -> None:
    """All five query presets must be defined with weights."""
    presets = list(QueryPreset)
    assert len(presets) == 5
    assert set(p.value for p in presets) == {"recent", "knowledge", "emotional", "connected", "conflict"}

    for preset in presets:
        weights = PRESET_WEIGHTS[preset]
        assert len(weights) == 4
        assert all(isinstance(w, float) for w in weights)


@pytest.mark.unit
def test_scoring_service_is_stateless() -> None:
    """ScoringService must not require any I/O dependencies."""
    sig = inspect.signature(ScoringService.__init__)
    params = set(sig.parameters.keys()) - {"self"}
    # ScoringService should be stateless — no required init params
    assert params == set()


@pytest.mark.unit
def test_recall_service_depends_on_graphiti_and_adapter() -> None:
    """RecallService must depend on GraphitiClient, MemoryGraphAdapter, and ScoringService."""
    fields = {f.name for f in RecallService.__dataclass_fields__.values()}
    assert "graphiti_client" in fields
    assert "graph_adapter" in fields
    assert "scoring_service" in fields


@pytest.mark.unit
def test_scored_memory_has_required_fields() -> None:
    """ScoredMemory must carry all required explainability fields."""
    fields = {f.name for f in ScoredMemory.__dataclass_fields__.values()}
    required = {"uuid", "name", "content", "scope", "memory_type", "final_score", "breakdown"}
    assert required <= fields
    # Optional additions beyond the required core:
    #   warden_label       — frontier warden-gate FLAG label (None on live path without warden)
    #   temporal_facts     — rung1a bi-temporal facts attached post-rank (empty list by default)
    #   is_superseded_view — True only for a superseded View version surfaced via include_superseded
    #   view_kind          — counter-View claim-shape id; used in dedup to keep distinct view kinds
    #                        from collapsing (rank-inert; display/dedup only)
    #   stale_anchor_info  — Hook Center stale-anchor labeling metadata (None when not evaluated;
    #                        display/advisory only, rank-inert)
    #   retrieval_score / retrieval_score_kind — honest raw retrieval lane metadata
    #   is_scalar_authority — marks the slot-keyed injected View as the current value so the consumer
    #                        leads with it and treats other observations as history (Phase 4c; DATA only)
    assert fields - required == {
        "warden_label",
        "temporal_facts",
        "is_superseded_view",
        "view_kind",
        "stale_anchor_info",
        "retrieval_score",
        "retrieval_score_kind",
        "is_scalar_authority",
    }


@pytest.mark.unit
def test_relevance_breakdown_has_required_fields() -> None:
    """RelevanceBreakdown must carry all four component scores and preset info."""
    fields = {f.name for f in RelevanceBreakdown.__dataclass_fields__.values()}
    assert fields == {
        "semantic_similarity", "adjacency_bonus", "recency_bonus", "prominence_bonus",
        "conflict_bonus", "type_boost", "preset", "alpha", "beta", "gamma", "delta",
    }


@pytest.mark.unit
def test_graphiti_client_has_search_scored_method() -> None:
    """GraphitiClient must expose search_scored as an async method."""
    method = getattr(GraphitiClient, "search_scored", None)
    assert method is not None
    assert asyncio.iscoroutinefunction(method)


@pytest.mark.unit
def test_adapter_has_retrieval_methods() -> None:
    """MemoryGraphAdapter must expose fetch_candidate_metadata, fetch_adjacency_pairs, touch_retrieved_nodes."""
    for method_name in ("fetch_candidate_metadata", "fetch_adjacency_pairs", "touch_retrieved_nodes"):
        assert callable(getattr(MemoryGraphAdapter, method_name, None)), f"Missing: {method_name}"


@pytest.mark.unit
def test_recall_result_is_frozen() -> None:
    """RecallResult, ScoredMemory, and RelevanceBreakdown must be frozen dataclasses."""
    breakdown = RelevanceBreakdown(
        semantic_similarity=0.5, adjacency_bonus=0.1, recency_bonus=0.9,
        prominence_bonus=0.3, conflict_bonus=0.0, type_boost=0.0,
        preset="knowledge", alpha=0.2, beta=0.1, gamma=0.5, delta=0.0,
    )
    scored = ScoredMemory(
        uuid="t", name="t", content=None, scope="PERSISTENT",
        memory_type="SEMANTIC", final_score=1.0, breakdown=breakdown,
    )
    result = RecallResult(query="q", preset="knowledge", results=[scored],
                          candidates_evaluated=1, nodes_touched=1)

    with pytest.raises(AttributeError):
        result.query = "changed"
    with pytest.raises(AttributeError):
        scored.uuid = "changed"
    with pytest.raises(AttributeError):
        breakdown.preset = "changed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_service_returns_recall_result_type(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    """RecallService.recall must return a RecallResult, not a dict."""
    stub_graphiti_client.search_scored_results = [("e-1", "Test", 0.8)]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "e-1", "name": "Test", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "c", "summary": None, "last_accessed": None,
            "edge_count": 1, "sharpness": 0.0, "freshness": "ACTIVE", "user_flagged": False,
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )

    result = await svc.recall("test")

    assert isinstance(result, RecallResult)
    assert len(result.results) > 0
    assert isinstance(result.results[0], ScoredMemory)
