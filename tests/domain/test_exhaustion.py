"""Tests for the retrieval exhaustion policy (R3 rung E).

Pins the load-bearing rule: penalize repeated UNPRODUCTIVE retrieval
(retrievals_since_progress), never raw retrieval count, and never exempt memories."""

from __future__ import annotations

from menhir.domain.exhaustion import (
    ExemptReason,
    ExhaustionDecision,
    RetrievalStats,
    exhaustion_decision,
    exhaustion_factor,
)


def test_allow_when_below_attenuate_threshold() -> None:
    stats = RetrievalStats(session_retrieval_count=1, retrievals_since_progress=1)
    assert exhaustion_decision(stats) is ExhaustionDecision.ALLOW
    assert exhaustion_factor(stats) == 1.0


def test_attenuate_then_suppress_on_unproductive_repeats() -> None:
    assert exhaustion_decision(RetrievalStats(retrievals_since_progress=2)) is ExhaustionDecision.ATTENUATE
    assert exhaustion_decision(RetrievalStats(retrievals_since_progress=4)) is ExhaustionDecision.SUPPRESS
    assert 0.0 < exhaustion_factor(RetrievalStats(retrievals_since_progress=2)) < 1.0
    assert exhaustion_factor(RetrievalStats(retrievals_since_progress=4)) == 0.0


def test_raw_retrieval_count_never_penalizes_by_itself() -> None:
    """A heavily-retrieved memory that keeps producing progress (counter reset) is ALLOW.

    This is the whole point: productive recency must not be punished."""
    stats = RetrievalStats(session_retrieval_count=50, retrievals_since_progress=0)
    assert exhaustion_decision(stats) is ExhaustionDecision.ALLOW
    assert exhaustion_factor(stats) == 1.0


def test_exempt_memory_never_penalized_even_when_looping() -> None:
    for reason in ExemptReason:
        stats = RetrievalStats(retrievals_since_progress=99, exempt_reason=reason)
        assert exhaustion_decision(stats) is ExhaustionDecision.ALLOW
        assert exhaustion_factor(stats) == 1.0


def test_thresholds_are_tunable() -> None:
    stats = RetrievalStats(retrievals_since_progress=2)
    # raise the bar: 2 no longer attenuates
    assert exhaustion_decision(stats, attenuate_at=3, suppress_at=5) is ExhaustionDecision.ALLOW


def test_factor_clamped() -> None:
    stats = RetrievalStats(retrievals_since_progress=2)
    assert exhaustion_factor(stats, attenuation=5.0) == 1.0
    assert exhaustion_factor(stats, attenuation=-1.0) == 0.0
