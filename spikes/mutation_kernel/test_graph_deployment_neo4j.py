from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.graph import (
    EdgeKind,
    EdgeSet,
    EntityKind,
    EntityProposal,
    GraphSchema,
    MaterializedEdge,
    Neo4jGraphMaterializer,
)
from spikes.mutation_kernel.graph_deployment import (
    EdgeKindContract,
    EntityKindContract,
    GraphDeploymentCoordinator,
    GraphExtensionDefinition,
    GraphMaterializerContract,
    StaleGraphRegistryError,
)
from spikes.mutation_kernel.identity import IdentityMigrationPlan, ReceiptIdentityAssignment
from spikes.mutation_kernel.identity_migration_fence import (
    FencedGenerationalNeo4jIdentityEvolution,
)
from spikes.mutation_kernel.kernel import EvidenceRef


NAMESPACE = "mutation-graph-deployment"
ENTITY_KIND = "deployment.person"
EDGE_KIND = "deployment.related"
MATERIALIZER = "deployment.current_relationship"
DEFINITION_ID = "deployment.reference-extension"


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


def _resolver_v1(proposal: EntityProposal) -> str:
    return str(proposal.property_map["external_id"]).strip().lower()


def _resolver_v2(proposal: EntityProposal) -> str:
    # Deliberately different semantics so a stale resolver would produce a different durable ID.
    return "v2:" + str(proposal.property_map["external_id"]).strip().lower()


def _schema(version: int) -> GraphSchema:
    resolver = _resolver_v1 if version == 1 else _resolver_v2
    return GraphSchema(
        entity_kinds=(
            EntityKind(
                kind_id=ENTITY_KIND,
                version=version,
                identity_resolver=resolver,
            ),
        ),
        edge_kinds=(
            EdgeKind(
                kind_id=EDGE_KIND,
                version=version,
                source_kinds=(ENTITY_KIND,),
                target_kinds=(ENTITY_KIND,),
                temporal=True,
                provenance_required=True,
            ),
        ),
    )


def _definition(version: int) -> GraphExtensionDefinition:
    return GraphExtensionDefinition(
        definition_id=DEFINITION_ID,
        version=version,
        entity_kinds=(EntityKindContract(kind_id=ENTITY_KIND, version=version),),
        edge_kinds=(
            EdgeKindContract(
                kind_id=EDGE_KIND,
                version=version,
                source_kinds=(ENTITY_KIND,),
                target_kinds=(ENTITY_KIND,),
                temporal=True,
                provenance_required=True,
            ),
        ),
        materializers=(
            GraphMaterializerContract(
                materializer_id=MATERIALIZER,
                version=version,
            ),
        ),
    )


def _proposal(source_id: str, external_id: str, display_name: str) -> EntityProposal:
    return EntityProposal(
        entity_kind=ENTITY_KIND,
        display_name=display_name,
        properties=(("external_id", external_id),),
        source=EvidenceRef(source_id, "deployment_fixture"),
        valid_at="2026-01-01T00:00:00Z",
        learned_at="2026-01-01T00:00:00Z",
        authority="manual",
    )


def _setup(repo: Neo4jRepository):
    repo.execute("MATCH (n) DETACH DELETE n")
    coordinator = GraphDeploymentCoordinator(repo, namespace=NAMESPACE)
    graph_v1 = Neo4jGraphMaterializer(repo, namespace=NAMESPACE, schema=_schema(1))
    graph_v2 = Neo4jGraphMaterializer(repo, namespace=NAMESPACE, schema=_schema(2))
    coordinator.activate()
    graph_v1.activate()
    graph_v2.activate()
    return coordinator, graph_v1, graph_v2


def test_stale_entity_resolver_cannot_create_entity_after_new_graph_definition_publishes() -> None:
    repo = _repo()
    try:
        coordinator, graph_v1, graph_v2 = _setup(repo)
        definition_v1 = _definition(1)
        definition_v2 = _definition(2)

        published_v1 = coordinator.publish_definition(definition_v1)
        assert published_v1["action"] == "advance"
        snapshot_v1 = coordinator.capture_snapshot((definition_v1,))

        first = coordinator.run_fenced(
            (definition_v1,),
            snapshot_v1,
            lambda: graph_v1.record_entity(
                _proposal("entity-v1", "original", "Original")
            ),
        )
        assert graph_v1.count_entities(entity_kind=ENTITY_KIND) == 1

        stale_proposal = _proposal("entity-stale", "shared", "Shared")
        stale_id = graph_v1.schema.resolve_entity(stale_proposal).entity_id
        fresh_id = graph_v2.schema.resolve_entity(stale_proposal).entity_id
        assert stale_id != fresh_id

        published_v2 = coordinator.publish_definition(definition_v2)
        assert published_v2["action"] == "advance"
        assert int(published_v2["epoch"]) > int(published_v1["epoch"])

        with pytest.raises(StaleGraphRegistryError, match="epoch changed"):
            coordinator.run_fenced(
                (definition_v1,),
                snapshot_v1,
                lambda: graph_v1.record_entity(stale_proposal),
            )
        assert graph_v1.count_entities(entity_kind=ENTITY_KIND) == 1

        with pytest.raises(StaleGraphRegistryError, match="do not match"):
            coordinator.capture_snapshot((definition_v1,))

        snapshot_v2 = coordinator.capture_snapshot((definition_v2,))
        persisted_v2 = coordinator.run_fenced(
            (definition_v2,),
            snapshot_v2,
            lambda: graph_v2.record_entity(stale_proposal),
        )
        assert persisted_v2.entity_id == fresh_id
        assert persisted_v2.entity_id != stale_id
        assert persisted_v2.entity_id != first.entity_id
        assert graph_v2.count_entities(entity_kind=ENTITY_KIND) == 2
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_prepared_identity_migration_cannot_cross_graph_definition_epoch() -> None:
    repo = _repo()
    try:
        coordinator, graph_v1, graph_v2 = _setup(repo)
        definition_v1 = _definition(1)
        definition_v2 = _definition(2)
        coordinator.publish_definition(definition_v1)
        snapshot_v1 = coordinator.capture_snapshot((definition_v1,))

        original_proposal = _proposal("migration-original", "migration-original", "Original")
        target_proposal = _proposal("migration-target", "migration-target", "Target")
        original = coordinator.run_fenced(
            (definition_v1,), snapshot_v1, lambda: graph_v1.record_entity(original_proposal)
        )
        target = coordinator.run_fenced(
            (definition_v1,), snapshot_v1, lambda: graph_v1.record_entity(target_proposal)
        )
        identity_v1 = FencedGenerationalNeo4jIdentityEvolution(graph_v1)
        identity_v1.activate()

        plan = IdentityMigrationPlan(
            migration_id="deployment-fenced-migration",
            version=1,
            entity_kind=ENTITY_KIND,
            effective_at="2026-02-01T00:00:00Z",
            reason="deployment fixture migration",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=original.entity_id,
                    source_key=original_proposal.source_key,
                    target_entity_id=target.entity_id,
                ),
            ),
        )
        prepared_v1 = coordinator.run_fenced(
            (definition_v1,),
            snapshot_v1,
            lambda: identity_v1.prepare(plan),
        )
        assert prepared_v1.assignments[0].expected_identity_generation == 0

        coordinator.publish_definition(definition_v2)
        with pytest.raises(StaleGraphRegistryError, match="epoch changed"):
            coordinator.run_fenced(
                (definition_v1,),
                snapshot_v1,
                lambda: identity_v1.apply_prepared(prepared_v1),
            )

        unchanged = identity_v1.resolve_receipt(
            entity_kind=ENTITY_KIND,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert unchanged is not None
        assert unchanged.entity.entity_id == original.entity_id
        assert unchanged.identity_generation == 0
        migration_rows = repo.execute(
            "MATCH (m:MutationIdentityMigration {namespace:$namespace}) RETURN count(m) AS n",
            {"namespace": NAMESPACE},
        )
        assert int(migration_rows[0]["n"]) == 0

        snapshot_v2 = coordinator.capture_snapshot((definition_v2,))
        identity_v2 = FencedGenerationalNeo4jIdentityEvolution(graph_v2)
        identity_v2.activate()
        # A fresh process must re-prepare under the new graph-definition snapshot.
        prepared_v2 = coordinator.run_fenced(
            (definition_v2,),
            snapshot_v2,
            lambda: identity_v2.prepare(plan),
        )
        applied = coordinator.run_fenced(
            (definition_v2,),
            snapshot_v2,
            lambda: identity_v2.apply_prepared(prepared_v2),
        )
        assert applied.replay is False
        changed = identity_v2.resolve_receipt(
            entity_kind=ENTITY_KIND,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert changed is not None
        assert changed.entity.entity_id == target.entity_id
        assert changed.identity_generation == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_stale_edge_materializer_cannot_write_topology_after_definition_upgrade() -> None:
    repo = _repo()
    try:
        coordinator, graph_v1, graph_v2 = _setup(repo)
        definition_v1 = _definition(1)
        definition_v2 = _definition(2)
        coordinator.publish_definition(definition_v1)
        snapshot_v1 = coordinator.capture_snapshot((definition_v1,))

        source = coordinator.run_fenced(
            (definition_v1,),
            snapshot_v1,
            lambda: graph_v1.record_entity(_proposal("edge-source", "source", "Source")),
        )
        target = coordinator.run_fenced(
            (definition_v1,),
            snapshot_v1,
            lambda: graph_v1.record_entity(_proposal("edge-target", "target", "Target")),
        )
        outcome = EdgeSet(
            materializer_id=MATERIALIZER,
            slot_key="relationship-slot",
            effective_at="2026-02-01T00:00:00Z",
            edges=(
                MaterializedEdge(
                    edge_kind=EDGE_KIND,
                    source_entity_id=source.entity_id,
                    source_entity_kind=ENTITY_KIND,
                    target_entity_id=target.entity_id,
                    target_entity_kind=ENTITY_KIND,
                    valid_from="2026-02-01T00:00:00Z",
                    contributor_ids=("assertion-relationship-1",),
                    effective_authority="manual",
                    confidence=1.0,
                ),
            ),
            assertion_ids=("assertion-relationship-1",),
        )

        coordinator.publish_definition(definition_v2)
        with pytest.raises(StaleGraphRegistryError, match="epoch changed"):
            coordinator.run_fenced(
                (definition_v1,),
                snapshot_v1,
                lambda: graph_v1.reconcile_edges(outcome),
            )
        assert graph_v1.count_edges(active=True) == 0

        snapshot_v2 = coordinator.capture_snapshot((definition_v2,))
        write = coordinator.run_fenced(
            (definition_v2,),
            snapshot_v2,
            lambda: graph_v2.reconcile_edges(outcome),
        )
        assert write["changed"] is True
        assert graph_v2.count_edges(active=True) == 1
        current = graph_v2.load_edges(
            materializer_id=MATERIALIZER,
            slot_key="relationship-slot",
            active=True,
        )
        assert len(current) == 1
        assert current[0].source_entity_id == source.entity_id
        assert current[0].target_entity_id == target.entity_id
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
