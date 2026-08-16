from __future__ import annotations

from dataclasses import dataclass
import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.admission import AdmissionEngine, make_grant
from spikes.mutation_kernel.admission_policies import InvestigationAdmissionPolicy
from spikes.mutation_kernel.graph import ResolvedEntity
from spikes.mutation_kernel.investigation import PARCEL_KIND, PERSON_KIND, ownership_assertion
from spikes.mutation_kernel.kernel import Abstention, EvidenceRef, View
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore
from spikes.mutation_kernel.registry import ProjectionTarget
from spikes.mutation_kernel.trust import TrustResolutionProposal, build_trust_profile
from spikes.mutation_kernel.trust_deployment import (
    PurposeTrustResolverDefinition,
    StaleTrustResolverRegistryError,
    TrustResolverDeploymentCoordinator,
)
from spikes.mutation_kernel.trust_fold import (
    INVESTIGATION_CONCLUSION_VIEW,
    InvestigationConclusionTrustResolver,
    Neo4jTrustFoldTraceStore,
    TrustAwareOwnershipFold,
)
from spikes.mutation_kernel.trust_store import Neo4jTrustProfileStore


NAMESPACE = "mutation-trust-deployment"
PURPOSES = ("beneficial_owner", "recorded_title")


ALICE = ResolvedEntity(
    entity_kind=PERSON_KIND,
    entity_kind_version=1,
    entity_id="deployment-alice",
    canonical_key="external:deployment-alice",
    display_name="Alice",
)
BOB = ResolvedEntity(
    entity_kind=PERSON_KIND,
    entity_kind_version=1,
    entity_id="deployment-bob",
    canonical_key="external:deployment-bob",
    display_name="Bob",
)
PARCEL = ResolvedEntity(
    entity_kind=PARCEL_KIND,
    entity_kind_version=1,
    entity_id="deployment-parcel",
    canonical_key="travis:deployment-parcel",
    display_name="Deployment Parcel",
)


@dataclass(frozen=True)
class StricterInvestigationConclusionTrustResolverV2:
    """V2 deliberately invalidates the old operator-synthesis shortcut.

    A beneficial-owner operator note now needs a new explicit v2 evidentiary role. Existing immutable
    profiles do not have it, so the old note and deed both resolve to agent for beneficial ownership,
    turning the previously materialized Bob View into a top-trust conflict/abstention.
    """

    resolver_id: str = "investigation.conclusion_trust"
    version: int = 2

    def resolve(self, profile, purpose: str) -> TrustResolutionProposal:
        role = profile.facet("investigation.evidentiary_role", "unspecified")
        source_kinds = set(profile.source_kinds)
        used = ("core.source_kinds", "investigation.evidentiary_role")

        if purpose == "recorded_title":
            if role == "title_record" and source_kinds & {"deed", "court_record", "official_filing"}:
                return TrustResolutionProposal(
                    authority="trusted_tool",
                    reason="v2_direct_title_record",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="v2_indirect_for_recorded_title",
                used_facets=used,
            )

        if purpose == "beneficial_owner":
            if (
                role == "verified_beneficial_owner_synthesis_v2"
                and "investigator_note" in source_kinds
            ):
                return TrustResolutionProposal(
                    authority="manual",
                    reason="v2_verified_beneficial_owner_synthesis",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="v2_requires_verified_beneficial_owner_synthesis",
                used_facets=used,
            )

        return TrustResolutionProposal(
            authority="agent",
            reason="v2_unknown_purpose",
            used_facets=used,
        )


@dataclass(frozen=True)
class StricterInvestigationConclusionTrustResolverV3(StricterInvestigationConclusionTrustResolverV2):
    version: int = 3


def _admitted(
    *,
    source_id: str,
    source_kind: str,
    provenance_class: str,
    ingress_authority: str,
    owner: ResolvedEntity,
    role: str,
):
    source = EvidenceRef(source_id, source_kind)
    proposal = ownership_assertion(
        source,
        investigation_id="deployment-investigation",
        owner=owner,
        parcel=PARCEL,
        valid_at="2026-03-01T00:00:00Z",
        authority="user",
        interpreter_version="trust-deployment-fixture-v1",
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
        extension_facets=(("investigation.evidentiary_role", role),),
    )
    return decision.admitted_assertion, profile


def _fixture():
    deed, deed_profile = _admitted(
        source_id="deployment-deed",
        source_kind="deed",
        provenance_class="trusted_record",
        ingress_authority="trusted_tool",
        owner=ALICE,
        role="title_record",
    )
    note, note_profile = _admitted(
        source_id="deployment-note",
        source_kind="investigator_note",
        provenance_class="operator_manual",
        ingress_authority="trusted_tool",
        owner=BOB,
        role="beneficial_owner_synthesis",
    )
    return (
        (deed, note),
        {
            deed.assertion_id: deed_profile,
            note.assertion_id: note_profile,
        },
    )


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        view_type=INVESTIGATION_CONCLUSION_VIEW,
        subject_id=PARCEL.entity_id,
        scope="deployment-investigation",
        key="owner",
        dimensions=(("purpose", "beneficial_owner"),),
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


def test_resolver_upgrade_dirties_target_fences_stale_commit_and_refolds_to_abstention() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        envelope_store = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
        profile_store = Neo4jTrustProfileStore(repo, namespace=NAMESPACE)
        trace_store = Neo4jTrustFoldTraceStore(repo, namespace=NAMESPACE)
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        envelope_store.activate()
        profile_store.activate()
        trace_store.activate()
        deployment.activate()

        assertions, profiles = _fixture()
        for assertion in assertions:
            envelope_store.record_assertion(assertion)
            profile_store.record(profiles[assertion.assertion_id])

        resolver_v1 = InvestigationConclusionTrustResolver()
        definition_v1 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v1,
            purposes=PURPOSES,
        )
        first_publish = deployment.publish_definition(definition_v1)
        assert first_publish == {
            "action": "advance",
            "epoch": 1,
            "current_version": 1,
            "invalidated_target_count": 0,
        }
        target = _target()
        deployment.register_dependency(definition_v1, target)
        snapshot_v1 = deployment.capture_snapshot(definition_v1)

        result_v1 = TrustAwareOwnershipFold(
            purpose="beneficial_owner",
            resolver=resolver_v1,
        ).fold(assertions, profiles=profiles)
        assert isinstance(result_v1.outcome, View)
        assert result_v1.outcome.value["owner_entity_id"] == BOB.entity_id
        assert result_v1.trace.resolver_version == 1

        def commit_v1():
            envelope_store.record_outcome(result_v1.outcome)
            trace_store.record(result_v1.trace)

        deployment.run_fenced(definition_v1, resolver_v1, snapshot_v1, commit_v1)
        current = envelope_store.load_outcomes(
            subject_id=PARCEL.entity_id,
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            scope="deployment-investigation",
            key="owner",
            dimensions=(("purpose", "beneficial_owner"),),
        )
        assert current == [result_v1.outcome]
        assert trace_store.count() == 1

        resolver_v2 = StricterInvestigationConclusionTrustResolverV2()
        definition_v2 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v2,
            purposes=PURPOSES,
        )
        second_publish = deployment.publish_definition(definition_v2)
        assert second_publish == {
            "action": "advance",
            "epoch": 2,
            "current_version": 2,
            "invalidated_target_count": 1,
        }
        pending = deployment.pending_work(resolver_v1.resolver_id)
        assert len(pending) == 1
        work = pending[0]
        assert work.resolver_version == 2
        assert work.generation == 1
        assert work.completed_generation == 0
        assert work.target == target

        # Publication makes the existing v1 View known-dirty, but it remains present until repair.
        assert envelope_store.load_views(
            subject_id=PARCEL.entity_id,
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            scope="deployment-investigation",
            key="owner",
            dimensions=(("purpose", "beneficial_owner"),),
        ) == [result_v1.outcome]

        stale_operation_called = False

        def stale_commit():
            nonlocal stale_operation_called
            stale_operation_called = True
            envelope_store.record_outcome(result_v1.outcome)

        with pytest.raises(StaleTrustResolverRegistryError, match="changed"):
            deployment.run_fenced(
                definition_v1,
                resolver_v1,
                snapshot_v1,
                stale_commit,
            )
        assert stale_operation_called is False

        with pytest.raises(StaleTrustResolverRegistryError):
            deployment.capture_snapshot(definition_v1)

        snapshot_v2 = deployment.capture_snapshot(definition_v2)
        result_v2 = TrustAwareOwnershipFold(
            purpose="beneficial_owner",
            resolver=resolver_v2,
        ).fold(assertions, profiles=profiles)
        assert isinstance(result_v2.outcome, Abstention)
        assert result_v2.outcome.reason == "contested_top_trust"
        assert result_v2.trace.resolver_version == 2
        assert result_v2.trace.trace_id != result_v1.trace.trace_id

        def commit_v2():
            envelope_store.record_outcome(result_v2.outcome)
            trace_store.record(result_v2.trace)

        deployment.run_fenced(definition_v2, resolver_v2, snapshot_v2, commit_v2)
        deployment.complete_work(work, definition=definition_v2)

        outcomes = envelope_store.load_outcomes(
            subject_id=PARCEL.entity_id,
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            scope="deployment-investigation",
            key="owner",
            dimensions=(("purpose", "beneficial_owner"),),
        )
        assert outcomes == [result_v2.outcome]
        assert envelope_store.load_views(
            subject_id=PARCEL.entity_id,
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            scope="deployment-investigation",
            key="owner",
            dimensions=(("purpose", "beneficial_owner"),),
        ) == []
        assert deployment.pending_work(resolver_v1.resolver_id) == ()

        # The old derivation remains immutable history even though its current View was replaced.
        assert trace_store.count() == 2
        assert trace_store.load(result_v1.trace.trace_id) == result_v1.trace
        assert trace_store.load(result_v2.trace.trace_id) == result_v2.trace
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_same_resolver_version_with_changed_declared_contract_fails_closed() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        deployment.activate()
        resolver_v1 = InvestigationConclusionTrustResolver()
        original = PurposeTrustResolverDefinition.from_resolver(
            resolver_v1,
            purposes=PURPOSES,
        )
        deployment.publish_definition(original)

        changed_without_bump = PurposeTrustResolverDefinition(
            resolver_id=original.resolver_id,
            version=1,
            purposes=("beneficial_owner",),
        )
        with pytest.raises(ValueError, match="without a version bump"):
            deployment.publish_definition(changed_without_bump)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_stale_work_completion_cannot_acknowledge_a_newer_resolver_generation() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        deployment.activate()

        resolver_v1 = InvestigationConclusionTrustResolver()
        definition_v1 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v1,
            purposes=PURPOSES,
        )
        deployment.publish_definition(definition_v1)
        deployment.register_dependency(definition_v1, _target())

        resolver_v2 = StricterInvestigationConclusionTrustResolverV2()
        definition_v2 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v2,
            purposes=PURPOSES,
        )
        deployment.publish_definition(definition_v2)
        work_v2 = deployment.pending_work(resolver_v1.resolver_id)[0]
        assert work_v2.generation == 1

        resolver_v3 = StricterInvestigationConclusionTrustResolverV3()
        definition_v3 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v3,
            purposes=PURPOSES,
        )
        third = deployment.publish_definition(definition_v3)
        assert third["invalidated_target_count"] == 1
        work_v3 = deployment.pending_work(resolver_v1.resolver_id)[0]
        assert work_v3.generation == 2
        assert work_v3.resolver_version == 3

        with pytest.raises(StaleTrustResolverRegistryError, match="changed"):
            deployment.complete_work(work_v2, definition=definition_v2)
        assert deployment.pending_work(resolver_v1.resolver_id) == (work_v3,)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_dependency_must_name_a_purpose_declared_by_resolver_definition() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        deployment.activate()
        resolver_v1 = InvestigationConclusionTrustResolver()
        definition_v1 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v1,
            purposes=PURPOSES,
        )
        deployment.publish_definition(definition_v1)
        invalid = ProjectionTarget(
            view_type=INVESTIGATION_CONCLUSION_VIEW,
            subject_id=PARCEL.entity_id,
            scope="deployment-investigation",
            key="owner",
            dimensions=(("purpose", "not_declared"),),
        )
        with pytest.raises(ValueError, match="not declared"):
            deployment.register_dependency(definition_v1, invalid)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_current_definition_cannot_be_paired_with_stale_resolver_object_at_commit_gate() -> None:
    repo = _repo()
    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        deployment = TrustResolverDeploymentCoordinator(repo, namespace=NAMESPACE)
        deployment.activate()
        resolver_v2 = StricterInvestigationConclusionTrustResolverV2()
        definition_v2 = PurposeTrustResolverDefinition.from_resolver(
            resolver_v2,
            purposes=PURPOSES,
        )
        deployment.publish_definition(definition_v2)
        snapshot_v2 = deployment.capture_snapshot(definition_v2)

        stale_resolver = InvestigationConclusionTrustResolver()
        called = False

        def operation():
            nonlocal called
            called = True

        with pytest.raises(StaleTrustResolverRegistryError, match="does not match"):
            deployment.run_fenced(
                definition_v2,
                stale_resolver,
                snapshot_v2,
                operation,
            )
        assert called is False
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
