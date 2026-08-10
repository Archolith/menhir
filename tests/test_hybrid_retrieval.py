"""Unit tests for hybrid candidate generation (weighted RRF) and tuning config."""

from __future__ import annotations

import pytest

from menhir.domain.retrieval_tuning import (
    FLOOR_EXEMPT_SOURCES,
    SOURCE_PRIORS,
    CandidateSource,
    RetrievalTuningConfig,
)
from menhir.services.hybrid_retrieval import HybridCandidate, hybrid_search, weighted_rrf


# --- RetrievalTuningConfig -------------------------------------------------


@pytest.mark.unit
def test_tuning_config_defaults_reproduce_legacy_behavior() -> None:
    cfg = RetrievalTuningConfig()
    assert cfg.enable_bm25 is False  # default path = single fused search_scored
    assert cfg.hybrid_alpha == 0.5
    # Plan 1b staged behind a default-off flag: "rrf" == today's scale byte-for-byte.
    assert cfg.similarity_scale == "rrf"


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
def test_tuning_config_rejects_out_of_range_alpha(bad: float) -> None:
    with pytest.raises(ValueError):
        RetrievalTuningConfig(hybrid_alpha=bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "cosine", "RRF", "norm"])
def test_tuning_config_rejects_unknown_similarity_scale(bad: str) -> None:
    with pytest.raises(ValueError):
        RetrievalTuningConfig(similarity_scale=bad)


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


# --- weighted_rrf ----------------------------------------------------------


@pytest.mark.unit
def test_weighted_rrf_empty_inputs() -> None:
    assert weighted_rrf([], [], alpha=0.5) == []


@pytest.mark.unit
def test_weighted_rrf_symmetric_uses_production_ceiling_and_attributes() -> None:
    vec = [("v1", "v1"), ("x", "x"), ("v2", "v2")]
    bm = [("b1", "b1"), ("x", "x")]
    out = weighted_rrf(vec, bm, alpha=0.5)

    by_uuid = {c.uuid: c for c in out}
    assert out[0].similarity == 1.0
    # 'x' is in both passes -> attributed BM25 (floor-exempt). Under production's
    # rank_const=1 it ties both single-lane leaders; Graphiti processes BM25 first.
    assert by_uuid["x"].source == CandidateSource.BM25
    assert out[0].uuid == "b1"
    # bm25-only and vector-only keep their attribution
    assert by_uuid["b1"].source == CandidateSource.BM25
    assert by_uuid["v1"].source == CandidateSource.VECTOR


@pytest.mark.unit
def test_weighted_rrf_alpha_one_is_vector_only() -> None:
    vec = [("v1", "v1")]
    bm = [("b1", "b1")]
    out = {c.uuid: c for c in weighted_rrf(vec, bm, alpha=1.0)}
    # bm25-only candidate gets zero weight at alpha=1.0
    assert out["b1"].similarity == 0.0
    assert out["v1"].similarity == 2.0


@pytest.mark.unit
def test_weighted_rrf_alpha_zero_is_bm25_only() -> None:
    vec = [("v1", "v1")]
    bm = [("b1", "b1")]
    out = {c.uuid: c for c in weighted_rrf(vec, bm, alpha=0.0)}
    assert out["v1"].similarity == 0.0
    assert out["b1"].similarity == 2.0


@pytest.mark.unit
def test_weighted_rrf_is_deterministic() -> None:
    vec = [("a", "a"), ("b", "b"), ("c", "c")]
    bm = [("c", "c"), ("d", "d")]
    first = [c.uuid for c in weighted_rrf(vec, bm, alpha=0.5)]
    second = [c.uuid for c in weighted_rrf(vec, bm, alpha=0.5)]
    assert first == second


@pytest.mark.unit
def test_weighted_rrf_respects_limit() -> None:
    vec = [(f"v{i}", f"v{i}") for i in range(10)]
    out = weighted_rrf(vec, [], alpha=1.0, limit=3)
    assert len(out) == 3


# --- hybrid_search (async, against the stub graphiti client) ---------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hybrid_search_runs_both_passes_and_fuses(stub_graphiti_client) -> None:
    stub_graphiti_client.ranked_by_method_results = {
        "cosine_similarity": [("v1", "v1"), ("shared", "shared")],
        "bm25": [("shared", "shared"), ("b1", "b1")],
    }
    out = await hybrid_search(
        stub_graphiti_client, "q", config=RetrievalTuningConfig(enable_bm25=True, hybrid_alpha=0.5)
    )
    assert all(isinstance(c, HybridCandidate) for c in out)
    methods = stub_graphiti_client.ranked_by_method_calls[0]["methods"]
    assert set(methods) == {"cosine_similarity", "bm25"}
    assert {c.uuid for c in out} == {"v1", "shared", "b1"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hybrid_search_skips_unused_pass_at_pure_alpha(stub_graphiti_client) -> None:
    stub_graphiti_client.ranked_by_method_results = {"bm25": [("b1", "b1")]}
    await hybrid_search(
        stub_graphiti_client, "q", config=RetrievalTuningConfig(enable_bm25=True, hybrid_alpha=0.0)
    )
    # alpha=0 -> bm25 only; cosine pass not requested
    assert stub_graphiti_client.ranked_by_method_calls[0]["methods"] == ["bm25"]


# --- weighted_rrf_multi: N-lane fusion (content-vector lane) ----------------

from menhir.services.hybrid_retrieval import (  # noqa: E402
    ADMISSION_ATTRIBUTED,
    FusionLane,
    weighted_rrf_multi,
)


@pytest.mark.unit
def test_multi_two_lanes_reproduces_weighted_rrf_exactly() -> None:
    """Load-bearing parity: the 2-lane multi form == the legacy weighted_rrf, so the
    3-lane path cannot silently diverge from the control that must match production."""
    vec = [("v1", "v1"), ("shared", "shared"), ("v2", "v2")]
    bm = [("shared", "shared"), ("b1", "b1")]
    for alpha in (0.0, 0.25, 0.5, 0.75, 1.0):
        legacy = weighted_rrf(vec, bm, alpha=alpha)
        multi = weighted_rrf_multi(
            [
                FusionLane(CandidateSource.VECTOR, vec, weight=alpha),
                FusionLane(CandidateSource.BM25, bm, weight=1.0 - alpha),
            ],
            admission_policy=ADMISSION_ATTRIBUTED,
        )
        assert [(c.uuid, c.similarity, c.source) for c in legacy] == [
            (c.uuid, c.similarity, c.source) for c in multi
        ]


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
    actual = weighted_rrf(vector, bm25, alpha=0.5)
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
