from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.graph import (
    EdgeAbstention,
    EdgeSet,
    MaterializedEdge,
    Neo4jGraphMaterializer,
)
from spikes.mutation_kernel.investigation import (
    OWNS_EDGE,
    OWNERSHIP_ASSERTION,
    OWNERSHIP_MATERIALIZER,
    PARCEL_KIND,
    PERSON_KIND,
    fold_ownership,
    investigation_graph_schema,
    ownership_assertion,
    ownership_slot_key,
    parcel_proposal,
    person_proposal,
)
from spikes.mutation_kernel.kernel import EvidenceRef
from spikes.mutation_kernel.neo4j_store import Neo4jEnvelopeStore


NAMESPACE = "mutation-investigation-graph"
INVESTIGATION = "investigation:fixture"


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
    graph.activate()
    assertions.activate()
    return graph, assertions


def _entities(graph: Neo4jGraphMaterializer):
    alice = graph.record_entity(
        person_proposal(
            EvidenceRef("entity-alice", "fixture"),
            display_name="Alice Smith",
            external_id="person-alice",
            valid_at="2026-01-01T00:00:00Z",
        )
    )
    bob = graph.record_entity(
        person_proposal(
            EvidenceRef("entity-bob", "fixture"),
            display_name="Bob Jones",
            external_id="person-bob",
            valid_at="2026-01-01T00:00:00Z",
        )
    )
    parcel = graph.record_entity(
        parcel_proposal(
            EvidenceRef("entity-parcel", "county_record"),
            display_name="Parcel 123",
            county="Travis",
            account_id="P-123",
            valid_at="2026-01-01T00:00:00Z",
        )
    )
    return alice, bob, parcel


def _load_ownership(assertions: Neo4jEnvelopeStore, parcel_id: str):
    return assertions.load_assertions(
        subject_id=parcel_id,
        assertion_type=OWNERSHIP_ASSERTION,
        scope=INVESTIGATION,
        key="owner",
    )


def test_investigation_entity_identity_is_extension_owned_and_source_receipts_survive_aliases() -> None:
    repo = _repo()
    try:
        graph, _ = _setup(repo)
        first = graph.record_entity(
            person_proposal(
                EvidenceRef("alice-source-a", "deed"),
                display_name="Alice Smith",
                external_id="TX-PERSON-1",
                valid_at="2026-01-01T00:00:00Z",
            )
        )
        alias = graph.record_entity(
            person_proposal(
                EvidenceRef("alice-source-b", "campaign_filing"),
                display_name="A. Smith",
                external_id="tx-person-1",
                valid_at="2026-01-02T00:00:00Z",
            )
        )

        assert first.entity_id == alias.entity_id
        assert first.canonical_key == alias.canonical_key == "external:tx-person-1"
        assert graph.count_entities(entity_kind=PERSON_KIND) == 1
        assert graph.count_entity_receipts() == 2

        rows = repo.execute(
            """
            MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(entity:MutationEntity)
            WHERE receipt.namespace = $namespace
            RETURN entity.entity_id AS entity_id,
                   collect(receipt.observed_display_name) AS observed_names,
                   collect(receipt.source_key) AS source_keys
            """,
            {"namespace": NAMESPACE},
        )
        assert len(rows) == 1
        assert str(rows[0]["entity_id"]) == first.entity_id
        assert set(rows[0]["observed_names"]) == {"Alice Smith", "A. Smith"}
        assert len(set(rows[0]["source_keys"])) == 2
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_ownership_assertions_rebuild_actual_graph_edge_and_preserve_prior_owner_history() -> None:
    repo = _repo()
    try:
        graph, assertions = _setup(repo)
        alice, bob, parcel = _entities(graph)
        slot_key = ownership_slot_key(INVESTIGATION, parcel.entity_id)

        alice_claim = ownership_assertion(
            EvidenceRef("deed-alice", "deed"),
            investigation_id=INVESTIGATION,
            owner=alice,
            parcel=parcel,
            valid_at="2026-02-01T00:00:00Z",
            authority="trusted_tool",
        )
        assertions.record_assertion(alice_claim)

        first_outcome = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(first_outcome, EdgeSet)
        assert first_outcome.edges[0].source_entity_id == alice.entity_id
        first_write = graph.reconcile_edges(first_outcome)
        assert first_write["changed"] is True
        assert first_write["retired"] == 0
        assert graph.count_edges(active=True) == 1

        replay = graph.reconcile_edges(first_outcome)
        assert replay["changed"] is False
        assert graph.count_edges() == 1

        bob_claim = ownership_assertion(
            EvidenceRef("deed-bob", "deed"),
            investigation_id=INVESTIGATION,
            owner=bob,
            parcel=parcel,
            valid_at="2026-03-01T00:00:00Z",
            authority="trusted_tool",
        )
        assertions.record_assertion(bob_claim)

        second_outcome = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(second_outcome, EdgeSet)
        assert second_outcome.edges[0].source_entity_id == bob.entity_id
        second_write = graph.reconcile_edges(second_outcome)
        assert second_write["changed"] is True
        assert second_write["retired"] == 1
        assert graph.count_edges() == 2
        assert graph.count_edges(active=True) == 1

        history = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=slot_key,
        )
        assert len(history) == 2
        old = next(edge for edge in history if edge.source_entity_id == alice.entity_id)
        current = next(edge for edge in history if edge.source_entity_id == bob.entity_id)
        assert old.active is False
        assert old.retired_at == "2026-03-01T00:00:00Z"
        assert old.contributor_ids == (alice_claim.assertion_id,)
        assert current.active is True
        assert current.retired_at is None
        assert current.contributor_ids == (bob_claim.assertion_id,)
        assert current.edge_kind == OWNS_EDGE
        assert current.target_entity_id == parcel.entity_id

        # This is an actual Neo4j relationship, not a relation-shaped property node.
        actual = repo.execute(
            """
            MATCH (person:MutationEntity)-[r:MUTATION_EDGE]->(parcel:MutationEntity)
            WHERE r.namespace = $namespace AND r.active = true
            RETURN person.entity_kind AS source_kind,
                   r.edge_kind AS edge_kind,
                   parcel.entity_kind AS target_kind,
                   count(r) AS n
            """,
            {"namespace": NAMESPACE},
        )
        assert len(actual) == 1
        assert actual[0]["source_kind"] == PERSON_KIND
        assert actual[0]["edge_kind"] == OWNS_EDGE
        assert actual[0]["target_kind"] == PARCEL_KIND
        assert int(actual[0]["n"]) == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_conflicting_latest_ownership_abstains_and_clears_stale_current_edge() -> None:
    repo = _repo()
    try:
        graph, assertions = _setup(repo)
        alice, bob, parcel = _entities(graph)
        slot_key = ownership_slot_key(INVESTIGATION, parcel.entity_id)

        initial = ownership_assertion(
            EvidenceRef("ownership-initial", "deed"),
            investigation_id=INVESTIGATION,
            owner=alice,
            parcel=parcel,
            valid_at="2026-01-01T00:00:00Z",
        )
        assertions.record_assertion(initial)
        graph.reconcile_edges(fold_ownership(_load_ownership(assertions, parcel.entity_id)))
        assert graph.count_edges(active=True) == 1

        conflict_alice = ownership_assertion(
            EvidenceRef("ownership-conflict-a", "filing"),
            investigation_id=INVESTIGATION,
            owner=alice,
            parcel=parcel,
            valid_at="2026-04-01T00:00:00Z",
        )
        conflict_bob = ownership_assertion(
            EvidenceRef("ownership-conflict-b", "filing"),
            investigation_id=INVESTIGATION,
            owner=bob,
            parcel=parcel,
            valid_at="2026-04-01T00:00:00Z",
        )
        assertions.record_assertion(conflict_alice)
        assertions.record_assertion(conflict_bob)

        contested = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(contested, EdgeAbstention)
        assert contested.reason == "ambiguous_current_owner"
        assert set(contested.assertion_ids) == {
            conflict_alice.assertion_id,
            conflict_bob.assertion_id,
        }
        cleared = graph.reconcile_edges(contested)
        assert cleared["changed"] is True
        assert cleared["retired"] == 1
        assert graph.count_edges(active=True) == 0
        assert graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=slot_key,
            active=True,
        ) == []

        resolved = ownership_assertion(
            EvidenceRef("ownership-resolved", "deed"),
            investigation_id=INVESTIGATION,
            owner=bob,
            parcel=parcel,
            valid_at="2026-05-01T00:00:00Z",
        )
        assertions.record_assertion(resolved)
        current = fold_ownership(_load_ownership(assertions, parcel.entity_id))
        assert isinstance(current, EdgeSet)
        graph.reconcile_edges(current)
        active = graph.load_edges(
            materializer_id=OWNERSHIP_MATERIALIZER,
            slot_key=slot_key,
            active=True,
        )
        assert len(active) == 1
        assert active[0].source_entity_id == bob.entity_id
        assert active[0].contributor_ids == (resolved.assertion_id,)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_generic_edge_schema_rejects_wrong_endpoint_kind_before_graph_write() -> None:
    schema = investigation_graph_schema()
    with pytest.raises(ValueError, match="does not allow target kind"):
        schema.validate_edge(
            MaterializedEdge(
                edge_kind=OWNS_EDGE,
                source_entity_id="person-a",
                source_entity_kind=PERSON_KIND,
                target_entity_id="person-b",
                target_entity_kind=PERSON_KIND,
                valid_from="2026-01-01T00:00:00Z",
                contributor_ids=("assertion-a",),
                effective_authority="trusted_tool",
                confidence=1.0,
            )
        )
