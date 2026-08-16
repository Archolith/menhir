from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import threading

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository

from spikes.mutation_kernel.graph import Neo4jGraphMaterializer
from spikes.mutation_kernel.identity import (
    IdentityMigrationPlan,
    ReceiptIdentityAssignment,
)
from spikes.mutation_kernel.identity_migration_fence import (
    FencedGenerationalNeo4jIdentityEvolution,
)
from spikes.mutation_kernel.investigation import (
    PERSON_KIND,
    investigation_graph_schema,
    person_proposal,
)
from spikes.mutation_kernel.kernel import EvidenceRef


NAMESPACE = "mutation-identity-migration-fence"


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
    identity = FencedGenerationalNeo4jIdentityEvolution(graph)
    graph.activate()
    identity.activate()
    return graph, identity


def _person(
    graph: Neo4jGraphMaterializer,
    source_id: str,
    external_id: str,
    display_name: str,
):
    source = EvidenceRef(source_id, "identity_record")
    proposal = person_proposal(
        source,
        display_name=display_name,
        external_id=external_id,
        valid_at="2026-01-01T00:00:00Z",
        authority="manual",
    )
    return source, proposal, graph.record_entity(proposal)


def _plan(
    *,
    migration_id: str,
    source_entity_id: str,
    source_key: str,
    target_entity_id: str,
    effective_at: str,
) -> IdentityMigrationPlan:
    return IdentityMigrationPlan(
        migration_id=migration_id,
        version=1,
        entity_kind=PERSON_KIND,
        effective_at=effective_at,
        reason=f"test migration {migration_id}",
        assignments=(
            ReceiptIdentityAssignment(
                source_entity_id=source_entity_id,
                source_key=source_key,
                target_entity_id=target_entity_id,
            ),
        ),
    )


def _state(
    identity: FencedGenerationalNeo4jIdentityEvolution,
    *,
    entity_id: str,
    source_key: str,
):
    state = identity.resolve_receipt(
        entity_kind=PERSON_KIND,
        entity_id=entity_id,
        source_key=source_key,
    )
    assert state is not None
    return state


def _migration_count(repo: Neo4jRepository) -> int:
    rows = repo.execute(
        "MATCH (m:MutationIdentityMigration {namespace:$namespace}) RETURN count(m) AS n",
        {"namespace": NAMESPACE},
    )
    return int(rows[0].get("n", 0) or 0)


def _assert_no_lock_markers(repo: Neo4jRepository) -> None:
    rows = repo.execute(
        """
        MATCH (receipt:MutationEntityReceipt {namespace:$namespace})
        WHERE receipt.mutation_identity_migration_lock IS NOT NULL
        RETURN count(receipt) AS n
        """,
        {"namespace": NAMESPACE},
    )
    assert int(rows[0].get("n", 0) or 0) == 0


def test_stale_prepared_migration_fails_after_another_plan_advances_receipt() -> None:
    repo = _repo()
    try:
        graph, identity = _setup(repo)
        _, original_proposal, original = _person(
            graph, "stale-original", "stale-original", "Original Person"
        )
        _, _, target_a = _person(graph, "stale-target-a", "stale-a", "Target A")
        _, _, target_b = _person(graph, "stale-target-b", "stale-b", "Target B")

        plan_a = _plan(
            migration_id="stale-plan-a",
            source_entity_id=original.entity_id,
            source_key=original_proposal.source_key,
            target_entity_id=target_a.entity_id,
            effective_at="2026-02-01T00:00:00Z",
        )
        plan_b = _plan(
            migration_id="stale-plan-b",
            source_entity_id=original.entity_id,
            source_key=original_proposal.source_key,
            target_entity_id=target_b.entity_id,
            effective_at="2026-02-01T00:00:00Z",
        )
        prepared_a = identity.prepare(plan_a)
        prepared_b = identity.prepare(plan_b)
        assert prepared_a.assignments[0].expected_identity_generation == 0
        assert prepared_b.assignments[0].expected_identity_generation == 0

        applied_a = identity.apply_prepared(prepared_a)
        assert applied_a.replay is False
        state_a = _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert state_a.entity.entity_id == target_a.entity_id
        assert state_a.identity_generation == 1

        with pytest.raises(ValueError, match="stale identity migration"):
            identity.apply_prepared(prepared_b)
        still_a = _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert still_a.entity.entity_id == target_a.entity_id
        assert still_a.identity_generation == 1
        assert _migration_count(repo) == 1
        _assert_no_lock_markers(repo)

        replay_a = identity.apply_prepared(prepared_a)
        assert replay_a.replay is True
        assert _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        ).identity_generation == 1

        refreshed_b = identity.prepare(plan_b)
        assert refreshed_b.assignments[0].expected_identity_generation == 1
        applied_b = identity.apply_prepared(refreshed_b)
        assert applied_b.replay is False
        final = _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert final.entity.entity_id == target_b.entity_id
        assert final.identity_generation == 2
        assert _migration_count(repo) == 2
        _assert_no_lock_markers(repo)
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_multi_receipt_plan_is_atomic_when_one_prepared_generation_is_stale() -> None:
    repo = _repo()
    try:
        graph, identity = _setup(repo)
        _, proposal_one, original_one = _person(
            graph, "multi-original-one", "multi-original-one", "Original One"
        )
        _, proposal_two, original_two = _person(
            graph, "multi-original-two", "multi-original-two", "Original Two"
        )
        _, _, target_one = _person(graph, "multi-target-one", "multi-target-one", "Target One")
        _, _, target_two = _person(graph, "multi-target-two", "multi-target-two", "Target Two")
        _, _, alternate_one = _person(
            graph, "multi-alternate-one", "multi-alternate-one", "Alternate One"
        )

        combined_plan = IdentityMigrationPlan(
            migration_id="combined-two-receipt-plan",
            version=1,
            entity_kind=PERSON_KIND,
            effective_at="2026-03-01T00:00:00Z",
            reason="move two receipts atomically",
            assignments=(
                ReceiptIdentityAssignment(
                    source_entity_id=original_one.entity_id,
                    source_key=proposal_one.source_key,
                    target_entity_id=target_one.entity_id,
                ),
                ReceiptIdentityAssignment(
                    source_entity_id=original_two.entity_id,
                    source_key=proposal_two.source_key,
                    target_entity_id=target_two.entity_id,
                ),
            ),
        )
        stale_combined = identity.prepare(combined_plan)
        assert [row.expected_identity_generation for row in stale_combined.assignments] == [0, 0]

        advance_one = _plan(
            migration_id="advance-first-receipt-only",
            source_entity_id=original_one.entity_id,
            source_key=proposal_one.source_key,
            target_entity_id=alternate_one.entity_id,
            effective_at="2026-02-01T00:00:00Z",
        )
        identity.apply_prepared(identity.prepare(advance_one))
        assert _state(
            identity,
            entity_id=original_one.entity_id,
            source_key=proposal_one.source_key,
        ).identity_generation == 1
        second_before = _state(
            identity,
            entity_id=original_two.entity_id,
            source_key=proposal_two.source_key,
        )
        assert second_before.entity.entity_id == original_two.entity_id
        assert second_before.identity_generation == 0

        with pytest.raises(ValueError, match="stale identity migration"):
            identity.apply_prepared(stale_combined)

        first_after_failed = _state(
            identity,
            entity_id=original_one.entity_id,
            source_key=proposal_one.source_key,
        )
        second_after_failed = _state(
            identity,
            entity_id=original_two.entity_id,
            source_key=proposal_two.source_key,
        )
        assert first_after_failed.entity.entity_id == alternate_one.entity_id
        assert first_after_failed.identity_generation == 1
        assert second_after_failed.entity.entity_id == original_two.entity_id
        assert second_after_failed.identity_generation == 0
        assert _migration_count(repo) == 1
        _assert_no_lock_markers(repo)

        fresh_combined = identity.prepare(combined_plan)
        generations = {
            row.source_entity_id: row.expected_identity_generation
            for row in fresh_combined.assignments
        }
        assert generations[original_one.entity_id] == 1
        assert generations[original_two.entity_id] == 0
        applied = identity.apply_prepared(fresh_combined)
        assert applied.assignment_count == 2
        assert applied.replay is False

        final_one = _state(
            identity,
            entity_id=original_one.entity_id,
            source_key=proposal_one.source_key,
        )
        final_two = _state(
            identity,
            entity_id=original_two.entity_id,
            source_key=proposal_two.source_key,
        )
        assert final_one.entity.entity_id == target_one.entity_id
        assert final_one.identity_generation == 2
        assert final_two.entity.entity_id == target_two.entity_id
        assert final_two.identity_generation == 1
        assert _migration_count(repo) == 2
        _assert_no_lock_markers(repo)

        replay = identity.apply_prepared(fresh_combined)
        assert replay.replay is True
        assert _state(
            identity,
            entity_id=original_one.entity_id,
            source_key=proposal_one.source_key,
        ).identity_generation == 2
        assert _state(
            identity,
            entity_id=original_two.entity_id,
            source_key=proposal_two.source_key,
        ).identity_generation == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()


def test_two_real_concurrent_migration_writers_allow_exactly_one_generation_zero_plan() -> None:
    repo = _repo()
    try:
        graph, identity = _setup(repo)
        _, original_proposal, original = _person(
            graph, "concurrent-original", "concurrent-original", "Concurrent Original"
        )
        _, _, target_a = _person(
            graph, "concurrent-target-a", "concurrent-a", "Concurrent A"
        )
        _, _, target_b = _person(
            graph, "concurrent-target-b", "concurrent-b", "Concurrent B"
        )

        prepared_a = identity.prepare(
            _plan(
                migration_id="concurrent-plan-a",
                source_entity_id=original.entity_id,
                source_key=original_proposal.source_key,
                target_entity_id=target_a.entity_id,
                effective_at="2026-02-01T00:00:00Z",
            )
        )
        prepared_b = identity.prepare(
            _plan(
                migration_id="concurrent-plan-b",
                source_entity_id=original.entity_id,
                source_key=original_proposal.source_key,
                target_entity_id=target_b.entity_id,
                effective_at="2026-02-01T00:00:00Z",
            )
        )
        assert prepared_a.assignments[0].expected_identity_generation == 0
        assert prepared_b.assignments[0].expected_identity_generation == 0

        barrier = threading.Barrier(2)

        def run(prepared):
            barrier.wait(timeout=10)
            try:
                return ("ok", identity.apply_prepared(prepared))
            except Exception as exc:  # assertion below inspects the losing writer
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(run, prepared_a)
            future_b = pool.submit(run, prepared_b)
            outcomes = [future_a.result(timeout=30), future_b.result(timeout=30)]

        successes = [value for status, value in outcomes if status == "ok"]
        failures = [value for status, value in outcomes if status == "error"]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], ValueError)
        assert "stale identity migration" in str(failures[0])

        state = _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        )
        assert state.identity_generation == 1
        assert state.entity.entity_id in {target_a.entity_id, target_b.entity_id}
        assert _migration_count(repo) == 1
        _assert_no_lock_markers(repo)

        winner_prepared = (
            prepared_a if state.entity.entity_id == target_a.entity_id else prepared_b
        )
        replay = identity.apply_prepared(winner_prepared)
        assert replay.replay is True
        assert _state(
            identity,
            entity_id=original.entity_id,
            source_key=original_proposal.source_key,
        ).identity_generation == 1
    finally:
        try:
            repo.execute("MATCH (n) DETACH DELETE n")
        finally:
            repo.close()
