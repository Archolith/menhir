from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.admission import AdmissionEngine, make_grant
from spikes.mutation_kernel.admission_policies import InvestigationAdmissionPolicy
from spikes.mutation_kernel.graph import ResolvedEntity
from spikes.mutation_kernel.investigation import (
    PARCEL_KIND,
    PERSON_KIND,
    ownership_assertion,
)
from spikes.mutation_kernel.kernel import Abstention, EvidenceRef, View
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.trust import build_trust_profile
from spikes.mutation_kernel.trust_fold import (
    INVESTIGATION_CONCLUSION_VIEW,
    InvestigationConclusionTrustResolver,
    Neo4jTrustFoldTraceStore,
    TrustAwareOwnershipFold,
)
from spikes.mutation_kernel.trust_store import Neo4jTrustProfileStore


NAMESPACE = "mutation-trust-fold"


ALICE = ResolvedEntity(
    entity_kind=PERSON_KIND,
    entity_kind_version=1,
    entity_id="person-alice",
    canonical_key="external:alice",
    display_name="Alice",
)
BOB = ResolvedEntity(
    entity_kind=PERSON_KIND,
    entity_kind_version=1,
    entity_id="person-bob",
    canonical_key="external:bob",
    display_name="Bob",
)
PARCEL = ResolvedEntity(
    entity_kind=PARCEL_KIND,
    entity_kind_version=1,
    entity_id="parcel-42",
    canonical_key="travis:42",
    display_name="Parcel 42",
)


def _admitted_ownership(
    *,
    source_id: str,
    source_kind: str,
    provenance_class: str,
    ingress_authority: str,
    owner: ResolvedEntity,
    evidentiary_role: str,
    valid_at: str,
):
    source = EvidenceRef(source_id, source_kind)
    proposal = ownership_assertion(
        source,
        investigation_id="investigation-15",
        owner=owner,
        parcel=PARCEL,
        valid_at=valid_at,
        authority="user",
        interpreter_version="trust-fold-fixture-v1",
    )
    grant = make_grant(
        grant_id=f"grant-{source_id}",
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
    assert decision.admitted_assertion is not None
    profile = build_trust_profile(
        decision,
        grants=(grant,),
        extension_facets=(("investigation.evidentiary_role", evidentiary_role),),
    )
    return decision.admitted_assertion, profile


def _fixture():
    deed, deed_profile = _admitted_ownership(
        source_id="fold-deed",
        source_kind="deed",
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
        owner=ALICE,
        evidentiary_role="title_record",
        valid_at="2026-01-01T00:00:00Z",
    )
    note, note_profile = _admitted_ownership(
        source_id="fold-note",
        source_kind="investigator_note",
        provenance_class="operator_manual",
        ingress_authority="trusted_tool",
        owner=BOB,
        evidentiary_role="beneficial_owner_synthesis",
        valid_at="2026-01-02T00:00:00Z",
    )
    tip, tip_profile = _admitted_ownership(
        source_id="fold-tip",
        source_kind="anonymous_tip",
        provenance_class="untrusted_record",
        ingress_authority="agent",
        owner=BOB,
        evidentiary_role="anonymous_allegation",
        valid_at="2026-01-03T00:00:00Z",
    )
    user, user_profile = _admitted_ownership(
        source_id="fold-user",
        source_kind="user_statement",
        provenance_class="grounded_user_statement",
        ingress_authority="agent",
        owner=ALICE,
        evidentiary_role="third_party_statement",
        valid_at="2026-01-04T00:00:00Z",
    )
    assertions = (deed, note, tip, user)
    profiles = {
        deed.assertion_id: deed_profile,
        note.assertion_id: note_profile,
        tip.assertion_id: tip_profile,
        user.assertion_id: user_profile,
    }
    return assertions, profiles, {
        "deed": deed,
        "note": note,
        "tip": tip,
        "user": user,
    }


def test_same_evidence_set_forms_different_beliefs_by_purpose_with_exact_roles() -> None:
    assertions, profiles, rows = _fixture()

    recorded = TrustAwareOwnershipFold(purpose="recorded_title").fold(
        assertions,
        profiles=profiles,
    )
    assert isinstance(recorded.outcome, View)
    assert recorded.outcome.value == {
        "owner_entity_id": ALICE.entity_id,
        "purpose": "recorded_title",
    }
    assert recorded.outcome.contributor_ids == (rows["deed"].assertion_id,)
    assert recorded.trace.included_ids == (rows["deed"].assertion_id,)
    assert recorded.trace.excluded_ids == (rows["user"].assertion_id,)
    assert recorded.outcome.counterevidence_ids == tuple(
        sorted((rows["note"].assertion_id, rows["tip"].assertion_id))
    )
    assert recorded.trace.counterevidence_ids == recorded.outcome.counterevidence_ids
    assert recorded.outcome.effective_authority == "trusted_tool"

    beneficial = TrustAwareOwnershipFold(purpose="beneficial_owner").fold(
        assertions,
        profiles=profiles,
    )
    assert isinstance(beneficial.outcome, View)
    assert beneficial.outcome.value == {
        "owner_entity_id": BOB.entity_id,
        "purpose": "beneficial_owner",
    }
    assert beneficial.outcome.contributor_ids == (rows["note"].assertion_id,)
    assert beneficial.trace.excluded_ids == (rows["tip"].assertion_id,)
    assert beneficial.outcome.counterevidence_ids == tuple(
        sorted((rows["deed"].assertion_id, rows["user"].assertion_id))
    )
    assert beneficial.outcome.effective_authority == "trusted_tool"

    assert recorded.trace.trace_id != beneficial.trace.trace_id
    assert set(recorded.trace.included_ids) != set(beneficial.trace.included_ids)
    assert set(recorded.trace.counterevidence_ids) != set(
        beneficial.trace.counterevidence_ids
    )


def test_lower_trust_agreement_is_excluded_not_silently_promoted_into_contributors() -> None:
    assertions, profiles, rows = _fixture()
    result = TrustAwareOwnershipFold(purpose="beneficial_owner").fold(
        assertions,
        profiles=profiles,
    )
    assert isinstance(result.outcome, View)

    by_id = {row.assertion_id: row for row in result.trace.participations}
    tip = by_id[rows["tip"].assertion_id]
    note = by_id[rows["note"].assertion_id]
    assert tip.claimed_value == note.claimed_value == BOB.entity_id
    assert tip.role == "excluded"
    assert tip.effective_authority == "agent"
    assert note.role == "included"
    assert note.effective_authority == "trusted_tool"
    assert rows["tip"].assertion_id not in result.outcome.contributor_ids


def test_conflicting_lower_trust_evidence_is_preserved_as_counterevidence() -> None:
    assertions, profiles, rows = _fixture()
    result = TrustAwareOwnershipFold(purpose="recorded_title").fold(
        assertions,
        profiles=profiles,
    )
    assert isinstance(result.outcome, View)
    by_id = {row.assertion_id: row for row in result.trace.participations}
    assert by_id[rows["note"].assertion_id].role == "counterevidence"
    assert by_id[rows["tip"].assertion_id].role == "counterevidence"
    assert by_id[rows["user"].assertion_id].role == "excluded"


def test_equal_top_trust_conflict_abstains_instead_of_count_or_recency_tiebreak() -> None:
    deed, deed_profile = _admitted_ownership(
        source_id="tie-deed",
        source_kind="deed",
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
        owner=ALICE,
        evidentiary_role="title_record",
        valid_at="2026-02-01T00:00:00Z",
    )
    court, court_profile = _admitted_ownership(
        source_id="tie-court",
        source_kind="court_record",
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
        owner=BOB,
        evidentiary_role="title_record",
        valid_at="2026-02-02T00:00:00Z",
    )
    result = TrustAwareOwnershipFold(purpose="recorded_title").fold(
        (deed, court),
        profiles={
            deed.assertion_id: deed_profile,
            court.assertion_id: court_profile,
        },
    )
    assert isinstance(result.outcome, Abstention)
    assert result.outcome.reason == "contested_top_trust"
    assert result.outcome.assertion_ids == tuple(
        sorted((deed.assertion_id, court.assertion_id))
    )
    assert result.trace.status == "abstention"
    assert result.trace.included_ids == ()
    assert result.trace.excluded_ids == ()
    assert result.trace.counterevidence_ids == result.outcome.assertion_ids
    assert {row.role for row in result.trace.participations} == {"contested"}


def test_fold_fails_closed_when_profile_is_missing_or_bound_to_wrong_assertion() -> None:
    assertions, profiles, _rows = _fixture()
    missing = dict(profiles)
    missing.pop(assertions[0].assertion_id)
    with pytest.raises(ValueError, match="missing trust profile"):
        TrustAwareOwnershipFold(purpose="recorded_title").fold(
            assertions,
            profiles=missing,
        )

    wrong = dict(profiles)
    wrong[assertions[0].assertion_id] = profiles[assertions[1].assertion_id]
    with pytest.raises(ValueError, match="does not match assertion"):
        TrustAwareOwnershipFold(purpose="recorded_title").fold(
            assertions,
            profiles=wrong,
        )


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


def test_real_neo4j_persists_assertions_profiles_two_purpose_views_and_exact_fold_traces() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        envelope_store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        profile_store = Neo4jTrustProfileStore(repo, namespace=NAMESPACE)
        trace_store = Neo4jTrustFoldTraceStore(repo, namespace=NAMESPACE)
        envelope_store.activate()
        profile_store.activate()
        trace_store.activate()

        assertions, profiles, rows = _fixture()
        for assertion in assertions:
            envelope_store.record_assertion(assertion)
            profile_store.record(profiles[assertion.assertion_id])

        persisted_assertions = envelope_store.load_assertions(
            subject_id=PARCEL.entity_id,
            scope="investigation-15",
            key="owner",
        )
        persisted_profiles = {
            assertion.assertion_id: profile_store.load_for_assertion(assertion.assertion_id)[0]
            for assertion in persisted_assertions
        }
        assert {row.assertion_id for row in persisted_assertions} == {
            row.assertion_id for row in assertions
        }
        assert profile_store.count() == 4

        recorded = TrustAwareOwnershipFold(purpose="recorded_title").fold(
            persisted_assertions,
            profiles=persisted_profiles,
        )
        beneficial = TrustAwareOwnershipFold(purpose="beneficial_owner").fold(
            persisted_assertions,
            profiles=persisted_profiles,
        )
        assert isinstance(recorded.outcome, View)
        assert isinstance(beneficial.outcome, View)

        envelope_store.record_outcome(recorded.outcome)
        envelope_store.record_outcome(beneficial.outcome)
        first_trace = trace_store.record(recorded.trace)
        second_trace = trace_store.record(beneficial.trace)
        assert first_trace["created"] is True
        assert second_trace["created"] is True
        assert trace_store.record(recorded.trace)["created"] is False
        assert trace_store.count() == 2

        views = envelope_store.load_views(
            subject_id=PARCEL.entity_id,
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            scope="investigation-15",
            key="owner",
        )
        assert len(views) == 2
        by_purpose = {dict(view.dimensions)["purpose"]: view for view in views}
        assert by_purpose["recorded_title"] == recorded.outcome
        assert by_purpose["beneficial_owner"] == beneficial.outcome

        restored_recorded_trace = trace_store.load(recorded.trace.trace_id)
        restored_beneficial_trace = trace_store.load(beneficial.trace.trace_id)
        assert restored_recorded_trace == recorded.trace
        assert restored_beneficial_trace == beneficial.trace

        assert restored_recorded_trace is not None
        assert restored_recorded_trace.included_ids == (rows["deed"].assertion_id,)
        assert restored_recorded_trace.excluded_ids == (rows["user"].assertion_id,)
        assert restored_beneficial_trace is not None
        assert restored_beneficial_trace.included_ids == (rows["note"].assertion_id,)
        assert restored_beneficial_trace.excluded_ids == (rows["tip"].assertion_id,)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
