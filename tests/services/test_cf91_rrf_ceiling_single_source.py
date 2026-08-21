"""CF-91: the dual-method RRF ceiling has a single source of truth.

The same quantity -- the ceiling a dual-method RRF score can reach -- was
declared independently in ``hybrid_retrieval.RRF_COMMON_CEILING`` and
``scoring_service.GRAPHITI_RRF_DUAL_METHOD_MAX``. They agreed at 2.0, but
nothing kept them agreeing. Consolidation deletes the local copy and points the
hybrid call site at the established survivor.

The regression to prevent is a SECOND declaration reappearing, so these tests
are structural: the module must not re-declare its own ceiling, and the call
site must actually read the surviving constant (not a leftover local copy).
"""

from __future__ import annotations

import pytest

from menhir.services import hybrid_retrieval
from menhir.services.hybrid_retrieval import (
    CandidateSource,
    FusionLane,
    weighted_rrf_multi,
)

pytestmark = pytest.mark.unit


def _two_lane_fixture() -> list[FusionLane]:
    return [
        FusionLane(
            CandidateSource.VECTOR, [("a", "A"), ("b", "B")], weight=1.0
        ),
        FusionLane(
            CandidateSource.BM25, [("b", "B"), ("c", "C")], weight=1.0
        ),
    ]


def test_no_local_ceiling_declared() -> None:
    """The module must not define its own ceiling constant anymore."""
    assert "RRF_COMMON_CEILING" not in vars(hybrid_retrieval)


def test_call_site_reads_surviving_constant(monkeypatch) -> None:
    """Load-bearing: the scale at the call site follows the survivor's value.

    If the call site had a leftover local copy instead of reading the imported
    survivor, monkeypatching the imported name would have no effect.
    """
    lanes = _two_lane_fixture()
    active_weight = sum(max(0.0, lane.weight) for lane in lanes)

    monkeypatch.setattr(hybrid_retrieval, "GRAPHITI_RRF_DUAL_METHOD_MAX", 4.0)

    scaled = weighted_rrf_multi(lanes)
    by_uuid = {c.uuid: c.similarity for c in scaled}

    # With ceiling 4.0 and active_weight 2.0, scale = 4.0 / 2.0 = 2.0.
    # candidate "b" fused = 1.0 (vector rank0) + 0.5 (bm25 rank1) = 1.5.
    assert by_uuid["b"] == pytest.approx(2.0 * 1.5)
    assert active_weight == pytest.approx(2.0)


def test_scaling_unchanged_from_today() -> None:
    """POSITIVE CONTROL: with the real ceiling the result is unchanged.

    Computed by hand from the formula at the call site
    ``scale = 2.0 / active_weight`` with active_weight = 2.0 (two lanes of
    weight 1.0), so scale = 1.0. Candidate "b" is rank 0 in the vector lane
    and rank 1 in the bm25 lane: fused = 1/1 + 1/2 = 1.5, similarity = 1.5.
    Candidate "a" is rank 0 in one lane only: fused = 1.0. Candidate "c" is
    rank 1 in one lane: fused = 0.5.
    """
    scaled = weighted_rrf_multi(_two_lane_fixture())
    by_uuid = {c.uuid: c.similarity for c in scaled}

    assert by_uuid["a"] == pytest.approx(1.0)
    assert by_uuid["b"] == pytest.approx(1.5)
    assert by_uuid["c"] == pytest.approx(0.5)
    assert list(by_uuid) == ["b", "a", "c"]
