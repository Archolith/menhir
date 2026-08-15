from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.graph import EdgeAbstention, EdgeSet, Neo4jGraphMaterializer
from spikes.mutation_kernel.identity import (
    IdentityMigrationPlan,
    Neo4jIdentityEvolution,
    ReceiptIdentityAssignment,
)
from spikes.mutation_kernel.investigation import (
    OWNERSHIP_ASSERTION,
    OWNERSHIP_MATERIALIZER,
    PERSON_KIND,
    fold_ownership,
    investigation_graph_schema,
    ownership_assertion,
    ownership_slot_key,
    parcel_proposal,
    person_proposal,
)
from spikes.mutation_kernel.investigation_identity import fold_ownership_with_identity
from spikes.mutation_kernel.kernel import EvidenceRef
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore


NAMESPACE = "mutation-investigation-identity"
INVESTIGATION = "investigation:identity-evolution"


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
    graph = Neo4jGraphMaterializer(
        repo,
        namespace=NAMESPACE,
        schema=investigation_graph_schema(),
    )
    assertions = Neo4jEnvelopeStore(repo, namespace=NAMESPACE)
    identity = Neo4jIdentityEvolution(graph)
    graph.activate()
    assertions.activate()
    identity.activate()
    return graph, assertions, identity


def _parcel(graph: Neo4jGraphMaterializer):
    return graph.record_entity(
        parcel_proposal(
            EvidenceRef("identity-parcel", "county_record"),
            display_name="Parcel 123",
            county="Travis",
            account_id="P-123",
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


def _resolver(identity: Neo4jIdentityEvolution):
    def resolve(kind: str, entity_id: str, source_key: str):
        return identity.resolve(
            entity_kind=kind,
            entity_id=entity_id,
            source_key=source_key,
        ).entity

    return resolve


def test_identity_merge_reassigns_receipts_and_refolds_conflicting_owners() -> None:
    repo = _repo()
    try:
        graph, assertions, identity = _setup(repo)
        source_a = EvidenceRef("merge-person-a", "deed")
        source_b = EvidenceRef("merge-person-b", "campaign_filing")

        proposal_a = person_proposal(
            source_a,
            display_name="Alice Smith",
            external_id="legacy-alice-a",
            valid_at="2026-01-01T00:00:00Z",
        )
        proposal_b = person_proposal(
            source_b,
            display_name="A. Smith",
            external_id="legacy-alice-b",
            valid_at="2026-01-01T00:00:00Z",
        )
        alice_a = graph.record_entity(proposal_a)
        alice_b = graph.record_entity(proposal_b)
        parcel = _parcel(graph)

        claim_a = ownership_assertion(
            source_a,
            investigation_id=INVESTIGATION,
            owner=alice_a,
            parcel=parcel,
            valid_at="2026-03-01T00:00:00Z",
        )
        claim_b = ownership_assertion(
            source_b,
            investigation_id=INVESTIGATION,
            owner=alice_b,
            parcel=parcel,
            valid_at="2026-03-01T00:00:00Z",
        )
        assert claim_a.source_key == proposal_a.source_key
        assert claim_b.source_key == proposal_b.source_key
        assertions.record_assertion(claim_a)
        assertions.record_assertion(claim_b)

        before = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(before, EdgeAbstention)
        assert before.reason == "ambiguous_current_owner"

        merged = graph.record_entity(
            person_proposal(
                EvidenceRef("merge-canonical-person", "identity_review"),
                display_name="Alice Smith",
                external_id="canonical-alice",
                valid_at="2026-04-01T00:00:00Z",
                authority="manual",
            )
        )
        plan = IdentityMigrationPlan(
            migration_id="merge-alice-legacy-identities",
            version=1,
            entity_kind=PERSON_KIND,
            effective_at="2026-04-01T00:00:00Z",
            reason="two legacy person identities were determined to be the same person",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=alice_a.entity_id,
                    source_key=proposal_a.source_key,
                    target_entity_id=merged.entity_id,
                ),
                ReceiptIdentityAssignment(
                    source_entity_id=alice_b.entity_id,
                    source_key=proposal_b.source_key,
                    target_entity_id=merged.entity_id,
                ),
            ),
        )
        applied = identity.apply(plan)
        assert applied.replay is False
        assert applied.assignment_count == 2

        resolved_a = identity.resolve(
            entity_kind=PERSON_KIND,
            entity_id=alice_a.entity_id,
        )
        resolved_b = identity.resolve(
            entity_kind=PERSON_KIND,
            entity_id=alice_b.entity_id,
        )
        assert resolved_a.entity is not None
        assert resolved_b.entity is not None
        assert resolved_a.entity.entity_id == merged.entity_id
        assert resolved_b.entity.entity_id == merged.entity_id

        after = fold_ownership_with_identity(
            _load_ownership(assertions, parcel.entity_id),
            _resolver(identity),
        )
        assert isinstance(after, EdgeSet)
        assert after.edges[0].source_entity_id == merged.entity_id
        assert set(after.edges[0].contributor_ids) == {
            claim_a.assertion_id,
            claim_b.assertion_id,
        }
        graph.reconcile_edges(after)
        active = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
            active=True,
        )
        assert len(active) == 1
        assert active[0].source_entity_id == merged.entity_id

        original_rows = repo.execute(
            """
            MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(original:MutationEntity)
            WHERE receipt.namespace=$namespace
              AND receipt.source_key IN $source_keys
            OPTIONAL MATCH (receipt)-[:CURRENT_IDENTITY]->(current:MutationEntity)
            RETURN receipt.source_key AS source_key,
                   original.entity_id AS original_id,
                   current.entity_id AS current_id
            """,
            {
                "namespace": NAMESPACE,
                "source_keys": [proposal_a.source_key, proposal_b.source_key],
            },
        )
        assert {
            (str(row["source_key"]), str(row["original_id"]), str(row["current_id"]))
            for row in original_rows
        } == {
            (proposal_a.source_key, alice_a.entity_id, merged.entity_id),
            (proposal_b.source_key, alice_b.entity_id, merged.entity_id),
        }

        active_flags = repo.execute(
            """
            MATCH (entity:MutationEntity)
            WHERE entity.namespace=$namespace
              AND entity.entity_id IN $entity_ids
            RETURN entity.entity_id AS entity_id,
                   coalesce(entity.identity_active,true) AS active
            """,
            {
                "namespace": NAMESPACE,
                "entity_ids": [alice_a.entity_id, alice_b.entity_id, merged.entity_id],
            },
        )
        flags = {str(row["entity_id"]): bool(row["active"]) for row in active_flags}
        assert flags[alice_a.entity_id] is False
        assert flags[alice_b.entity_id] is False
        assert flags[merged.entity_id] is True

        replay = identity.apply(plan)
        assert replay.replay is True
        migration_count = repo.execute(
            """
            MATCH (m:MutationIdentityMigration {namespace:$namespace})
            RETURN count(m) AS n
            """,
            {"namespace": NAMESPACE},
        )
        assert int(migration_count[0]["n"]) == 1

        conflicting_plan = IdentityMigrationPlan(
            migration_id=plan.migration_id,
            version=1,
            entity_kind=PERSON_KIND,
            effective_at=plan.effective_at,
            reason=plan.reason,
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=alice_a.entity_id,
                    source_key=proposal_a.source_key,
                    target_entity_id=alice_a.entity_id,
                ),
                ReceiptIdentityAssignment(
                    source_entity_id=alice_b.entity_id,
                    source_key=proposal_b.source_key,
                    target_entity_id=merged.entity_id,
                ),
            ),
        )
        with pytest.raises(ValueError, match="ID collision"):
            identity.apply(conflicting_plan)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_identity_split_retargets_current_edge_without_rewriting_assertion_history() -> None:
    repo = _repo()
    try:
        graph, assertions, identity = _setup(repo)
        source_a = EvidenceRef("split-source-a", "deed")
        source_b = EvidenceRef("split-source-b", "filing")
        proposal_a = person_proposal(
            source_a,
            display_name="John Smith",
            external_id="conflated-john",
            valid_at="2026-01-01T00:00:00Z",
        )
        proposal_b = person_proposal(
            source_b,
            display_name="J. Smith",
            external_id="CONFLATED-JOHN",
            valid_at="2026-01-15T00:00:00Z",
        )
        conflated_a = graph.record_entity(proposal_a)
        conflated_b = graph.record_entity(proposal_b)
        assert conflated_a.entity_id == conflated_b.entity_id
        conflated = conflated_a
        parcel = _parcel(graph)

        old_claim = ownership_assertion(
            source_a,
            investigation_id=INVESTIGATION,
            owner=conflated,
            parcel=parcel,
            valid_at="2026-01-01T00:00:00Z",
        )
        latest_claim = ownership_assertion(
            source_b,
            investigation_id=INVESTIGATION,
            owner=conflated,
            parcel=parcel,
            valid_at="2026-02-01T00:00:00Z",
        )
        assertions.record_assertion(old_claim)
        assertions.record_assertion(latest_claim)

        initial = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(initial, EdgeSet)
        assert initial.edges[0].source_entity_id == conflated.entity_id
        graph.reconcile_edges(initial)

        john_a = graph.record_entity(
            person_proposal(
                EvidenceRef("split-target-a", "identity_review"),
                display_name="John Smith A",
                external_id="john-a",
                valid_at="2026-03-01T00:00:00Z",
                authority="manual",
            )
        )
        john_b = graph.record_entity(
            person_proposal(
                EvidenceRef("split-target-b", "identity_review"),
                display_name="John Smith B",
                external_id="john-b",
                valid_at="2026-03-01T00:00:00Z",
                authority="manual",
            )
        )
        plan = IdentityMigrationPlan(
            migration_id="split-conflated-john",
            version=1,
            entity_kind=PERSON_KIND,
            effective_at="2026-03-01T00:00:00Z",
            reason="two source receipts previously collapsed into one person were separated",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=conflated.entity_id,
                    source_key=proposal_a.source_key,
                    target_entity_id=john_a.entity_id,
                ),
                ReceiptIdentityAssignment(
                    source_entity_id=conflated.entity_id,
                    source_key=proposal_b.source_key,
                    target_entity_id=john_b.entity_id,
                ),
            ),
        )
        identity.apply(plan)

        global_resolution = identity.resolve(
            entity_kind=PERSON_KIND,
            entity_id=conflated.entity_id,
        )
        assert global_resolution.status == "ambiguous"
        assert {row.entity_id for row in global_resolution.entities} == {
            john_a.entity_id,
            john_b.entity_id,
        }
        source_a_resolution = identity.resolve(
            entity_kind=PERSON_KIND,
            entity_id=conflated.entity_id,
            source_key=proposal_a.source_key,
        )
        source_b_resolution = identity.resolve(
            entity_kind=PERSON_KIND,
            entity_id=conflated.entity_id,
            source_key=proposal_b.source_key,
        )
        assert source_a_resolution.entity is not None
        assert source_b_resolution.entity is not None
        assert source_a_resolution.entity.entity_id == john_a.entity_id
        assert source_b_resolution.entity.entity_id == john_b.entity_id

        rebuilt = fold_ownership_with_identity(
            _load_ownership(assertions, parcel.entity_id),
            _resolver(identity),
        )
        assert isinstance(rebuilt, EdgeSet)
        assert rebuilt.edges[0].source_entity_id == john_b.entity_id
        assert rebuilt.edges[0].contributor_ids == (latest_claim.assertion_id,)
        write = graph.reconcile_edges(rebuilt)
        assert write["changed"] is True
        assert write["retired"] == 1

        history = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=ownership_slot_key(INVESTIGATION, parcel.entity_id),
        )
        assert len(history) == 2
        historical = next(
            edge for edge in history
            if edge.source_entity_id == conflated.entity_id
        )
        current = next(
            edge for edge in history
            if edge.source_entity_id == john_b.entity_id
        )
        assert historical.active is False
        assert historical.contributor_ids == (latest_claim.assertion_id,)
        assert current.active is True
        assert current.contributor_ids == (latest_claim.assertion_id,)

        loaded = _load_ownership(assertions, parcel.entity_id)
        persisted_latest = next(
            row for row in loaded
            if row.assertion_id == latest_claim.assertion_id
        )
        assert persisted_latest.value["owner_entity_id"] == conflated.entity_id

        receipt_rows = identity.receipt_resolution_rows(
            entity_kind=PERSON_KIND,
            source_entity_id=conflated.entity_id,
        )
        assert {
            (row["source_key"], row["original_entity_id"], row["current_entity_id"])
            for row in receipt_rows
        } == {
            (proposal_a.source_key, conflated.entity_id, john_a.entity_id),
            (proposal_b.source_key, conflated.entity_id, john_b.entity_id),
        }

        old_active = repo.execute(
            """
            MATCH (entity:MutationEntity)
            WHERE entity.namespace=$namespace AND entity.entity_id=$entity_id
            RETURN coalesce(entity.identity_active,true) AS active
            """,
            {"namespace": NAMESPACE, "entity_id": conflated.entity_id},
        )
        assert bool(old_active[0]["active"]) is False
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_identity_aware_fold_abstains_when_latest_assertion_has_no_receipt_resolution() -> None:
    repo = _repo()
    try:
        graph, assertions, identity = _setup(repo)
        source = EvidenceRef("unresolved-source", "deed")
        person = graph.record_entity(
            person_proposal(
                EvidenceRef("different-entity-source", "registry"),
                display_name="Unknown Person",
                external_id="unknown-person",
                valid_at="2026-01-01T00:00:00Z",
            )
        )
        parcel = _parcel(graph)
        claim = ownership_assertion(
            source,
            investigation_id=INVESTIGATION,
            owner=person,
            parcel=parcel,
            valid_at="2026-02-01T00:00:00Z",
        )
        assertions.record_assertion(claim)

        outcome = fold_ownership_with_identity(
            _load_ownership(assertions, parcel.entity_id),
            _resolver(identity),
        )
        assert isinstance(outcome, EdgeAbstention)
        assert outcome.reason == "unresolved_current_owner_identity"
        assert outcome.assertion_ids == (claim.assertion_id,)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
