from __future__ import annotations

import pytest

from menhir.domain.admission import (
    AdmissionPolicyDecision,
    AdmissionRequest,
    OrderedAuthoritySemantics,
    SourceAuthorityGrant,
    SourceProvenance,
    decide_admission,
)


class PolicyWithBogusComparator:
    """Policy may propose a ceiling; it does not own the core comparison primitive."""

    def __init__(self) -> None:
        self.compare_calls = 0

    def compare(self, left: str, right: str):
        self.compare_calls += 1
        raise AssertionError("admission must not use a policy-provided comparator")

    def evaluate(self, *, request, source, grant):
        return AdmissionPolicyDecision(
            accepted=True,
            policy_id="domain-policy",
            policy_version="v1",
            authority_ceiling="high",
        )


@pytest.mark.unit
def test_policy_cannot_replace_trusted_authority_semantics():
    policy = PolicyWithBogusComparator()
    decision = decide_admission(
        request=AdmissionRequest("high", "current_fact"),
        source=SourceProvenance("source-1", "captured_record"),
        grant=SourceAuthorityGrant(
            grant_id="grant-1",
            source_id="source-1",
            source_kind="captured_record",
            provenance_class="machine_observation",
            authority_ceiling="low",
        ),
        policy=policy,
        semantics=OrderedAuthoritySemantics(("low", "high")),
    )

    assert decision.effective_authority == "low"
    assert decision.requested_promotion is True
    assert decision.policy_attempted_promotion is True
    assert policy.compare_calls == 0
