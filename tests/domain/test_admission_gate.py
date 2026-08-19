"""Unit tests for admission gate — user-tier claim verification."""

import pytest

from menhir.domain.truth.admission_gate import (
    AdmissionVerdict,
    evaluate_user_tier_claim,
)


class TestAdmissionGate:
    """Test cases for user-tier admission gating."""

    def test_passthrough_non_apex_source(self):
        """Non-user/manual sources pass through unchanged."""
        verdict = evaluate_user_tier_claim(
            requested_source="agent_inference",
            turn_evidence=None,
            claimed_text="some fact",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is True
        assert verdict.effective_source == "agent_inference"
        assert verdict.reason == "passthrough (not user/manual)"

    def test_deny_no_turn_evidence(self):
        """Missing turn evidence downgrades to agent_inference."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence=None,
            claimed_text="some fact",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "no turn_evidence_uuid" in verdict.reason

    def test_deny_wrong_role(self):
        """Non-user role denies the claim."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "assistant",
                "declarant": "assistant",
                "text": "I said something",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I said something",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "role is 'assistant'" in verdict.reason

    def test_deny_session_mismatch(self):
        """Explicit session mismatch denies the claim."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "some fact",
                "session_id": "sess_other",
                "namespace": "ns1",
            },
            claimed_text="some fact",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "session mismatch" in verdict.reason

    def test_deny_namespace_mismatch(self):
        """Explicit namespace mismatch denies the claim."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "some fact",
                "session_id": "sess1",
                "namespace": "ns_other",
            },
            claimed_text="some fact",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "namespace mismatch" in verdict.reason

    def test_tolerate_null_session(self):
        """Null sessions on both sides don't cause mismatch."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "some fact",
                "session_id": None,
                "namespace": None,
            },
            claimed_text="some fact",
            session_id=None,
            namespace=None,
        )
        assert verdict.granted is True
        assert verdict.effective_source == "user"

    def test_deny_ungrounded_text(self):
        """Text not present in turn denies the claim."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I bought a bicycle",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I purchased a motorcycle",  # Different from source
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "not grounded" in verdict.reason

    def test_grant_exact_match(self):
        """Exact text match grants the claim."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I bought a bicycle",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I bought a bicycle",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is True
        assert verdict.effective_source == "user"
        assert verdict.reason == "grounded"
        assert verdict.turn_evidence_uuid == "turn123"

    def test_substring_no_longer_grants(self):
        """CF-17: a substring of the prompt no longer grants apex tier.

        This test previously asserted the opposite, and it was one of the two places the defect
        was written down as intended behaviour. Containment cannot tell an assertion from a
        mention -- the same branch that grounded "I bought a bicycle" here also grounded
        "the deploy failed" against "Alice claimed the deploy failed."

        The claim is denied because it is not the whole turn and not a whole sentence of it. A
        memory that IS a whole sentence still grounds; see tests/domain/test_assertion_spans.py.
        """
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "Today I bought a bicycle for $200 at the store",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I bought a bicycle",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"

    def test_token_overlap_no_longer_grants(self):
        """CF-17: token overlap no longer grants. This test formerly asserted that it did."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I purchased a new bicycle yesterday",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I bought a bicycle",
            session_id="sess1",
            namespace="ns1",
        )
        # CF-17: this is the branch that admitted every contradiction. The arithmetic recorded
        # below is correct and is exactly the problem -- for a claim of N retained tokens with one
        # substituted, overlap = N-1 >= 0.5N for all N >= 2, so "the deploy failed" grounded
        # against "the deploy succeeded" by the same rule that grounds this paraphrase.
        #
        # Denying paraphrase is the deliberate cost. No offline deterministic test can tell
        # "purchased" from "bought" without also admitting "succeeded" for "failed", so a
        # paraphrased memory is stored at agent_inference (0.5), which describes it accurately.
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"

    def test_manual_source_same_logic(self):
        """'manual' source uses same gating logic as 'user'."""
        verdict = evaluate_user_tier_claim(
            requested_source="manual",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I made a note",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I made a note",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is True
        assert verdict.effective_source == "manual"

    def test_case_insensitive_grounding(self):
        """Text grounding is case-insensitive."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I BOUGHT A BICYCLE",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="i bought a bicycle",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is True

    def test_whitespace_normalization(self):
        """Extra whitespace doesn't prevent grounding."""
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence={
                "turn_id": "turn123",
                "role": "user",
                "declarant": "user",
                "text": "I   bought\n\ta  bicycle",
                "session_id": "sess1",
                "namespace": "ns1",
            },
            claimed_text="I bought a bicycle",
            session_id="sess1",
            namespace="ns1",
        )
        assert verdict.granted is True

    def test_verdict_frozen(self):
        """AdmissionVerdict is frozen (immutable)."""
        verdict = AdmissionVerdict(
            granted=True,
            effective_source="user",
            reason="test",
            turn_evidence_uuid="turn1",
        )
        with pytest.raises(AttributeError):
            verdict.granted = False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
