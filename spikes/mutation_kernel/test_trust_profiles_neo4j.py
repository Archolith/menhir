from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.admission import AdmissionEngine, make_grant
from spikes.mutation_kernel.admission_policies import (
    INVESTIGATION_SOURCE_CLAIM,
    PERSONALITY_EXPLICIT_PREFERENCE,
    InvestigationAdmissionPolicy,
    PersonalityAdmissionPolicy,
)
from spikes.mutation_kernel.kernel import AUTHORITY_ORDER, EvidenceRef, make_assertion
from spikes.mutation_kernel.trust import (
    PurposeTrustEngine,
    build_legacy_trust_profile,
    build_trust_profile,
)
from spikes.mutation_kernel.trust_resolvers import (
    InvestigationPurposeTrustResolver,
    LegacyAuthorityResolver,
    PersonalityPurposeTrustResolver,
)
from spikes.mutation_kernel.trust_store import Neo4jTrustProfileStore


NAMESPACE = "mutation-trust-profile"


def _proposal(
    source: EvidenceRef,
    *,
    assertion_type: str,
    authority: str,
    value: object,
    key: str = "claim",
):
    return make_assertion(
        source=source,
        assertion_type=assertion_type,
        subject_id="trust-subject",
        scope="trust-scope",
        key=key,
        value=value,
        valid_at="2026-08-15T00:00:00Z",
        authority=authority,
        interpreter_version="trust-fixture-v1",
    )


def _admit_investigation(
    source: EvidenceRef,
    *,
    provenance_class: str,
    ingress_authority: str,
    requested_authority: str = "user",
):
    proposal = _proposal(
        source,
        assertion_type=INVESTIGATION_SOURCE_CLAIM,
        authority=requested_authority,
        value={"claim": source.source_id},
    )
    grant = make_grant(
        grant_id=f"grant-{source.source_id}",
        evidence=source,
        provenance_class=provenance_class,
        authority_ceiling=ingress_authority,
    )
    decision = AdmissionEngine().admit(
        proposal,
        grants=(grant,),
        policy=InvestigationAdmissionPolicy(),
    )
    assert decision.status == "admitted"
    return decision, grant


def test_same_deed_profile_resolves_differently_for_recorded_and_beneficial_owner() -> None:
    source = EvidenceRef("trust-deed", "deed")
    decision, grant = _admit_investigation(
        source,
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
    )
    assert decision.effective_authority == "trusted_tool"
    profile = build_trust_profile(
        decision,
        grants=(grant,),
        extension_facets=(("investigation.document_role", "title_record"),),
    )

    engine = PurposeTrustEngine()
    resolver = InvestigationPurposeTrustResolver()
    recorded = engine.resolve(profile, purpose="recorded_title", resolver=resolver)
    beneficial = engine.resolve(profile, purpose="beneficial_owner", resolver=resolver)

    assert recorded.effective_authority == "trusted_tool"
    assert recorded.clamped is False
    assert beneficial.effective_authority == "agent"
    assert beneficial.clamped is False
    assert recorded.profile_id == beneficial.profile_id == profile.profile_id
    assert profile.admission_authority == "trusted_tool"
    assert profile.facet("investigation.document_role") == "title_record"


def test_same_authenticated_user_profile_is_user_for_own_preference_but_agent_for_external_fact() -> None:
    source = EvidenceRef("trust-user-preference", "user_message")
    proposal = _proposal(
        source,
        assertion_type=PERSONALITY_EXPLICIT_PREFERENCE,
        authority="user",
        value={"preference": "concise"},
        key="response_style",
    )
    grant = make_grant(
        grant_id="grant-trust-user-preference",
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
    assert decision.effective_authority == "user"
    profile = build_trust_profile(decision, grants=(grant,))

    engine = PurposeTrustEngine()
    resolver = PersonalityPurposeTrustResolver()
    preference = engine.resolve(profile, purpose="explicit_preference", resolver=resolver)
    external = engine.resolve(profile, purpose="external_fact", resolver=resolver)

    assert preference.effective_authority == "user"
    assert external.effective_authority == "agent"
    assert preference.profile_id == external.profile_id


def test_purpose_resolver_cannot_raise_an_anonymous_tip_above_admission_ceiling() -> None:
    source = EvidenceRef("trust-anonymous-tip", "anonymous_tip")
    decision, grant = _admit_investigation(
        source,
        provenance_class="untrusted_record",
        ingress_authority="agent",
        requested_authority="manual",
    )
    assert decision.effective_authority == "agent"
    profile = build_trust_profile(decision, grants=(grant,))

    resolution = PurposeTrustEngine().resolve(
        profile,
        purpose="lead_priority",
        resolver=InvestigationPurposeTrustResolver(),
    )
    assert resolution.requested_authority == "trusted_tool"
    assert resolution.admission_ceiling == "agent"
    assert resolution.effective_authority == "agent"
    assert resolution.clamped is True


def test_extensions_can_add_opaque_facets_but_cannot_overwrite_core_facets() -> None:
    source = EvidenceRef("trust-extension-facet", "deed")
    decision, grant = _admit_investigation(
        source,
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
    )
    profile = build_trust_profile(
        decision,
        grants=(grant,),
        extension_facets=(
            ("investigation.corroboration", "single_source"),
            ("investigation.document_role", "title_record"),
        ),
    )
    assert profile.facet("investigation.corroboration") == "single_source"
    assert profile.facet("core.admission_authority") == "trusted_tool"

    with pytest.raises(ValueError, match="protected core trust facets"):
        build_trust_profile(
            decision,
            grants=(grant,),
            extension_facets=(("core.admission_authority", "user"),),
        )


def test_legacy_authority_profile_preserves_existing_total_order_for_scalar_compatibility() -> None:
    engine = PurposeTrustEngine()
    resolver = LegacyAuthorityResolver()
    for authority in AUTHORITY_ORDER:
        profile = build_legacy_trust_profile(
            assertion_id=f"legacy-{authority}",
            source_key=f"legacy-source-{authority}",
            authority=authority,
            source_kind="menhir.typed_scalar",
        )
        for purpose in ("scalar_state", "arbitrary_future_purpose"):
            resolution = engine.resolve(profile, purpose=purpose, resolver=resolver)
            assert resolution.effective_authority == authority
            assert resolution.requested_authority == authority
            assert resolution.clamped is False


def test_two_profiles_with_same_admission_authority_can_still_have_different_domain_evidence_shape() -> None:
    deed_source = EvidenceRef("same-ceiling-deed", "deed")
    deed_decision, deed_grant = _admit_investigation(
        deed_source,
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
        requested_authority="trusted_tool",
    )
    manual_source = EvidenceRef("same-ceiling-note", "investigator_note")
    manual_decision, manual_grant = _admit_investigation(
        manual_source,
        provenance_class="operator_manual",
        ingress_authority="trusted_tool",
        requested_authority="trusted_tool",
    )
    assert deed_decision.effective_authority == "trusted_tool"
    assert manual_decision.effective_authority == "trusted_tool"

    deed_profile = build_trust_profile(deed_decision, grants=(deed_grant,))
    note_profile = build_trust_profile(manual_decision, grants=(manual_grant,))
    resolver = InvestigationPurposeTrustResolver()
    engine = PurposeTrustEngine()

    deed_beneficial = engine.resolve(
        deed_profile,
        purpose="beneficial_owner",
        resolver=resolver,
    )
    note_beneficial = engine.resolve(
        note_profile,
        purpose="beneficial_owner",
        resolver=resolver,
    )
    assert deed_beneficial.effective_authority == "agent"
    # Resolver asks for manual on the operator note, but the admission hard ceiling was deliberately
    # only trusted_tool, so core still clamps it to trusted_tool.
    assert note_beneficial.requested_authority == "manual"
    assert note_beneficial.effective_authority == "trusted_tool"
    assert note_beneficial.clamped is True


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


def test_real_neo4j_trust_profile_roundtrip_preserves_opaque_facets_and_purpose_resolution() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store = Neo4jTrustProfileStore(repo, namespace=NAMESPACE)
        store.activate()

        source = EvidenceRef("persisted-trust-deed", "deed")
        decision, grant = _admit_investigation(
            source,
            provenance_class="trusted_record",
            ingress_authority="trusted_tool",
        )
        profile = build_trust_profile(
            decision,
            grants=(grant,),
            extension_facets=(
                ("investigation.corroboration", "two_independent_indexes"),
                ("investigation.document_role", "title_record"),
            ),
        )
        first = store.record(profile)
        assert first["created"] is True
        replay = store.record(profile)
        assert replay["created"] is False
        assert store.count() == 1

        loaded = store.load_for_assertion(profile.assertion_id)
        assert loaded == [profile]
        restored = loaded[0]
        assert restored.facet("investigation.corroboration") == "two_independent_indexes"
        assert restored.provenance_classes == ("trusted_record",)
        assert restored.source_kinds == ("deed",)

        resolver = InvestigationPurposeTrustResolver()
        engine = PurposeTrustEngine()
        assert engine.resolve(
            restored,
            purpose="recorded_title",
            resolver=resolver,
        ).effective_authority == "trusted_tool"
        assert engine.resolve(
            restored,
            purpose="beneficial_owner",
            resolver=resolver,
        ).effective_authority == "agent"
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
