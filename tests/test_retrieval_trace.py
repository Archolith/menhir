"""R0 retrieval observability — unit tests for the inline trace.

Covers the two surfaces:
- ScoringService.score_candidates(trace=...) — the floor-capture meat (which
  candidates survived the source-aware floor, with score parts + rank).
- RecallService.recall(trace=...) — end-to-end assembly + the default-off
  no-op contract (production path unchanged unless a caller opts in).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from menhir.domain.recall import CandidateData, QueryPreset, RecallResult
from menhir.domain.retrieval_trace_models import CandidateTrace, RetrievalTrace, ScoringTrace
from menhir.domain.retrieval_tuning import CandidateSource
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


def _make_candidate(**overrides: object) -> CandidateData:
    defaults = dict(
        uuid="node-1",
        name="test",
        content="some content",
        scope="PERSISTENT",
        memory_type="SEMANTIC",
        similarity=0.8,
        last_accessed_days_ago=1.0,
        edge_count=5,
        adjacency_score=0.5,
        freshness="ACTIVE",
    )
    defaults.update(overrides)
    return CandidateData(**defaults)


# --- ScoringService trace ---------------------------------------------------


@pytest.mark.unit
def test_score_trace_defaults_to_none_and_is_noop() -> None:
    """trace defaults to None and produces identical results to omitting it."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="a", similarity=0.7, edge_count=3),
        _make_candidate(uuid="b", similarity=0.5, edge_count=8),
    ]
    without = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)
    with_none = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, trace=None)

    assert [r.uuid for r in without] == [r.uuid for r in with_none]
    assert [r.final_score for r in without] == [r.final_score for r in with_none]


@pytest.mark.unit
def test_score_trace_records_survivors_with_rank_and_parts() -> None:
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="low", similarity=0.2, edge_count=0, adjacency_score=0.0),
        _make_candidate(uuid="high", similarity=0.9, edge_count=10, adjacency_score=1.0),
    ]
    trace = ScoringTrace()
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, trace=trace)

    survivors = [c for c in trace.candidates if c.survived_floor]
    assert {c.uuid for c in survivors} == {"low", "high"}
    # Ranks follow the sorted result order, contiguous from 0.
    by_uuid = {c.uuid: c for c in survivors}
    assert by_uuid["high"].rank == 0
    assert by_uuid["low"].rank == 1
    # Score parts + final score are carried for survivors and match the result.
    result_by_uuid = {r.uuid: r for r in results}
    for c in survivors:
        assert c.score_parts is not None
        assert c.score_parts == result_by_uuid[c.uuid].breakdown
        assert c.final_score == result_by_uuid[c.uuid].final_score


@pytest.mark.unit
def test_score_trace_records_floored_vector_candidate() -> None:
    """A vector candidate below the floor is recorded, not silently dropped."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="keep", similarity=0.5, source=CandidateSource.VECTOR),
        _make_candidate(uuid="drop", similarity=0.05, source=CandidateSource.VECTOR),
    ]
    trace = ScoringTrace()
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, trace=trace)

    assert [r.uuid for r in results] == ["keep"]
    dropped = next(c for c in trace.candidates if c.uuid == "drop")
    assert dropped.survived_floor is False
    assert dropped.score_parts is None
    assert dropped.final_score is None
    assert dropped.rank is None
    assert dropped.similarity == 0.05


@pytest.mark.unit
def test_score_trace_floor_is_source_aware() -> None:
    """BM25 below the floor survives (exempt); vector below the floor does not."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="vec_low", similarity=0.05, source=CandidateSource.VECTOR),
        _make_candidate(uuid="bm25_low", similarity=0.05, source=CandidateSource.BM25),
    ]
    trace = ScoringTrace()
    svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, trace=trace)

    by_uuid = {c.uuid: c for c in trace.candidates}
    assert by_uuid["bm25_low"].survived_floor is True
    assert by_uuid["bm25_low"].source == CandidateSource.BM25
    assert by_uuid["vec_low"].survived_floor is False


@pytest.mark.unit
def test_score_trace_records_all_floored_even_when_empty() -> None:
    """When every candidate is floored, results are empty but the trace records them."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="a", similarity=0.01, source=CandidateSource.VECTOR),
        _make_candidate(uuid="b", similarity=0.05, source=CandidateSource.VECTOR),
    ]
    trace = ScoringTrace()
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, trace=trace)

    assert results == []
    assert {c.uuid for c in trace.candidates} == {"a", "b"}
    assert all(c.survived_floor is False for c in trace.candidates)


# --- RecallService trace ----------------------------------------------------


def _setup(stub_graphiti_client, stub_memory_graph_adapter, *, sims=(0.85, 0.65)) -> None:
    last_accessed = datetime.now(timezone.utc) - timedelta(hours=1)
    stub_graphiti_client.search_scored_results = [
        ("entity-1", "Test Entity", sims[0]),
        ("entity-2", "Another Entity", sims[1]),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "entity-1", "name": "Test Entity", "scope": "PERSISTENT",
            "type": "SEMANTIC", "content": "test content", "summary": None,
            "last_accessed": last_accessed, "edge_count": 5, "sharpness": 0.5,
            "freshness": "ACTIVE", "user_flagged": False,
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
async def test_recall_trace_off_by_default(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query")

    assert isinstance(result, RecallResult)
    assert result.trace is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_trace_populated_when_requested(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _setup(stub_graphiti_client, stub_memory_graph_adapter)
    svc = _service(stub_graphiti_client, stub_memory_graph_adapter)

    result = await svc.recall("test query", trace=True)

    assert isinstance(result.trace, RetrievalTrace)
    tr = result.trace
    assert tr.query == "test query"
    assert tr.preset == QueryPreset.KNOWLEDGE.value
    # Phase timings are surfaced, including the total.
    assert "total" in tr.phases
    assert tr.total_ms == tr.phases["total"]
    assert "vector_search" in tr.phases
    # Both stub candidates are above the floor -> traced as survivors with ranks.
    assert {c.uuid for c in tr.candidates} == {"entity-1", "entity-2"}
    assert all(isinstance(c, CandidateTrace) for c in tr.candidates)
    assert all(c.survived_floor for c in tr.candidates)
    ranks = sorted(c.rank for c in tr.candidates)
    assert ranks == [0, 1]
