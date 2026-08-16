from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from menhir.domain.admission import (
    AdmissionPolicyDecision,
    AdmissionRequest,
    AdmissionStatus,
    OrderedAuthoritySemantics,
    SourceAuthorityGrant,
    SourceProvenance,
    decide_admission,
)
from menhir.domain.typed_assertion import EVIDENCE_TIERS, evidence_rank
from menhir.infrastructure.admission_source import load_turn_evidence_admission_source


class StaticPolicy:
    def __init__(self, decision: AdmissionPolicyDecision) -> None:
        self.decision = decision
        self.calls = 0

    def evaluate(self, *, request, source, grant):
        self.calls += 1
        return self.decision


class FakeNeo4j:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        return self.rows


def _grant(
    *,
    source_id: str = "turn-1",
    source_kind: str = "turn_evidence",
    provenance_class: str = "model_inference",
    ceiling: str = "observed",
) -> SourceAuthorityGrant:
    return SourceAuthorityGrant(
        grant_id="grant-1",
        source_id=source_id,
        source_kind=source_kind,
        provenance_class=provenance_class,
        authority_ceiling=ceiling,
    )


def _allow(*, ceiling: str | None = None, reason: str = "allowed") -> StaticPolicy:
    return StaticPolicy(
        AdmissionPolicyDecision(
            accepted=True,
            policy_id="policy",
            policy_version="v1",
            authority_ceiling=ceiling,
            reason=reason,
        )
    )


@pytest.mark.unit
def test_requested_authority_is_untrusted_and_clamped_to_core_grant():
    semantics = OrderedAuthoritySemantics(("observed", "reviewed", "operator"))
    decision = decide_admission(
        request=AdmissionRequest(
            requested_authority="operator",
            purpose="current_fact",
        ),
        source=SourceProvenance("turn-1", "turn_evidence"),
        grant=_grant(ceiling="observed"),
        policy=_allow(),
        semantics=semantics,
    )

    assert decision.status is AdmissionStatus.ADMITTED
    assert decision.effective_authority == "observed"
    assert decision.requested_authority == "operator"
    assert decision.requested_promotion is True
    assert decision.admission_ceiling == "observed"


@pytest.mark.unit
def test_source_grant_binding_is_exact_and_rejects_before_policy():
    policy = _allow()
    decision = decide_admission(
        request=AdmissionRequest("reviewed", "current_fact"),
        source=SourceProvenance("turn-OTHER", "turn_evidence"),
        grant=_grant(ceiling="reviewed"),
        policy=policy,
        semantics=OrderedAuthoritySemantics(("observed", "reviewed")),
    )

    assert decision.status is AdmissionStatus.REJECTED
    assert decision.effective_authority is None
    assert "does not match" in decision.reason
    assert policy.calls == 0


@pytest.mark.unit
def test_investigation_policy_can_lower_official_record_for_beneficial_owner():
    # Investigation owns these labels. Core admission.py contains none of them.
    semantics = OrderedAuthoritySemantics(
        (
            "model_inference",
            "anonymous_tip",
            "media_report",
            "firsthand_statement",
            "official_record",
        )
    )
    decision = decide_admission(
        request=AdmissionRequest(
            requested_authority="official_record",
            purpose="beneficial_owner",
        ),
        source=SourceProvenance("deed-17", "county_record"),
        grant=_grant(
            source_id="deed-17",
            source_kind="county_record",
            provenance_class="recorded_instrument",
            ceiling="official_record",
        ),
        policy=_allow(
            ceiling="media_report",
            reason="record proves title, not beneficial ownership",
        ),
        semantics=semantics,
    )

    assert decision.admitted is True
    assert decision.effective_authority == "media_report"
    assert decision.policy_ceiling == "media_report"
    assert decision.policy_attempted_promotion is False
    assert decision.provenance_class == "recorded_instrument"


@pytest.mark.unit
def test_personality_policy_uses_same_contract_with_different_authority_vocabulary():
    # Personality deliberately uses different semantics from investigation.
    semantics = OrderedAuthoritySemantics(
        ("model_guess", "third_party_statement", "explicit_user_preference")
    )
    decision = decide_admission(
        request=AdmissionRequest(
            requested_authority="explicit_user_preference",
            purpose="own_preference",
        ),
        source=SourceProvenance("turn-pref", "conversation_turn"),
        grant=_grant(
            source_id="turn-pref",
            source_kind="conversation_turn",
            provenance_class="explicit_user_statement",
            ceiling="explicit_user_preference",
        ),
        policy=_allow(),
        semantics=semantics,
    )

    assert decision.admitted is True
    assert decision.effective_authority == "explicit_user_preference"
    assert decision.requested_promotion is False


@pytest.mark.unit
def test_policy_cannot_raise_above_core_admission_ceiling():
    semantics = OrderedAuthoritySemantics(
        ("model_inference", "anonymous_tip", "official_record")
    )
    decision = decide_admission(
        request=AdmissionRequest("anonymous_tip", "ownership_hypothesis"),
        source=SourceProvenance("tip-1", "intake"),
        grant=_grant(
            source_id="tip-1",
            source_kind="intake",
            provenance_class="anonymous_statement",
            ceiling="anonymous_tip",
        ),
        policy=_allow(ceiling="official_record"),
        semantics=semantics,
    )

    assert decision.admitted is True
    assert decision.effective_authority == "anonymous_tip"
    assert decision.policy_attempted_promotion is True


@pytest.mark.unit
def test_policy_rejection_produces_no_effective_authority():
    policy = StaticPolicy(
        AdmissionPolicyDecision(
            accepted=False,
            policy_id="investigation",
            policy_version="v2",
            reason="source class is not admissible for this claim",
        )
    )
    decision = decide_admission(
        request=AdmissionRequest("reviewed", "ownership_hypothesis"),
        source=SourceProvenance("tip-2", "intake"),
        grant=_grant(
            source_id="tip-2",
            source_kind="intake",
            provenance_class="anonymous_statement",
            ceiling="reviewed",
        ),
        policy=policy,
        semantics=OrderedAuthoritySemantics(("observed", "reviewed")),
    )

    assert decision.status is AdmissionStatus.REJECTED
    assert decision.effective_authority is None
    assert decision.policy_id == "investigation"
    assert decision.policy_version == "v2"


@pytest.mark.unit
def test_unknown_authority_fails_closed_as_incomparable():
    decision = decide_admission(
        request=AdmissionRequest("forged_superuser", "current_fact"),
        source=SourceProvenance("turn-1", "turn_evidence"),
        grant=_grant(ceiling="reviewed"),
        policy=_allow(),
        semantics=OrderedAuthoritySemantics(("observed", "reviewed")),
    )

    assert decision.status is AdmissionStatus.REJECTED
    assert decision.effective_authority is None
    assert "incomparable" in decision.reason


@pytest.mark.unit
def test_legacy_scalar_order_is_consumed_without_moving_it_into_generic_core():
    assert EVIDENCE_TIERS == ("agent", "trusted_tool", "manual", "user")
    assert [evidence_rank(tier) for tier in EVIDENCE_TIERS] == [0, 1, 2, 3]

    decision = decide_admission(
        request=AdmissionRequest("user", "scalar_state"),
        source=SourceProvenance("turn-scalar", "turn_evidence"),
        grant=_grant(
            source_id="turn-scalar",
            source_kind="turn_evidence",
            provenance_class="model_inference",
            ceiling="agent",
        ),
        policy=_allow(),
        semantics=OrderedAuthoritySemantics(tuple(EVIDENCE_TIERS)),
    )

    assert decision.admitted is True
    assert decision.effective_authority == "agent"
    assert decision.requested_promotion is True


@pytest.mark.unit
def test_admission_receipt_is_immutable():
    decision = decide_admission(
        request=AdmissionRequest("observed", "current_fact"),
        source=SourceProvenance("turn-1", "turn_evidence"),
        grant=_grant(ceiling="observed"),
        policy=_allow(),
        semantics=OrderedAuthoritySemantics(("observed", "reviewed")),
    )

    with pytest.raises(FrozenInstanceError):
        decision.effective_authority = "reviewed"


@pytest.mark.unit
def test_turn_evidence_adapter_binds_to_stored_turn_id_and_omits_content():
    fake = FakeNeo4j(
        [
            {
                "source_id": "turn-42",
                "source_kind": "claude_code_hook",
                "role": "user",
                "declarant": "user",
                "producer_source_id": "producer-7",
                "source_client": "claude_code",
                "hook_version": "hook-v2",
            }
        ]
    )

    source = load_turn_evidence_admission_source(fake, "turn-42")

    assert source is not None
    assert source.provenance == SourceProvenance(
        source_id="turn-42",
        source_kind="claude_code_hook",
    )
    assert source.declarant == "user"
    assert source.producer_source_id == "producer-7"
    query, params = fake.executed[-1]
    assert "t.turn_id AS source_id" in query
    assert "t.source_kind AS source_kind" in query
    assert "t.text" not in query
    assert params == {"turn_id": "turn-42"}


@pytest.mark.unit
def test_turn_evidence_adapter_fails_closed_without_stored_source_kind():
    fake = FakeNeo4j(
        [
            {
                "source_id": "turn-legacy",
                "source_kind": None,
                "role": "user",
                "declarant": "user",
            }
        ]
    )

    assert load_turn_evidence_admission_source(fake, "turn-legacy") is None
    with pytest.raises(ValueError, match="turn_id"):
        load_turn_evidence_admission_source(fake, "  ")
