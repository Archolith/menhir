"""Unit tests for the pure ScoringService."""

from __future__ import annotations

import math

import pytest

from menhir.domain.recall import (
    CandidateData,
    QueryPreset,
    PRESET_WEIGHTS,
    RetrievalScoreKind,
)
from menhir.services.scoring_service import (
    GRAPHITI_RRF_DUAL_METHOD_MAX,
    MIN_SIMILARITY_THRESHOLD,
    ScoringService,
)


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


@pytest.mark.unit
def test_graphiti_rrf_scale_contract() -> None:
    """Pin the RRF scale MIN_SIMILARITY_THRESHOLD gates (plan 1a).

    The 0.15 floor and GRAPHITI_RRF_DUAL_METHOD_MAX are calibrated against
    graphiti's RRF reranker score, whose dual-method top hit == 2.0 under
    rank_const=1. If graphiti changes rank_const or the RRF formula, this fails
    loudly so the floor (and the PENDING=1.0 mid-rank accident it documents) is
    re-examined, not silently rescaled.
    """
    import inspect

    from graphiti_core.search.search_utils import rrf

    # The rank_const the whole scale rides on.
    assert inspect.signature(rrf).parameters["rank_const"].default == 1

    # Dual-method top hit (both rankers place uuid "a" first) tops out at the
    # pinned max; single-method top hit == 1.0.
    _uuids, dual_scores = rrf([["a"], ["a"]])
    assert dual_scores[0] == GRAPHITI_RRF_DUAL_METHOD_MAX == 2.0

    _uuids, single_scores = rrf([["a"]])
    assert single_scores[0] == 1.0

    # The floor is a rank cut well below either ceiling, not a cosine cutoff.
    assert 0.0 < MIN_SIMILARITY_THRESHOLD < 1.0


@pytest.mark.unit
def test_score_candidates_applies_knowledge_preset_weights() -> None:
    svc = ScoringService()
    candidates = [_make_candidate()]
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)

    assert len(results) == 1
    r = results[0]
    alpha, beta, gamma, delta = PRESET_WEIGHTS[QueryPreset.KNOWLEDGE]
    assert r.breakdown.alpha == alpha
    assert r.breakdown.beta == beta
    assert r.breakdown.gamma == gamma
    assert r.breakdown.delta == delta
    assert r.breakdown.preset == "knowledge"


@pytest.mark.unit
def test_score_candidates_preserves_raw_score_semantics() -> None:
    result = ScoringService().score_candidates(
        [
            _make_candidate(
                retrieval_score=0.42,
                retrieval_score_kind=RetrievalScoreKind.SOURCE_PRIOR,
            )
        ],
        QueryPreset.KNOWLEDGE,
    )[0]

    assert result.retrieval_score == 0.42
    assert result.retrieval_score_kind is RetrievalScoreKind.SOURCE_PRIOR
    assert result.breakdown.semantic_similarity == 0.8  # legacy field is preserved


@pytest.mark.unit
def test_score_candidates_applies_recent_preset_weights() -> None:
    svc = ScoringService()
    candidates = [_make_candidate()]
    results = svc.score_candidates(candidates, QueryPreset.RECENT)

    r = results[0]
    alpha, beta, gamma, delta = PRESET_WEIGHTS[QueryPreset.RECENT]
    assert r.breakdown.alpha == alpha
    assert r.breakdown.beta == beta
    assert r.breakdown.gamma == gamma
    assert r.breakdown.delta == delta


@pytest.mark.unit
def test_score_candidates_ranks_by_final_score_descending() -> None:
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="low", similarity=0.2, edge_count=0, adjacency_score=0.0),
        _make_candidate(uuid="high", similarity=0.9, edge_count=10, adjacency_score=1.0),
        _make_candidate(uuid="mid", similarity=0.5, edge_count=3, adjacency_score=0.3),
    ]
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)

    assert results[0].uuid == "high"
    assert results[-1].uuid == "low"
    scores = [r.final_score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_recency_bonus_decays_exponentially() -> None:
    svc = ScoringService()
    fresh = _make_candidate(uuid="fresh", last_accessed_days_ago=0.0, edge_count=1, adjacency_score=0.0)
    stale = _make_candidate(uuid="stale", last_accessed_days_ago=30.0, edge_count=1, adjacency_score=0.0)

    results = svc.score_candidates([fresh, stale], QueryPreset.RECENT)
    fresh_r = next(r for r in results if r.uuid == "fresh")
    stale_r = next(r for r in results if r.uuid == "stale")

    assert fresh_r.breakdown.recency_bonus > stale_r.breakdown.recency_bonus
    assert abs(fresh_r.breakdown.recency_bonus - 1.0) < 1e-9
    expected_stale = math.exp(-0.1 * 30.0)
    assert abs(stale_r.breakdown.recency_bonus - expected_stale) < 1e-9


@pytest.mark.unit
def test_prominence_bonus_is_log_scaled() -> None:
    """Prominence computation is log-scaled and contributes once gamma is active."""
    svc = ScoringService()
    low = _make_candidate(uuid="low", edge_count=1, adjacency_score=0.0)
    high = _make_candidate(uuid="high", edge_count=50, adjacency_score=0.0)

    results = svc.score_candidates([low, high], QueryPreset.KNOWLEDGE)
    low_r = next(r for r in results if r.uuid == "low")
    high_r = next(r for r in results if r.uuid == "high")

    # The breakdown still reports the computed prominence values
    assert high_r.breakdown.prominence_bonus > low_r.breakdown.prominence_bonus
    assert abs(high_r.breakdown.prominence_bonus - 1.0) < 1e-9
    assert high_r.breakdown.gamma == 0.1
    assert high_r.final_score > low_r.final_score


@pytest.mark.unit
def test_adjacency_bonus_uses_edge_weights() -> None:
    svc = ScoringService()
    connected = _make_candidate(uuid="conn", adjacency_score=0.8, edge_count=1)
    isolated = _make_candidate(uuid="iso", adjacency_score=0.0, edge_count=1)

    results = svc.score_candidates([connected, isolated], QueryPreset.CONNECTED)
    conn_r = next(r for r in results if r.uuid == "conn")
    iso_r = next(r for r in results if r.uuid == "iso")

    assert conn_r.breakdown.adjacency_bonus > iso_r.breakdown.adjacency_bonus


@pytest.mark.unit
def test_scoring_is_deterministic() -> None:
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="a", similarity=0.7, edge_count=3),
        _make_candidate(uuid="b", similarity=0.5, edge_count=8),
    ]

    r1 = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)
    r2 = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)

    assert [r.final_score for r in r1] == [r.final_score for r in r2]
    assert [r.uuid for r in r1] == [r.uuid for r in r2]


@pytest.mark.unit
def test_all_presets_produce_valid_scores() -> None:
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="a", similarity=0.6, edge_count=5, adjacency_score=0.3),
    ]

    for preset in QueryPreset:
        results = svc.score_candidates(candidates, preset)
        assert len(results) == 1
        assert results[0].final_score > 0
        assert results[0].breakdown.preset == preset.value


@pytest.mark.unit
def test_empty_candidates_returns_empty() -> None:
    svc = ScoringService()
    results = svc.score_candidates([], QueryPreset.KNOWLEDGE)
    assert results == []


@pytest.mark.unit
def test_adjacency_clamped_to_one() -> None:
    svc = ScoringService()
    candidates = [_make_candidate(adjacency_score=5.0, edge_count=1)]
    results = svc.score_candidates(candidates, QueryPreset.CONNECTED)
    assert results[0].breakdown.adjacency_bonus == 1.0


@pytest.mark.unit
def test_min_similarity_threshold_filters_noise() -> None:
    """Candidates below the minimum similarity threshold should be excluded."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="good", similarity=0.5),
        _make_candidate(uuid="noise", similarity=0.05),
        _make_candidate(uuid="borderline", similarity=0.15),
    ]
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)
    uuids = [r.uuid for r in results]
    assert "good" in uuids
    assert "borderline" in uuids
    assert "noise" not in uuids


@pytest.mark.unit
def test_floor_is_source_aware_for_non_vector_candidates() -> None:
    """BM25/pending/file-linked candidates below the floor survive; vector does not."""
    from menhir.domain.retrieval_tuning import CandidateSource

    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="vec_low", similarity=0.05, source=CandidateSource.VECTOR),
        _make_candidate(uuid="bm25_low", similarity=0.05, source=CandidateSource.BM25),
        _make_candidate(uuid="pending_low", similarity=0.0, source=CandidateSource.PENDING),
        _make_candidate(uuid="file_low", similarity=0.0, source=CandidateSource.FILE_LINKED),
    ]
    uuids = [r.uuid for r in svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)]
    assert "vec_low" not in uuids  # vector below floor -> dropped
    assert "bm25_low" in uuids  # exact lexical hit -> kept despite low similarity
    assert "pending_low" in uuids
    assert "file_low" in uuids


@pytest.mark.unit
def test_floor_still_gates_vector_candidates() -> None:
    """A VECTOR candidate below the floor is still excluded (no regression)."""
    from menhir.domain.retrieval_tuning import CandidateSource

    svc = ScoringService()
    candidates = [_make_candidate(uuid="v", similarity=0.05, source=CandidateSource.VECTOR)]
    assert svc.score_candidates(candidates, QueryPreset.KNOWLEDGE) == []


@pytest.mark.unit
def test_min_similarity_threshold_customizable() -> None:
    """The threshold can be overridden per-call."""
    svc = ScoringService()
    candidates = [_make_candidate(uuid="low", similarity=0.2)]
    # With default threshold (0.15), this candidate passes
    assert len(svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)) == 1
    # With a higher threshold, it's filtered
    assert len(svc.score_candidates(candidates, QueryPreset.KNOWLEDGE, min_similarity=0.3)) == 0


@pytest.mark.unit
def test_all_below_threshold_returns_empty() -> None:
    """If all candidates are below threshold, scoring returns empty list."""
    svc = ScoringService()
    candidates = [
        _make_candidate(uuid="a", similarity=0.01),
        _make_candidate(uuid="b", similarity=0.05),
    ]
    results = svc.score_candidates(candidates, QueryPreset.KNOWLEDGE)
    assert results == []
