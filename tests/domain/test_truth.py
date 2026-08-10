"""Unit tests for the domain/truth/ package.

Covers:
- ReviewState values and str-enum contract
- review_state_from_confidence: boundary values, None, edge cases
- WardenLabel values and str-enum contract
- TruthClaim Protocol satisfaction by TruthAttestation
- TruthAttestation factory classmethods
- SOURCE_CONFIDENCE_* constant ordering
- SOURCE_CONFIDENCE thresholds match review_state_from_confidence boundaries
"""

from __future__ import annotations

import pytest

from menhir.domain.belief import RecallBucket
from menhir.domain.truth import (
    ReviewState,
    SOURCE_CONFIDENCE_AGENT,
    SOURCE_CONFIDENCE_AGENT_REVIEWED,
    SOURCE_CONFIDENCE_STRUCTURAL,
    SOURCE_CONFIDENCE_USER,
    TruthAttestation,
    TruthClaim,
    WardenLabel,
    review_state_from_confidence,
)
from menhir.domain.warden import WardenDecision


# ---------------------------------------------------------------------------
# ReviewState
# ---------------------------------------------------------------------------

class TestReviewState:
    def test_values_are_strings(self) -> None:
        for state in ReviewState:
            assert isinstance(state, str), f"{state!r} should be a str"

    def test_str_equality(self) -> None:
        assert ReviewState.HUMAN_REVIEWED == "human_reviewed"
        assert ReviewState.AGENT_REVIEWED == "agent_reviewed"
        assert ReviewState.UNREVIEWED     == "unreviewed"

    def test_has_three_tiers(self) -> None:
        assert len(ReviewState) == 3


# ---------------------------------------------------------------------------
# review_state_from_confidence — threshold coupling verification
# ---------------------------------------------------------------------------

class TestReviewStateFromConfidence:
    """Boundary tests ensure thresholds stay aligned with named constants."""

    def test_none_maps_to_unreviewed(self) -> None:
        assert review_state_from_confidence(None) is ReviewState.UNREVIEWED

    def test_structural_threshold_exact(self) -> None:
        # At exactly the structural boundary → HUMAN_REVIEWED
        assert review_state_from_confidence(SOURCE_CONFIDENCE_STRUCTURAL) is ReviewState.HUMAN_REVIEWED

    def test_user_confidence_maps_to_human_reviewed(self) -> None:
        assert review_state_from_confidence(SOURCE_CONFIDENCE_USER) is ReviewState.HUMAN_REVIEWED

    def test_just_below_structural_maps_to_agent_reviewed(self) -> None:
        # Epsilon below SOURCE_CONFIDENCE_STRUCTURAL → should be AGENT_REVIEWED
        below = SOURCE_CONFIDENCE_STRUCTURAL - 0.01
        assert review_state_from_confidence(below) is ReviewState.AGENT_REVIEWED

    def test_agent_reviewed_threshold_exact(self) -> None:
        # At exactly the AGENT_REVIEWED boundary → AGENT_REVIEWED
        assert review_state_from_confidence(SOURCE_CONFIDENCE_AGENT_REVIEWED) is ReviewState.AGENT_REVIEWED

    def test_just_below_agent_reviewed_maps_to_unreviewed(self) -> None:
        below = SOURCE_CONFIDENCE_AGENT_REVIEWED - 0.01
        assert review_state_from_confidence(below) is ReviewState.UNREVIEWED

    def test_agent_confidence_maps_to_unreviewed(self) -> None:
        assert review_state_from_confidence(SOURCE_CONFIDENCE_AGENT) is ReviewState.UNREVIEWED

    def test_zero_maps_to_unreviewed(self) -> None:
        assert review_state_from_confidence(0.0) is ReviewState.UNREVIEWED

    def test_negative_maps_to_unreviewed(self) -> None:
        assert review_state_from_confidence(-0.1) is ReviewState.UNREVIEWED

    def test_above_one_maps_to_human_reviewed(self) -> None:
        # Values > 1.0 are out-of-range but should not raise
        assert review_state_from_confidence(1.5) is ReviewState.HUMAN_REVIEWED

    def test_constant_ordering(self) -> None:
        """The constants themselves must obey the tier ordering."""
        assert SOURCE_CONFIDENCE_AGENT < SOURCE_CONFIDENCE_AGENT_REVIEWED
        assert SOURCE_CONFIDENCE_AGENT_REVIEWED < SOURCE_CONFIDENCE_STRUCTURAL
        assert SOURCE_CONFIDENCE_STRUCTURAL <= SOURCE_CONFIDENCE_USER


# ---------------------------------------------------------------------------
# WardenLabel
# ---------------------------------------------------------------------------

class TestWardenLabel:
    def test_values_are_strings(self) -> None:
        for label in WardenLabel:
            assert isinstance(label, str)

    def test_str_equality(self) -> None:
        assert WardenLabel.HISTORICAL       == "historical"
        assert WardenLabel.CONFLICT         == "conflict"
        assert WardenLabel.UNCERTAIN        == "uncertain"
        assert WardenLabel.SUSPICIOUS_CHAIN == "suspicious_chain"

    def test_has_four_labels(self) -> None:
        assert len(WardenLabel) == 4

    def test_isinstance_check(self) -> None:
        # A bare string does NOT satisfy the enum; a WardenLabel value does.
        assert isinstance(WardenLabel.CONFLICT, WardenLabel)
        assert not isinstance("conflict", WardenLabel)


# ---------------------------------------------------------------------------
# TruthClaim Protocol
# ---------------------------------------------------------------------------

class TestTruthClaimProtocol:
    def test_attestation_satisfies_protocol(self) -> None:
        ta = TruthAttestation(
            content="fact",
            review_state=ReviewState.HUMAN_REVIEWED,
            source="git",
            source_confidence=SOURCE_CONFIDENCE_STRUCTURAL,
            assertion_policy=RecallBucket.SAFE_TO_ASSERT,
            warden_decision=WardenDecision.ADMIT,
        )
        assert isinstance(ta, TruthClaim)

    def test_plain_object_does_not_satisfy_protocol(self) -> None:
        assert not isinstance(object(), TruthClaim)


# ---------------------------------------------------------------------------
# TruthAttestation
# ---------------------------------------------------------------------------

class TestTruthAttestation:
    def _base(self, **kw) -> TruthAttestation:
        defaults = dict(
            content="test",
            review_state=ReviewState.HUMAN_REVIEWED,
            source="git",
            source_confidence=SOURCE_CONFIDENCE_STRUCTURAL,
            assertion_policy=RecallBucket.SAFE_TO_ASSERT,
            warden_decision=WardenDecision.ADMIT,
        )
        defaults.update(kw)
        return TruthAttestation(**defaults)

    def test_is_current_when_not_superseded(self) -> None:
        assert self._base(is_superseded=False).is_current is True

    def test_not_current_when_superseded(self) -> None:
        assert self._base(is_superseded=True).is_current is False

    def test_is_admitted_for_admit(self) -> None:
        assert self._base(warden_decision=WardenDecision.ADMIT).is_admitted is True

    def test_is_admitted_for_attenuate(self) -> None:
        assert self._base(warden_decision=WardenDecision.ATTENUATE).is_admitted is True

    def test_not_admitted_for_refuse(self) -> None:
        assert self._base(warden_decision=WardenDecision.REFUSE).is_admitted is False

    def test_not_admitted_for_flag(self) -> None:
        assert self._base(warden_decision=WardenDecision.FLAG).is_admitted is False

    def test_label_accepts_warden_label(self) -> None:
        ta = self._base(label=WardenLabel.HISTORICAL)
        assert ta.label is WardenLabel.HISTORICAL
        assert ta.label == "historical"

    def test_label_none_by_default(self) -> None:
        assert self._base().label is None

    def test_from_source_confidence_factory(self) -> None:
        ta = TruthAttestation.from_source_confidence(
            "fact",
            source="user",
            source_confidence=SOURCE_CONFIDENCE_USER,
            assertion_policy=RecallBucket.SAFE_TO_ASSERT,
            warden_decision=WardenDecision.ADMIT,
        )
        assert ta.review_state is ReviewState.HUMAN_REVIEWED
        assert ta.source_confidence == SOURCE_CONFIDENCE_USER

    def test_from_source_confidence_none_defaults_to_agent(self) -> None:
        ta = TruthAttestation.from_source_confidence(
            "fact",
            source="agent",
            source_confidence=None,
            assertion_policy=RecallBucket.SAFE_TO_ASSERT,
            warden_decision=WardenDecision.ADMIT,
        )
        assert ta.review_state is ReviewState.UNREVIEWED
        assert ta.source_confidence == SOURCE_CONFIDENCE_AGENT

    def test_unscored_factory(self) -> None:
        ta = TruthAttestation.unscored("content", source="project-scan", source_confidence=0.9)
        assert ta.review_state is ReviewState.HUMAN_REVIEWED
        assert ta.assertion_policy is RecallBucket.SAFE_TO_ASSERT
        assert ta.warden_decision is WardenDecision.ADMIT

    def test_unscored_factory_none_confidence(self) -> None:
        ta = TruthAttestation.unscored("content", source="llm")
        assert ta.source_confidence == SOURCE_CONFIDENCE_AGENT
        assert ta.review_state is ReviewState.UNREVIEWED

    def test_rationale_defaults_empty(self) -> None:
        assert self._base().rationale == ()

    def test_frozen(self) -> None:
        ta = self._base()
        with pytest.raises((TypeError, AttributeError)):
            ta.content = "changed"  # type: ignore[misc]
