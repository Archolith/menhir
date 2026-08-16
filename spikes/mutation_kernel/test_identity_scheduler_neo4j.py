from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.identity import (
    IdentityMigrationPlan,
    ReceiptIdentityAssignment,
)
from spikes.mutation_kernel.identity_generation import GenerationalNeo4jIdentityEvolution
from spikes.mutation_kernel.identity_generation_guard import (
    GenerationalGuardedNeo4jGraphMaterializer,
)
from spikes.mutation_kernel.identity_scheduler import IdentityDependencyScheduler
from spikes.mutation_kernel.investigation import (
    OWNERSHIP_ASSERTION,
    OWNERSHIP_MATERIALIZER,
    ownership_assertion,
    ownership_slot_key,
    parcel_proposal,
    person_proposal,
    investigation_graph_schema,
)
from spikes.mutation_kernel.investigation_identity_dependencies import (
    derive_ownership_with_identity_guards,
    ownership_identity_dependency_definition,
)
from spikes.mutation_kernel.kernel import EvidenceRef
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore


NAMESPACE = "mutation-identity-scheduler"
INVESTIGATION = "investigation:identity-scheduler"


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


def _setup(repo: Neo4jRepository):
    repo.execute("MATCH (n) DETACH DELETE n")
    graph = GenerationalGuardedNeo4jGraphMaterializer(
        repo,
        namespace=NAMESPACE,
        schema=investigation_graph_schema(),
    )
    assertions = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
    identity = GenerationalNeo4jIdentityEvolution(graph)
    scheduler = IdentityDependencyScheduler(repo, namespace=NAMESPACE)
    graph.activate()
    assertions.activate()
    identity.activate()
    scheduler.activate()
    return graph, assertions, identity, scheduler


def _parcel(graph: GenerationalGuardedNeo4jGraphMaterializer, suffix: str = "123"):
    return graph.record_entity(
        parcel_proposal(
            EvidenceRef(f"parcel-{suffix}", "county_record"),
            display_name=f"Parcel {suffix}",
            county="Travis",
            account_id=f"P-{suffix}",
            valid_at="2026-01-01T00:00:00Z",
        )
    )


def _load_ownership(assertions: Neo4jEnvelopeStore, parcel_id: str):
    return assertions.load_assertions(
        subject_id=parcel_id,
        assertion_type=OWNERSHIP_ASSERTION,
        scope=INVESTIGATION,
        key="owner",
    )


def test_identity_change_dirties_graph_work_and_stale_workers_cannot_restore_old_topology() -> None:
    repo = _repo()
    try:
        graph, assertions, identity, scheduler = _setup(repo)

        historical_source = EvidenceRef("historical-owner-source", "deed")
        historical_proposal = person_proposal(
            historical_source,
            display_name="Legacy Owner",
            external_id="legacy-owner",
            valid_at="2026-01-01T00:00:00Z",
        )
        historical_owner = graph.record_entity(historical_proposal)
        corrected_a = graph.record_entity(
            person_proposal(
                EvidenceRef("corrected-owner-a", "identity_review"),
                display_name="Corrected Owner A",
                external_id="corrected-owner-a",
                valid_at="2026-02-01T00:00:00Z",
                authority="manual",
            )
        )
        corrected_b = graph.record_entity(
            person_proposal(
                EvidenceRef("corrected-owner-b", "identity_review"),
                display_name="Corrected Owner B",
                external_id="corrected-owner-b",
                valid_at="2026-03-01T00:00:00Z",
                authority="manual",
            )
        )
        assert historical_owner.entity_id != corrected_a.entity_id
        assert corrected_a.entity_id != corrected_b.entity_id
        parcel = _parcel(graph)

        claim = ownership_assertion(
            historical_source,
            investigation_id=INVESTIGATION,
            owner=historical_owner,
            parcel=parcel,
            valid_at="2026-01-15T00:00:00Z",
        )
        assert claim.source_key == historical_proposal.source_key
        assertions.record_assertion(claim)

        initial = derive_ownership_with_identity_guards(
            _load_ownership(assertions, parcel.entity_id),
            identity,
        )
        assert initial.guards[0].expected_identity_generation == 0
        initial_write = graph.reconcile_edges_guarded(initial.outcome, initial.guards)
        assert initial_write["changed"] is True
        active = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
            active=True,
        )
        assert len(active) == 1
        assert active[0].source_entity_id == historical_owner.entity_id

        definition = ownership_identity_dependency_definition(assertions)
        identity.bootstrap_current_identity()
        baseline = scheduler.bootstrap(definition)
        assert baseline
        assert scheduler.load_dirty() == []

        stale_pre_migration = derive_ownership_with_identity_guards(
            _load_ownership(assertions, parcel.entity_id),
            identity,
        )
        assert stale_pre_migration.guards[0].expected_current_entity_id == historical_owner.entity_id
        assert stale_pre_migration.guards[0].expected_identity_generation == 0

        first_migration = IdentityMigrationPlan(
            migration_id="owner-correction-a",
            version=1,
            entity_kind=historical_owner.entity_kind,
            effective_at="2026-02-01T00:00:00Z",
            reason="source receipt was determined to refer to corrected owner A",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=historical_owner.entity_id,
                    source_key=historical_proposal.source_key,
                    target_entity_id=corrected_a.entity_id,
                ),
            ),
        )
        identity.apply(first_migration)

        resolved_after_first = identity.resolve_receipt(
            entity_kind=historical_owner.entity_kind,
            entity_id=historical_owner.entity_id,
            source_key=historical_proposal.source_key,
        )
        assert resolved_after_first is not None
        assert resolved_after_first.entity.entity_id == corrected_a.entity_id
        assert resolved_after_first.identity_generation == 1
        guarded_receipt = repo.execute(
            """
            MATCH (receipt:MutationEntityReceipt {storage_key:$storage_key})
                  -[:CURRENT_IDENTITY]->(current:MutationEntity)
            RETURN receipt.source_key AS source_key,
                   current.entity_id AS current_entity_id,
                   coalesce(receipt.identity_generation,0) AS identity_generation
            """,
            {"storage_key": stale_pre_migration.guards[0].receipt_storage_key},
        )
        assert len(guarded_receipt) == 1
        assert str(guarded_receipt[0]["source_key"]) == historical_proposal.source_key
        assert str(guarded_receipt[0]["current_entity_id"]) == corrected_a.entity_id
        assert int(guarded_receipt[0]["identity_generation"]) == 1

        with pytest.raises(ValueError, match="identity guard changed"):
            graph.reconcile_edges_guarded(
                stale_pre_migration.outcome,
                stale_pre_migration.guards,
            )
        active_before_repair = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
            active=True,
        )
        assert len(active_before_repair) == 1
        assert active_before_repair[0].source_entity_id == historical_owner.entity_id

        first_sync = scheduler.sync(definition)
        assert first_sync.changed_mapping_count == 1
        assert first_sync.target_count == 1
        assert first_sync.dirtied_target_count == 1
        dirty = scheduler.load_dirty()
        assert len(dirty) == 1
        first_dirty = dirty[0]
        assert first_dirty.target.materializer_id == OWNERSHIP_MATERIALIZER
        assert first_dirty.target.slot_key == ownership_slot_key(INVESTIGATION, parcel.entity_id)
        assert first_dirty.generation == 1
        assert first_dirty.projected_generation == 0

        fresh_a = derive_ownership_with_identity_guards(
            _load_ownership(assertions, parcel.entity_id),
            identity,
        )
        assert fresh_a.guards[0].expected_current_entity_id == corrected_a.entity_id
        assert fresh_a.guards[0].expected_identity_generation == 1
        first_repair = graph.reconcile_edges_guarded(fresh_a.outcome, fresh_a.guards)
        assert first_repair["changed"] is True
        assert first_repair["retired"] == 1
        assert scheduler.mark_projected(
            first_dirty,
            projection_hash=str(first_repair["projection_hash"]),
        ) is True
        assert scheduler.load_dirty() == []

        active_after_first = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
            active=True,
        )
        assert len(active_after_first) == 1
        assert active_after_first[0].source_entity_id == corrected_a.entity_id

        stale_a = derive_ownership_with_identity_guards(
            _load_ownership(assertions, parcel.entity_id),
            identity,
        )
        assert stale_a.guards[0].expected_current_entity_id == corrected_a.entity_id
        assert stale_a.guards[0].expected_identity_generation == 1

        second_migration = IdentityMigrationPlan(
            migration_id="owner-correction-b",
            version=1,
            entity_kind=historical_owner.entity_kind,
            effective_at="2026-03-01T00:00:00Z",
            reason="later identity review corrected the same receipt to owner B",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=historical_owner.entity_id,
                    source_key=historical_proposal.source_key,
                    target_entity_id=corrected_b.entity_id,
                ),
            ),
        )
        identity.apply(second_migration)
        resolved_after_second = identity.resolve_receipt(
            entity_kind=historical_owner.entity_kind,
            entity_id=historical_owner.entity_id,
            source_key=historical_proposal.source_key,
        )
        assert resolved_after_second is not None
        assert resolved_after_second.entity.entity_id == corrected_b.entity_id
        assert resolved_after_second.identity_generation == 2

        with pytest.raises(ValueError, match="identity guard changed"):
            graph.reconcile_edges_guarded(stale_a.outcome, stale_a.guards)

        second_sync = scheduler.sync(definition)
        assert second_sync.changed_mapping_count == 1
        assert second_sync.target_count == 1
        dirty = scheduler.load_dirty()
        assert len(dirty) == 1
        second_dirty = dirty[0]
        assert second_dirty.generation == 2
        assert second_dirty.projected_generation == 1

        fresh_b = derive_ownership_with_identity_guards(
            _load_ownership(assertions, parcel.entity_id),
            identity,
        )
        assert fresh_b.guards[0].expected_current_entity_id == corrected_b.entity_id
        assert fresh_b.guards[0].expected_identity_generation == 2
        second_repair = graph.reconcile_edges_guarded(fresh_b.outcome, fresh_b.guards)
        assert second_repair["changed"] is True
        assert second_repair["retired"] == 1
        assert scheduler.mark_projected(
            second_dirty,
            projection_hash=str(second_repair["projection_hash"]),
        ) is True
        assert scheduler.load_dirty() == []

        history = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
        )
        assert len(history) == 3
        assert {edge.source_entity_id for edge in history} == {
            historical_owner.entity_id,
            corrected_a.entity_id,
            corrected_b.entity_id,
        }
        current = [edge for edge in history if edge.active]
        assert len(current) == 1
        assert current[0].source_entity_id == corrected_b.entity_id
        assert all(edge.contributor_ids == (claim.assertion_id,) for edge in history)

        no_change = scheduler.sync(definition)
        assert no_change.changed_mapping_count == 0
        assert no_change.target_count == 0
        assert scheduler.load_dirty() == []
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_identity_dependency_mapper_dirties_only_ownership_slots_that_reference_changed_people() -> None:
    repo = _repo()
    try:
        graph, assertions, identity, scheduler = _setup(repo)

        source_a = EvidenceRef("precision-owner-a", "deed")
        source_b = EvidenceRef("precision-owner-b", "deed")
        proposal_a = person_proposal(
            source_a,
            display_name="Owner A",
            external_id="owner-a",
            valid_at="2026-01-01T00:00:00Z",
        )
        proposal_b = person_proposal(
            source_b,
            display_name="Owner B",
            external_id="owner-b",
            valid_at="2026-01-01T00:00:00Z",
        )
        owner_a = graph.record_entity(proposal_a)
        owner_b = graph.record_entity(proposal_b)
        corrected_a = graph.record_entity(
            person_proposal(
                EvidenceRef("precision-owner-a-corrected", "identity_review"),
                display_name="Owner A Corrected",
                external_id="owner-a-corrected",
                valid_at="2026-02-01T00:00:00Z",
                authority="manual",
            )
        )
        parcel_a = _parcel(graph, "A")
        parcel_b = _parcel(graph, "B")

        claim_a = ownership_assertion(
            source_a,
            investigation_id=INVESTIGATION,
            owner=owner_a,
            parcel=parcel_a,
            valid_at="2026-01-15T00:00:00Z",
        )
        claim_b = ownership_assertion(
            source_b,
            investigation_id=INVESTIGATION,
            owner=owner_b,
            parcel=parcel_b,
            valid_at="2026-01-15T00:00:00Z",
        )
        assert claim_a.source_key == proposal_a.source_key
        assert claim_b.source_key == proposal_b.source_key
        assertions.record_assertion(claim_a)
        assertions.record_assertion(claim_b)

        identity.bootstrap_current_identity()
        definition = ownership_identity_dependency_definition(assertions)
        scheduler.bootstrap(definition)

        identity.apply(
            IdentityMigrationPlan(
                migration_id="precision-owner-a-migration",
                version=1,
                entity_kind=owner_a.entity_kind,
                effective_at="2026-02-01T00:00:00Z",
                reason="only owner A identity changed",
                assignments=(
                    ReceiptIdentityAssignment(
                        source_entity_id=owner_a.entity_id,
                        source_key=proposal_a.source_key,
                        target_entity_id=corrected_a.entity_id,
                    ),
                ),
            )
        )

        sync = scheduler.sync(definition)
        assert sync.changed_mapping_count == 1
        assert sync.target_count == 1
        assert sync.dirtied_target_count == 1
        dirty = scheduler.load_dirty()
        assert len(dirty) == 1
        assert dirty[0].target.slot_key == ownership_slot_key(INVESTIGATION, parcel_a.entity_id)
        assert dirty[0].target.slot_key != ownership_slot_key(INVESTIGATION, parcel_b.entity_id)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
