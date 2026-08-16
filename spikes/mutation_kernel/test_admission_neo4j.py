from __future__ import annotations

from dataclasses import dataclass
import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.admission import (
    AdmissionEngine,
    ExtensionAdmission,
    SourceAuthorityGrant,
    make_grant,
)
from spikes.mutation_kernel.admission_policies import (
    INVESTIGATION_SOURCE_CLAIM,
    PERSONALITY_EXPLICIT_PREFERENCE,
    InvestigationAdmissionPolicy,
    PersonalityAdmissionPolicy,
)
from spikes.mutation_kernel.admission_store import Neo4jAdmissionStore
from spikes.mutation_kernel.kernel import EvidenceRef, make_assertion
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.personality import TRAIT_ASSERTION


@dataclass(frozen=True)
class PermissiveUserPolicy:
    policy_id: str = "test.permissive-user"
    version: int = 1

    def evaluate(self, assertion, grants):
        return ExtensionAdmission(
            admitted=True,
            authority_ceiling="user",
            reason="test_policy_requests_maximum_authority",
        )


def _proposal(
    source: EvidenceRef,
    *,
    assertion_type: str,
    value: object,
    authority: str = "user",
    subject_id: str = "subject-1",
    key: str = "claim",
):
    return make_assertion(
        source=source,
        assertion_type=assertion_type,
        subject_id=subject_id,
        scope="scope-1",
        key=key,
        value=value,
        valid_at="2026-08-15T00:00:00Z",
        authority=authority,
        interpreter_version="extractor-v1",
    )


def test_generic_core_ceiling_blocks_self_promotion_even_if_extension_allows_user() -> None:
    source = EvidenceRef("model-event-1", "model_observation")
    proposal = _proposal(
        source,
        assertion_type="test.claim",
        value={"value": "inferred"},
        authority="user",
    )
    grant = make_grant(
        grant_id="grant-model-1",
        evidence=source,
        provenance_class="model_inference",
        authority_ceiling="agent",
    )

    decision = AdmissionEngine().admit(
        proposal,
        grants=(grant,),
        policy=PermissiveUserPolicy(),
    )
    assert decision.status == "admitted"
    assert decision.requested_authority == "user"
    assert decision.ingress_ceiling == "agent"
    assert decision.policy_ceiling == "user"
    assert decision.effective_authority == "agent"
    assert decision.attempted_promotion is True
    assert decision.admitted_assertion is not None
    assert decision.admitted_assertion.authority == "agent"
    assert decision.admitted_assertion.source_key == proposal.source_key
    assert decision.admitted_assertion.assertion_id != proposal.assertion_id
    assert "admission:test.permissive-user@1" in decision.admitted_assertion.interpreter_version


def test_personality_explicit_preference_requires_verified_explicit_admission_path() -> None:
    source = EvidenceRef("user-message-1", "user_message")
    proposal = _proposal(
        source,
        assertion_type=PERSONALITY_EXPLICIT_PREFERENCE,
        value={"preference": "concise"},
        authority="user",
        key="response_style",
    )
    engine = AdmissionEngine()
    policy = PersonalityAdmissionPolicy()

    model_grant = make_grant(
        grant_id="grant-model-from-user-message",
        evidence=source,
        provenance_class="model_inference",
        authority_ceiling="agent",
    )
    rejected = engine.admit(proposal, grants=(model_grant,), policy=policy)
    assert rejected.status == "rejected"
    assert rejected.reason == "explicit_preference_requires_verified_explicit_source"
    assert rejected.admitted_assertion is None

    explicit_grant = make_grant(
        grant_id="grant-grounded-user-message",
        evidence=source,
        provenance_class="grounded_user_statement",
        authority_ceiling="user",
    )
    admitted = engine.admit(proposal, grants=(explicit_grant,), policy=policy)
    assert admitted.status == "admitted"
    assert admitted.effective_authority == "user"
    assert admitted.attempted_promotion is False
    assert admitted.admitted_assertion is not None
    assert admitted.admitted_assertion.authority == "user"
    assert admitted.decision_id != rejected.decision_id


def test_personality_interpretation_is_capped_at_agent_even_from_user_grounded_source() -> None:
    source = EvidenceRef("user-message-trait", "user_message")
    proposal = _proposal(
        source,
        assertion_type=TRAIT_ASSERTION,
        value={"score": 0.8},
        authority="user",
        key="assertiveness",
    )
    grant = make_grant(
        grant_id="trait-user-source",
        evidence=source,
        provenance_class="grounded_user_statement",
        authority_ceiling="user",
    )
    decision = AdmissionEngine().admit(
        proposal,
        grants=(grant,),
        policy=PersonalityAdmissionPolicy(),
    )
    assert decision.status == "admitted"
    assert decision.ingress_ceiling == "user"
    assert decision.policy_ceiling == "agent"
    assert decision.effective_authority == "agent"
    assert decision.attempted_promotion is True


def test_investigation_policy_distinguishes_deed_tip_user_statement_and_manual_note() -> None:
    engine = AdmissionEngine()
    policy = InvestigationAdmissionPolicy()

    cases = (
        ("deed-1", "deed", "trusted_record", "trusted_tool", "user", "trusted_tool"),
        ("tip-1", "anonymous_tip", "untrusted_record", "trusted_tool", "manual", "agent"),
        ("user-claim-1", "user_statement", "grounded_user_statement", "user", "user", "agent"),
        ("note-1", "investigator_note", "operator_manual", "manual", "user", "manual"),
    )
    for source_id, source_kind, provenance, ingress, requested, expected in cases:
        source = EvidenceRef(source_id, source_kind)
        proposal = _proposal(
            source,
            assertion_type=INVESTIGATION_SOURCE_CLAIM,
            value={"claim": source_id},
            authority=requested,
        )
        grant = make_grant(
            grant_id=f"grant-{source_id}",
            evidence=source,
            provenance_class=provenance,
            authority_ceiling=ingress,
        )
        decision = engine.admit(proposal, grants=(grant,), policy=policy)
        assert decision.status == "admitted"
        assert decision.effective_authority == expected


def test_evidence_source_kind_cannot_be_forged_past_core_grant_binding() -> None:
    source = EvidenceRef("record-claimed-as-deed", "deed")
    proposal = _proposal(
        source,
        assertion_type=INVESTIGATION_SOURCE_CLAIM,
        value={"claim": "ownership"},
        authority="trusted_tool",
    )
    forged_mismatch_grant = SourceAuthorityGrant(
        grant_id="grant-actual-anonymous-source",
        source_id=source.source_id,
        verified_source_kind="anonymous_tip",
        provenance_class="untrusted_record",
        authority_ceiling="agent",
    )
    decision = AdmissionEngine().admit(
        proposal,
        grants=(forged_mismatch_grant,),
        policy=InvestigationAdmissionPolicy(),
    )
    assert decision.status == "rejected"
    assert decision.reason == "evidence_grant_mismatch"


def test_multi_source_assertion_uses_weakest_core_and_policy_ceiling() -> None:
    deed = EvidenceRef("multi-deed", "deed")
    tip = EvidenceRef("multi-tip", "anonymous_tip")
    proposal = _proposal(
        deed,
        assertion_type=INVESTIGATION_SOURCE_CLAIM,
        value={"claim": "combined"},
        authority="user",
    )
    # Preserve one semantic assertion but make it explicitly multi-source for the admission test.
    proposal = proposal.__class__(
        **{
            **proposal.__dict__,
            "evidence": (deed, tip),
        }
    )
    grants = (
        make_grant(
            grant_id="multi-deed-grant",
            evidence=deed,
            provenance_class="trusted_record",
            authority_ceiling="trusted_tool",
        ),
        make_grant(
            grant_id="multi-tip-grant",
            evidence=tip,
            provenance_class="untrusted_record",
            authority_ceiling="agent",
        ),
    )
    decision = AdmissionEngine().admit(
        proposal,
        grants=grants,
        policy=InvestigationAdmissionPolicy(),
    )
    assert decision.status == "admitted"
    assert decision.ingress_ceiling == "agent"
    assert decision.policy_ceiling == "agent"
    assert decision.effective_authority == "agent"


def test_admission_policy_version_seals_distinct_durable_interpretation_ids() -> None:
    source = EvidenceRef("versioned-preference", "user_message")
    proposal = _proposal(
        source,
        assertion_type=PERSONALITY_EXPLICIT_PREFERENCE,
        value={"preference": "short"},
        authority="user",
    )
    grant = make_grant(
        grant_id="versioned-preference-grant",
        evidence=source,
        provenance_class="grounded_user_statement",
        authority_ceiling="user",
    )
    engine = AdmissionEngine()
    v1 = engine.admit(
        proposal,
        grants=(grant,),
        policy=PersonalityAdmissionPolicy(version=1),
    )
    v2 = engine.admit(
        proposal,
        grants=(grant,),
        policy=PersonalityAdmissionPolicy(version=2),
    )
    assert v1.status == v2.status == "admitted"
    assert v1.admitted_assertion is not None
    assert v2.admitted_assertion is not None
    assert v1.admitted_assertion.source_key == v2.admitted_assertion.source_key
    assert v1.admitted_assertion.assertion_id != v2.admitted_assertion.assertion_id


NAMESPACE = "mutation-admission"


def _repo() -> Neo4jRepository:
    uri = os.getenv("MENHIR_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("requires explicit MENHIR_TEST_NEO4J_URI scratch instance")
    repo = Neo4jRepository(
        uri=uri,
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
    )
    if not repo.ping():
        repo.close()
        pytest.fail(f"configured test Neo4j is unreachable: {uri}")
    return repo


def test_real_neo4j_admission_receipts_preserve_rejections_and_only_store_sealed_assertions() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        assertions = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        admissions = Neo4jAdmissionStore(
            repo,
            namespace=NAMESPACE,
            assertions=assertions,
        )
        assertions.activate()
        admissions.activate()
        engine = AdmissionEngine()

        model_source = EvidenceRef("persist-model", "model_observation")
        model_proposal = _proposal(
            model_source,
            assertion_type="test.claim",
            value={"value": "model"},
            authority="user",
        )
        model_decision = engine.admit(
            model_proposal,
            grants=(
                make_grant(
                    grant_id="persist-model-grant",
                    evidence=model_source,
                    provenance_class="model_inference",
                    authority_ceiling="agent",
                ),
            ),
            policy=PermissiveUserPolicy(),
        )
        model_write = admissions.admit_and_record(model_decision)
        assert model_write["assertion_recorded"] is True

        rejected_source = EvidenceRef("persist-rejected", "user_message")
        rejected_proposal = _proposal(
            rejected_source,
            assertion_type=PERSONALITY_EXPLICIT_PREFERENCE,
            value={"preference": "invented"},
            authority="user",
        )
        rejected_decision = engine.admit(
            rejected_proposal,
            grants=(
                make_grant(
                    grant_id="persist-rejected-model-grant",
                    evidence=rejected_source,
                    provenance_class="model_inference",
                    authority_ceiling="agent",
                ),
            ),
            policy=PersonalityAdmissionPolicy(),
        )
        rejected_write = admissions.admit_and_record(rejected_decision)
        assert rejected_write["assertion_recorded"] is False

        explicit_source = EvidenceRef("persist-explicit", "user_message")
        explicit_proposal = _proposal(
            explicit_source,
            assertion_type=PERSONALITY_EXPLICIT_PREFERENCE,
            value={"preference": "concise"},
            authority="user",
            key="response_style",
        )
        explicit_decision = engine.admit(
            explicit_proposal,
            grants=(
                make_grant(
                    grant_id="persist-explicit-grant",
                    evidence=explicit_source,
                    provenance_class="grounded_user_statement",
                    authority_ceiling="user",
                ),
            ),
            policy=PersonalityAdmissionPolicy(),
        )
        explicit_write = admissions.admit_and_record(explicit_decision)
        assert explicit_write["assertion_recorded"] is True

        forged_source = EvidenceRef("persist-forged", "deed")
        forged_proposal = _proposal(
            forged_source,
            assertion_type=INVESTIGATION_SOURCE_CLAIM,
            value={"claim": "forged kind"},
            authority="user",
        )
        forged_decision = engine.admit(
            forged_proposal,
            grants=(
                SourceAuthorityGrant(
                    grant_id="persist-forged-grant",
                    source_id=forged_source.source_id,
                    verified_source_kind="anonymous_tip",
                    provenance_class="untrusted_record",
                    authority_ceiling="agent",
                ),
            ),
            policy=InvestigationAdmissionPolicy(),
        )
        admissions.admit_and_record(forged_decision)

        assert assertions.count_assertions() == 2
        stored = assertions.load_assertions()
        authorities = {row.assertion_type: row.authority for row in stored}
        assert authorities["test.claim"] == "agent"
        assert authorities[PERSONALITY_EXPLICIT_PREFERENCE] == "user"
        assert all("|admission:" in row.interpreter_version for row in stored)
        assert all(row.assertion_id not in {model_proposal.assertion_id, explicit_proposal.assertion_id} for row in stored)

        receipts = admissions.load_receipts()
        assert len(receipts) == 4
        by_id = {row["decision_id"]: row for row in receipts}
        assert by_id[model_decision.decision_id]["attempted_promotion"] is True
        assert by_id[model_decision.decision_id]["effective_authority"] == "agent"
        assert by_id[model_decision.decision_id]["assertion_persisted"] is True
        assert by_id[rejected_decision.decision_id]["status"] == "rejected"
        assert by_id[rejected_decision.decision_id]["assertion_persisted"] is False
        assert by_id[forged_decision.decision_id]["reason"] == "evidence_grant_mismatch"
        assert by_id[forged_decision.decision_id]["assertion_persisted"] is False

        replay = admissions.admit_and_record(model_decision)
        assert replay["assertion_recorded"] is True
        assert assertions.count_assertions() == 2
        assert len(admissions.load_receipts()) == 4
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
