"""Unit tests for N-lane weighted RRF fusion and source priors."""

from __future__ import annotations

import pytest

from menhir.domain.retrieval_tuning import (
    FLOOR_EXEMPT_SOURCES,
    SOURCE_PRIORS,
    CandidateSource,
)


# --- Source priors ---------------------------------------------------------


@pytest.mark.unit
def test_source_priors_and_floor_exemption_are_consistent() -> None:
    # vector/bm25 carry no baseline prior (their score is real); injected sources do.
    assert SOURCE_PRIORS[CandidateSource.VECTOR] == 0.0
    assert SOURCE_PRIORS[CandidateSource.BM25] == 0.0
    assert SOURCE_PRIORS[CandidateSource.PENDING] == 1.0
    assert SOURCE_PRIORS[CandidateSource.FILE_LINKED] == 0.3
    # vector is the only source subject to the cosine floor.
    assert CandidateSource.VECTOR not in FLOOR_EXEMPT_SOURCES
    assert CandidateSource.BM25 in FLOOR_EXEMPT_SOURCES


# --- weighted_rrf_multi: N-lane fusion (content-vector lane) ----------------

from menhir.services.hybrid_retrieval import (  # noqa: E402
    ADMISSION_ATTRIBUTED,
    FusionLane,
    weighted_rrf_multi,
)


@pytest.mark.unit
def test_multi_records_contributing_sources_and_single_admission() -> None:
    vec = [("shared", "shared"), ("v1", "v1")]
    content = [("shared", "shared"), ("c1", "c1")]
    out = {c.uuid: c for c in weighted_rrf_multi([
        FusionLane(CandidateSource.VECTOR, vec),
        FusionLane(CandidateSource.CONTENT_VECTOR, content),
    ])}
    # 'shared' found by both semantic lanes -> both contributing, admitted VECTOR
    assert out["shared"].contributing_sources == frozenset(
        {CandidateSource.VECTOR, CandidateSource.CONTENT_VECTOR}
    )
    assert out["shared"].source == CandidateSource.VECTOR  # VECTOR precedes CONTENT_VECTOR
    # The production-fused policy is rank-affecting and labels every fused hit VECTOR.
    assert out["c1"].source == CandidateSource.VECTOR
    assert out["c1"].contributing_sources == frozenset({CandidateSource.CONTENT_VECTOR})


@pytest.mark.unit
def test_multi_bm25_contribution_does_not_change_production_admission() -> None:
    """Lane provenance must not grant floor exemption in the production-parity arms."""
    bm = [("x", "x")]
    content = [("x", "x")]
    out = {c.uuid: c for c in weighted_rrf_multi([
        FusionLane(CandidateSource.BM25, bm),
        FusionLane(CandidateSource.CONTENT_VECTOR, content),
    ])}
    assert out["x"].source == CandidateSource.VECTOR
    assert out["x"].contributing_sources == frozenset(
        {CandidateSource.BM25, CandidateSource.CONTENT_VECTOR}
    )


@pytest.mark.unit
def test_multi_attributed_policy_retains_existing_bm25_admission() -> None:
    out = weighted_rrf_multi(
        [
            FusionLane(CandidateSource.BM25, [("x", "x")]),
            FusionLane(CandidateSource.CONTENT_VECTOR, [("x", "x")]),
        ],
        admission_policy=ADMISSION_ATTRIBUTED,
    )
    assert out[0].source == CandidateSource.BM25


@pytest.mark.unit
def test_two_lane_order_matches_graphiti_production_rrf() -> None:
    from graphiti_core.search.search_utils import rrf as graphiti_rrf

    vector = [("single_top", "single_top"), *[(f"v{i}", f"v{i}") for i in range(1, 6)], ("both", "both")]
    bm25 = [(f"b{i}", f"b{i}") for i in range(6)] + [("both", "both")]
    expected, expected_scores = graphiti_rrf(
        [[uuid for uuid, _name in bm25], [uuid for uuid, _name in vector]]
    )
    actual = weighted_rrf_multi(
        [
            FusionLane(CandidateSource.BM25, bm25, weight=0.5),
            FusionLane(CandidateSource.VECTOR, vector, weight=0.5),
        ],
        admission_policy=ADMISSION_ATTRIBUTED,
    )
    assert [candidate.uuid for candidate in actual] == expected
    assert [candidate.similarity for candidate in actual] == expected_scores


@pytest.mark.unit
def test_multi_three_lane_uses_fixed_production_ceiling() -> None:
    """A third lane cannot increase the fixed dual-method production ceiling."""
    vec = [("a", "a"), ("b", "b")]
    bm = [("a", "a"), ("c", "c")]
    content = [("a", "a"), ("d", "d")]
    out = weighted_rrf_multi([
        FusionLane(CandidateSource.VECTOR, vec),
        FusionLane(CandidateSource.BM25, bm),
        FusionLane(CandidateSource.CONTENT_VECTOR, content),
    ])
    assert out[0].similarity == 2.0
    assert out[0].uuid == "a"  # found by all three lanes -> highest fused score
    assert all(0.0 <= c.similarity <= 2.0 for c in out)


@pytest.mark.unit
def test_multi_empty_and_limit() -> None:
    assert weighted_rrf_multi([]) == []
    assert weighted_rrf_multi([FusionLane(CandidateSource.VECTOR, [])]) == []
    many = [FusionLane(CandidateSource.VECTOR, [(f"v{i}", f"v{i}") for i in range(10)])]
    assert len(weighted_rrf_multi(many, limit=3)) == 3


@pytest.mark.unit
def test_multi_is_deterministic() -> None:
    lanes = [
        FusionLane(CandidateSource.VECTOR, [("a", "a"), ("b", "b")]),
        FusionLane(CandidateSource.CONTENT_VECTOR, [("b", "b"), ("c", "c")]),
    ]
    first = [c.uuid for c in weighted_rrf_multi(lanes)]
    second = [c.uuid for c in weighted_rrf_multi(lanes)]
    assert first == second
