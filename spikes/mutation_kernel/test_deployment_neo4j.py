from __future__ import annotations

import os

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.deployment import (
    CoordinatedProjectionNeo4jStore,
    StaleRegistryError,
    sync_definition_targets,
)
from spikes.mutation_kernel.kernel import EvidenceRef, Retirement, View, make_assertion
from spikes.mutation_kernel.registry import (
    ProjectionDefinition,
    ProjectionRegistry,
    ProjectionTarget,
    derive_registered_projection,
    reconcile_registered_batch,
)


ASSERTION_TYPE = "personality.conflict_signal"
VIEW_TYPE = "personality.conflict_style_view"
DEFINITION_ID = "personality.conflict-style"
SUBJECT = "agent:deployment"


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


def _assertion(source_id: str, *, value: float, when: str):
    return make_assertion(
        source=EvidenceRef(source_id, "fixture"),
        assertion_type=ASSERTION_TYPE,
        subject_id=SUBJECT,
        scope="global",
        key="conflict",
        value=value,
        valid_at=when,
    )


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        view_type=VIEW_TYPE,
        subject_id=SUBJECT,
        scope="global",
        key="conflict",
    )


def _mapper(assertion):
    if assertion.assertion_type != ASSERTION_TYPE:
        return ()
    return (_target(),)


def _removed_mapper(assertion):
    return ()


def _fold(assertions):
    return View(
        view_type=VIEW_TYPE,
        subject_id=SUBJECT,
        scope="global",
        key="conflict",
        value=sum(float(row.value) for row in assertions) / len(assertions),
        confidence=1.0,
        contributor_ids=tuple(row.assertion_id for row in assertions),
    )


def _definition(version: int, *, removed: bool = False) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id=DEFINITION_ID,
        version=version,
        input_types=(ASSERTION_TYPE,),
        view_type=VIEW_TYPE,
        mapper=_removed_mapper if removed else _mapper,
        fold=_fold,
    )


def test_shared_registry_epoch_rejects_stale_process_before_assertion_commit() -> None:
    repo = _repo()
    store = CoordinatedProjectionNeo4jStore(
        repo,
        namespace="mutation-deployment-stale-routing",
    )
    v1 = _definition(1)
    registry_v1 = ProjectionRegistry((v1,))
    v2 = _definition(2)
    registry_v2 = ProjectionRegistry((v2,))

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()

        published_v1 = store.publish_definition(v1)
        assert published_v1["action"] == "advance"
        snapshot_v1 = store.capture_registry_snapshot(registry_v1)

        first = _assertion(
            "deployment-route-a",
            value=0.8,
            when="2026-01-01T00:00:00Z",
        )
        written = store.record_assertion_with_snapshot(first, registry_v1, snapshot_v1)
        assert written["created"] is True
        assert store.count_assertions() == 1

        published_v2 = store.publish_definition(v2)
        assert published_v2["action"] == "advance"
        assert int(published_v2["epoch"]) > snapshot_v1.epoch

        second = _assertion(
            "deployment-route-b",
            value=-0.4,
            when="2026-01-02T00:00:00Z",
        )
        with pytest.raises(StaleRegistryError):
            store.record_assertion_with_snapshot(second, registry_v1, snapshot_v1)

        # The epoch check is inside the assertion/write transaction: stale routing wrote nothing.
        assert store.count_assertions() == 1

        snapshot_v2 = store.capture_registry_snapshot(registry_v2)
        written_v2 = store.record_assertion_with_snapshot(
            second,
            registry_v2,
            snapshot_v2,
        )
        assert written_v2["created"] is True
        assert written_v2["epoch"] == snapshot_v2.epoch
        assert store.count_assertions() == 2
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_definition_upgrade_retires_target_it_no_longer_maps_and_blocks_old_worker() -> None:
    repo = _repo()
    store = CoordinatedProjectionNeo4jStore(
        repo,
        namespace="mutation-deployment-removed-target",
    )
    v1 = _definition(1)
    registry_v1 = ProjectionRegistry((v1,))
    v2 = _definition(2, removed=True)
    registry_v2 = ProjectionRegistry((v2,))

    try:
        repo.execute("MATCH (n) DETACH DELETE n")
        store.activate()
        store.publish_definition(v1)
        snapshot_v1 = store.capture_registry_snapshot(registry_v1)

        first = _assertion(
            "deployment-retire-a",
            value=0.9,
            when="2026-02-01T00:00:00Z",
        )
        store.record_assertion_with_snapshot(first, registry_v1, snapshot_v1)
        initial = reconcile_registered_batch(store, registry_v1)
        assert len(initial) == 1
        assert isinstance(initial[0].outcome, View)
        assert store.count_views() == 1

        # Create generation 2 and let an old v1 worker derive it without writing yet.
        second = _assertion(
            "deployment-retire-b",
            value=0.5,
            when="2026-02-02T00:00:00Z",
        )
        store.record_assertion_with_snapshot(second, registry_v1, snapshot_v1)
        stale_dirty = store.load_registered_dirty()
        assert len(stale_dirty) == 1
        assert stale_dirty[0].definition_version == 1
        stale_derivation = derive_registered_projection(store, stale_dirty[0], v1)
        assert isinstance(stale_derivation.outcome, View)

        store.publish_definition(v2)
        snapshot_v2 = store.capture_registry_snapshot(registry_v2)
        synced = sync_definition_targets(store, v2, registry_v2, snapshot_v2)
        assert synced.current_target_count == 0
        assert synced.removed_target_count == 1
        assert synced.retired_target_count == 1

        outcomes = store.load_outcomes(
            subject_id=SUBJECT,
            view_type=VIEW_TYPE,
            scope="global",
            key="conflict",
        )
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], Retirement)
        assert outcomes[0].reason == "definition_no_longer_maps_target"
        assert store.count_views() == 0
        assert store.count_registered_dirty() == 0

        # A worker that began under v1 cannot bring the removed View back after v2 retirement.
        late = store.record_registered_outcome(stale_derivation.outcome, stale_dirty[0])
        assert late["applied"] is False
        after_late = store.load_outcomes(
            subject_id=SUBJECT,
            view_type=VIEW_TYPE,
            scope="global",
            key="conflict",
        )
        assert after_late == outcomes
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
