from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import Event
from typing import Any

import pytest

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import ProjectionLifecycleCorruptionError
from menhir.infrastructure.neo4j import Neo4jTransaction
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository


def _definition(version: int = 1) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id="scalar-state-atomicity",
        version=version,
        input_assertion_types=frozenset({"scalar"}),
        output_view_kind="scalar_state",
        assertion_id_resolver=lambda assertion: str(assertion),
        assertion_type_resolver=lambda _assertion: "scalar",
        target_resolver=lambda _assertion: None,
        fold=lambda _target, _assertions, _as_of: None,  # type: ignore[arg-type]
    )


def _target(subject: str = "ent-atomicity") -> ProjectionTarget:
    return ProjectionTarget(
        namespace="tenant-a",
        subject_id=subject,
        key=("owned", "pre-1920"),
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.mark.online
def test_projection_lifecycle_live_certification_failure_rolls_back_materialization(
    test_neo4j_repo,
) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    definition = _definition(1)
    target = _target()
    lifecycle.publish_definition(definition, semantic_hash="atomicity-semantic-v1")
    token = lifecycle.dirty_targets(definition, [target], reason="rollback-proof")[0]

    receipt_key = hashlib.sha256(
        f"{token.work_key}\0{token.generation}".encode("utf-8")
    ).hexdigest()
    test_neo4j_repo.execute(
        """
        CREATE (:ProjectionDerivationReceipt {
            receipt_key:$receipt_key,
            derivation_id:'conflicting-preexisting-receipt'
        })
        """,
        {"receipt_key": receipt_key},
    )

    def materialize(tx: Neo4jTransaction, work_token: Any) -> str:
        tx.execute(
            """
            CREATE (:ProjectionLifecycleRollbackTestView {
                work_key:$work_key,
                generation:$generation
            })
            """,
            {
                "work_key": work_token.work_key,
                "generation": work_token.generation,
            },
        )
        return _hash("would-have-been-installed")

    with pytest.raises(ProjectionLifecycleCorruptionError):
        lifecycle.commit(
            token,
            derivation_id="rollback-derive",
            materialize=materialize,
        )

    assert test_neo4j_repo.execute(
        "MATCH (n:ProjectionLifecycleRollbackTestView) RETURN count(n) AS count"
    ) == [{"count": 0}]
    pending = lifecycle.pending()
    assert len(pending) == 1
    assert pending[0].work_key == token.work_key
    assert pending[0].generation == token.generation


@pytest.mark.online
def test_projection_lifecycle_live_retirement_can_be_certified_fresh_as_absent(
    test_neo4j_repo,
) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    definition = _definition(1)
    target = _target("ent-retired")
    lifecycle.publish_definition(definition, semantic_hash="retirement-semantic-v1")

    present = lifecycle.reconcile_all_targets(definition, [target])[0]
    present_hash = _hash("present:ent-retired")

    def install_present(tx: Neo4jTransaction, work_token: Any) -> str:
        tx.execute(
            """
            MERGE (n:ProjectionLifecycleRetirementTestView {work_key:$work_key})
            SET n.generation=$generation
            RETURN n.work_key AS work_key
            """,
            {
                "work_key": work_token.work_key,
                "generation": work_token.generation,
            },
        )
        return present_hash

    lifecycle.commit(
        present,
        derivation_id="present-derive",
        materialize=install_present,
    )

    retired = lifecycle.reconcile_all_targets(definition, [])[0]
    assert retired.target == target
    assert retired.target_present is False
    absent_hash = _hash("absent:ent-retired")

    def install_absence(tx: Neo4jTransaction, work_token: Any) -> str:
        tx.execute(
            """
            MATCH (n:ProjectionLifecycleRetirementTestView {work_key:$work_key})
            DETACH DELETE n
            """,
            {"work_key": work_token.work_key},
        )
        return absent_hash

    certificate = lifecycle.commit(
        retired,
        derivation_id="retirement-derive",
        materialize=install_absence,
    )

    assert certificate.target_present is False
    assert certificate.projection_hash == absent_hash
    assert test_neo4j_repo.execute(
        "MATCH (n:ProjectionLifecycleRetirementTestView) RETURN count(n) AS count"
    ) == [{"count": 0}]
    assert lifecycle.pending() == ()

    assessment = lifecycle.assess_freshness(
        definition_id=definition.definition_id,
        target=target,
        current_projection_hash=absent_hash,
    )
    assert assessment.fresh is True
    assert assessment.target_present is False
    assert lifecycle.assess_freshness(
        definition_id=definition.definition_id,
        target=target,
        current_projection_hash=present_hash,
    ).stale is True


@pytest.mark.online
def test_projection_lifecycle_live_definition_publication_waits_for_inflight_commit(
    test_neo4j_repo,
) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    v1 = _definition(1)
    v2 = _definition(2)
    target = _target("ent-race")
    lifecycle.publish_definition(v1, semantic_hash="race-semantic-v1")
    token = lifecycle.dirty_targets(v1, [target], reason="race-work")[0]
    installed_hash = _hash("race-installed-v1")

    materializer_entered = Event()
    allow_commit = Event()
    publisher_started = Event()

    def materialize(tx: Neo4jTransaction, work_token: Any) -> str:
        materializer_entered.set()
        assert allow_commit.wait(timeout=5)
        tx.execute(
            """
            MERGE (n:ProjectionLifecycleRaceTestView {work_key:$work_key})
            SET n.generation=$generation
            RETURN n.work_key AS work_key
            """,
            {
                "work_key": work_token.work_key,
                "generation": work_token.generation,
            },
        )
        return installed_hash

    def commit_v1():
        return lifecycle.commit(
            token,
            derivation_id="race-derive-v1",
            materialize=materialize,
        )

    def publish_v2():
        publisher_started.set()
        return lifecycle.publish_definition(v2, semantic_hash="race-semantic-v2")

    with ThreadPoolExecutor(max_workers=2) as pool:
        commit_future = pool.submit(commit_v1)
        assert materializer_entered.wait(timeout=5)
        publish_future = pool.submit(publish_v2)
        assert publisher_started.wait(timeout=5)

        with pytest.raises(FutureTimeoutError):
            publish_future.result(timeout=0.2)

        allow_commit.set()
        certificate = commit_future.result(timeout=10)
        publication = publish_future.result(timeout=10)

    assert certificate.definition_version == 1
    assert publication.version == 2
    assert publication.invalidated_targets == 1

    pending = lifecycle.pending()
    assert len(pending) == 1
    assert pending[0].definition_version == 2
    assert pending[0].generation == token.generation + 1
    assert lifecycle.assess_freshness(
        definition_id=v2.definition_id,
        target=target,
        current_projection_hash=installed_hash,
    ).stale is True
