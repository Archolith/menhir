from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionLifecycleCorruptionError,
    ProjectionWorkAlreadyCompletedError,
    StaleProjectionDefinitionError,
    StaleProjectionWorkError,
)
from menhir.infrastructure.neo4j import Neo4jRepository, Neo4jTransaction
from menhir.infrastructure.projection_lifecycle_repository import (
    PROJECTION_LIFECYCLE_SCHEMA_QUERIES,
    ProjectionLifecycleRepository,
)


def _definition(version: int = 1) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id="scalar-state",
        version=version,
        input_assertion_types=frozenset({"scalar"}),
        output_view_kind="scalar_state",
        assertion_id_resolver=lambda assertion: str(assertion),
        assertion_type_resolver=lambda _assertion: "scalar",
        target_resolver=lambda _assertion: None,
        fold=lambda _target, _assertions, _as_of: None,  # type: ignore[arg-type]
    )


def _target(subject: str = "ent-1") -> ProjectionTarget:
    return ProjectionTarget(
        namespace="tenant-a",
        subject_id=subject,
        key=("owned", "pre-1920"),
    )


def _target_json(target: ProjectionTarget) -> str:
    return json.dumps(
        {
            "key": list(target.key),
            "namespace": target.namespace,
            "subject_id": target.subject_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _work_row(
    repository: ProjectionLifecycleRepository,
    target: ProjectionTarget,
    *,
    version: int = 1,
    generation: int = 1,
    target_present: bool = True,
    reason: str = "assertion_written",
    applied_generation: int | None = None,
) -> dict[str, Any]:
    row = {
        "work_key": repository.work_key("scalar-state", target),
        "definition_id": "scalar-state",
        "definition_version": version,
        "target_json": _target_json(target),
        "generation": generation,
        "target_present": target_present,
        "reason": reason,
    }
    if applied_generation is not None:
        row["applied_generation"] = applied_generation
    return row


class ScriptedTransaction:
    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.script = list(script)
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.executed.append((query, params or {}))
        if not self.script:
            raise AssertionError(f"unexpected transaction query: {query}")
        expected, rows = self.script.pop(0)
        assert expected in query
        return [dict(row) for row in rows]


class ScriptedNeo4j:
    def __init__(
        self,
        *,
        direct: list[tuple[str, list[dict[str, Any]]]] | None = None,
        transactions: list[list[tuple[str, list[dict[str, Any]]]]] | None = None,
    ) -> None:
        self.direct = list(direct or [])
        self.transactions = list(transactions or [])
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.transaction_history: list[ScriptedTransaction] = []

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.executed.append((query, params or {}))
        if not self.direct:
            # DDL activation returns no rows and can issue several statements.
            if query in PROJECTION_LIFECYCLE_SCHEMA_QUERIES:
                return []
            raise AssertionError(f"unexpected direct query: {query}")
        expected, rows = self.direct.pop(0)
        assert expected in query
        return [dict(row) for row in rows]

    def execute_write(self, work: Callable[[ScriptedTransaction], Any]) -> Any:
        if not self.transactions:
            raise AssertionError("unexpected write transaction")
        transaction = ScriptedTransaction(self.transactions.pop(0))
        self.transaction_history.append(transaction)
        result = work(transaction)  # type: ignore[arg-type]
        if transaction.script:
            raise AssertionError(f"unconsumed transaction steps: {transaction.script}")
        return result


@pytest.mark.unit
def test_activation_is_additive_and_feature_scoped() -> None:
    fake = ScriptedNeo4j()
    repository = ProjectionLifecycleRepository(fake)

    repository.activate()

    assert [query for query, _params in fake.executed] == list(
        PROJECTION_LIFECYCLE_SCHEMA_QUERIES
    )
    ddl = "\n".join(PROJECTION_LIFECYCLE_SCHEMA_QUERIES)
    assert "ProjectionDefinitionState" in ddl
    assert "ProjectionWorkState" in ddl
    assert "ProjectionDerivationReceipt" in ddl
    assert "MutationProjection" not in ddl


@pytest.mark.unit
def test_publish_is_idempotent_but_same_version_semantic_drift_fails_closed() -> None:
    first_fake = ScriptedNeo4j(
        transactions=[
            [
                ("MERGE (d:ProjectionDefinitionState", [
                    {"current_version": 0, "current_semantic_hash": ""}
                ]),
                ("OPTIONAL MATCH (w:ProjectionWorkState", [{"invalidated_targets": 2}]),
            ]
        ]
    )
    first = ProjectionLifecycleRepository(first_fake).publish_definition(
        _definition(1),
        semantic_hash="semantic-v1",
    )
    assert first.changed is True
    assert first.invalidated_targets == 2
    update_query = first_fake.transaction_history[0].executed[1][0]
    assert "w.generation=coalesce(w.generation,0)+1" in update_query
    assert "w.certified_projection_hash=null" in update_query

    replay_fake = ScriptedNeo4j(
        transactions=[
            [
                ("MERGE (d:ProjectionDefinitionState", [
                    {"current_version": 1, "current_semantic_hash": "semantic-v1"}
                ]),
            ]
        ]
    )
    replay = ProjectionLifecycleRepository(replay_fake).publish_definition(
        _definition(1),
        semantic_hash="semantic-v1",
    )
    assert replay.changed is False

    collision_fake = ScriptedNeo4j(
        transactions=[
            [
                ("MERGE (d:ProjectionDefinitionState", [
                    {"current_version": 1, "current_semantic_hash": "semantic-v1"}
                ]),
            ]
        ]
    )
    with pytest.raises(ProjectionLifecycleCorruptionError):
        ProjectionLifecycleRepository(collision_fake).publish_definition(
            _definition(1),
            semantic_hash="different-code-same-version",
        )


@pytest.mark.unit
def test_old_definition_publisher_cannot_move_shared_version_back() -> None:
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MERGE (d:ProjectionDefinitionState", [
                    {"current_version": 3, "current_semantic_hash": "semantic-v3"}
                ]),
            ]
        ]
    )
    with pytest.raises(StaleProjectionDefinitionError):
        ProjectionLifecycleRepository(fake).publish_definition(
            _definition(2),
            semantic_hash="semantic-v2",
        )


@pytest.mark.unit
def test_dirty_target_advances_exact_current_definition_generation() -> None:
    target = _target()
    placeholder = ScriptedNeo4j()
    key = ProjectionLifecycleRepository(placeholder).work_key("scalar-state", target)
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 1}]),
                ("UNWIND $targets AS row", [{
                    "work_key": key,
                    "definition_id": "scalar-state",
                    "definition_version": 1,
                    "target_json": _target_json(target),
                    "generation": 4,
                    "target_present": True,
                    "reason": "assertion_written",
                }]),
            ]
        ]
    )

    token = ProjectionLifecycleRepository(fake).dirty_targets(
        _definition(1),
        [target],
        reason="assertion_written",
    )[0]

    assert token.generation == 4
    assert token.target == target
    assert token.target_present is True

    stale = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 2}]),
            ]
        ]
    )
    with pytest.raises(StaleProjectionDefinitionError):
        ProjectionLifecycleRepository(stale).dirty_targets(
            _definition(1),
            [target],
            reason="assertion_written",
        )


@pytest.mark.unit
def test_authoritative_target_reconciliation_discovers_removals_without_redirtying_unchanged() -> None:
    target_a = _target("ent-a")
    target_b = _target("ent-b")
    target_c = _target("ent-c")
    keys = {
        target.subject_id: ProjectionLifecycleRepository(ScriptedNeo4j()).work_key(
            "scalar-state", target
        )
        for target in (target_a, target_b, target_c)
    }
    existing_a = {
        **_work_row(
            ProjectionLifecycleRepository(ScriptedNeo4j()),
            target_a,
            generation=2,
            target_present=True,
            reason="prior",
        )
    }
    existing_b = {
        **_work_row(
            ProjectionLifecycleRepository(ScriptedNeo4j()),
            target_b,
            generation=4,
            target_present=False,
            reason="prior-retire",
        )
    }
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 1}]),
                ("MATCH (w:ProjectionWorkState", [existing_a, existing_b]),
                ("UNWIND $targets AS row", [
                    {
                        "work_key": keys["ent-b"],
                        "definition_id": "scalar-state",
                        "definition_version": 1,
                        "target_json": _target_json(target_b),
                        "generation": 5,
                        "target_present": True,
                        "reason": "target_set_reconciled",
                    },
                    {
                        "work_key": keys["ent-c"],
                        "definition_id": "scalar-state",
                        "definition_version": 1,
                        "target_json": _target_json(target_c),
                        "generation": 1,
                        "target_present": True,
                        "reason": "target_set_reconciled",
                    },
                ]),
                ("UNWIND $targets AS row", [{
                    "work_key": keys["ent-a"],
                    "definition_id": "scalar-state",
                    "definition_version": 1,
                    "target_json": _target_json(target_a),
                    "generation": 3,
                    "target_present": False,
                    "reason": "target_set_reconciled",
                }]),
            ]
        ]
    )

    changed = ProjectionLifecycleRepository(fake).reconcile_all_targets(
        _definition(1),
        [target_b, target_c],
    )

    by_subject = {token.target.subject_id: token for token in changed}
    assert set(by_subject) == {"ent-a", "ent-b", "ent-c"}
    assert by_subject["ent-a"].target_present is False
    assert by_subject["ent-a"].generation == 3
    assert by_subject["ent-b"].target_present is True
    assert by_subject["ent-b"].generation == 5
    assert by_subject["ent-c"].generation == 1


@pytest.mark.unit
def test_pending_fails_closed_if_work_version_disagrees_with_shared_current_version() -> None:
    target = _target()
    repository = ProjectionLifecycleRepository(ScriptedNeo4j())
    row = _work_row(repository, target, version=1, generation=2)
    row["current_definition_version"] = 2
    fake = ScriptedNeo4j(
        direct=[
            ("MATCH (w:ProjectionWorkState)", [row]),
        ]
    )

    with pytest.raises(ProjectionLifecycleCorruptionError):
        ProjectionLifecycleRepository(fake).pending(limit=10)


@pytest.mark.unit
def test_stale_worker_is_rejected_before_materialization_callback() -> None:
    target = _target()
    repository = ProjectionLifecycleRepository(ScriptedNeo4j())
    token = _work_token(
        repository,
        target,
        generation=2,
    )
    current = _work_row(
        repository,
        target,
        generation=3,
        applied_generation=1,
    )
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 1}]),
                ("MATCH (w:ProjectionWorkState", [current]),
            ]
        ]
    )
    called = False

    def materialize(_tx: Neo4jTransaction, _token: Any) -> str:
        nonlocal called
        called = True
        return "should-not-run"

    with pytest.raises(StaleProjectionWorkError):
        ProjectionLifecycleRepository(fake).commit(
            token,
            derivation_id="derive-2",
            materialize=materialize,
        )
    assert called is False


def _work_token(
    repository: ProjectionLifecycleRepository,
    target: ProjectionTarget,
    *,
    generation: int,
    target_present: bool = True,
):
    from menhir.domain.projection_lifecycle import ProjectionWorkToken

    return ProjectionWorkToken(
        work_key=repository.work_key("scalar-state", target),
        definition_id="scalar-state",
        definition_version=1,
        target=target,
        generation=generation,
        target_present=target_present,
        reason="assertion_written",
    )


@pytest.mark.unit
def test_commit_materializes_between_fence_and_exact_derivation_certificate() -> None:
    target = _target()
    seed = ProjectionLifecycleRepository(ScriptedNeo4j())
    token = _work_token(seed, target, generation=2)
    persisted = _work_row(
        seed,
        target,
        generation=2,
        applied_generation=1,
    )
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 1}]),
                ("MATCH (w:ProjectionWorkState", [persisted]),
                ("VIEW WRITE", [{"uuid": "view-2"}]),
                ("MERGE (r:ProjectionDerivationReceipt", [{
                    "projection_hash": "projection-hash-2",
                    "derivation_id": "derive-2",
                    "certified_generation": 2,
                }]),
            ]
        ]
    )

    def materialize(tx: Neo4jTransaction, work_token: Any) -> str:
        assert work_token is token
        tx.execute("VIEW WRITE", {"subject": work_token.target.subject_id})
        return "projection-hash-2"

    certificate = ProjectionLifecycleRepository(fake).commit(
        token,
        derivation_id="derive-2",
        materialize=materialize,
    )

    assert certificate.generation == 2
    assert certificate.projection_hash == "projection-hash-2"
    queries = [query for query, _params in fake.transaction_history[0].executed]
    assert queries.index("VIEW WRITE") < next(
        index
        for index, query in enumerate(queries)
        if "MERGE (r:ProjectionDerivationReceipt" in query
    )


@pytest.mark.unit
def test_already_completed_generation_never_replays_materializer() -> None:
    target = _target()
    seed = ProjectionLifecycleRepository(ScriptedNeo4j())
    token = _work_token(seed, target, generation=2)
    persisted = _work_row(
        seed,
        target,
        generation=2,
        applied_generation=2,
    )
    fake = ScriptedNeo4j(
        transactions=[
            [
                ("MATCH (d:ProjectionDefinitionState", [{"current_version": 1}]),
                ("MATCH (w:ProjectionWorkState", [persisted]),
            ]
        ]
    )
    called = False

    def materialize(_tx: Neo4jTransaction, _token: Any) -> str:
        nonlocal called
        called = True
        return "hash"

    with pytest.raises(ProjectionWorkAlreadyCompletedError):
        ProjectionLifecycleRepository(fake).commit(
            token,
            derivation_id="derive-2",
            materialize=materialize,
        )
    assert called is False


def _freshness_row(
    repository: ProjectionLifecycleRepository,
    target: ProjectionTarget,
    *,
    projection_hash: str = "projection-hash-2",
) -> dict[str, Any]:
    work_key = repository.work_key("scalar-state", target)
    receipt_key = hashlib.sha256(f"{work_key}\0{2}".encode("utf-8")).hexdigest()
    return {
        "current_definition_version": 1,
        "work_definition_id": "scalar-state",
        "work_definition_version": 1,
        "target_json": _target_json(target),
        "work_generation": 2,
        "applied_generation": 2,
        "target_present": True,
        "certified_definition_version": 1,
        "certified_generation": 2,
        "certified_projection_hash": projection_hash,
        "certified_derivation_id": "derive-2",
        "certified_receipt_key": receipt_key,
        "receipt_key": receipt_key,
        "receipt_definition_id": "scalar-state",
        "receipt_definition_version": 1,
        "receipt_target_json": _target_json(target),
        "receipt_target_present": True,
        "receipt_generation": 2,
        "receipt_projection_hash": projection_hash,
        "receipt_derivation_id": "derive-2",
    }


@pytest.mark.unit
def test_freshness_requires_exact_current_projection_hash_and_receipt() -> None:
    target = _target()
    seed = ProjectionLifecycleRepository(ScriptedNeo4j())
    fresh_fake = ScriptedNeo4j(
        direct=[
            ("OPTIONAL MATCH (d:ProjectionDefinitionState", [
                _freshness_row(seed, target)
            ]),
        ]
    )
    fresh = ProjectionLifecycleRepository(fresh_fake).assess_freshness(
        definition_id="scalar-state",
        target=target,
        current_projection_hash="projection-hash-2",
    )
    assert fresh.fresh is True
    assert fresh.reason == "certified_current"

    mismatch_fake = ScriptedNeo4j(
        direct=[
            ("OPTIONAL MATCH (d:ProjectionDefinitionState", [
                _freshness_row(seed, target)
            ]),
        ]
    )
    mismatch = ProjectionLifecycleRepository(mismatch_fake).assess_freshness(
        definition_id="scalar-state",
        target=target,
        current_projection_hash="different-installed-state",
    )
    assert mismatch.stale is True
    assert "projection_hash_not_certified" in mismatch.reason


@pytest.mark.unit
def test_empty_queue_does_not_imply_freshness_without_certificate() -> None:
    target = _target()
    seed = ProjectionLifecycleRepository(ScriptedNeo4j())
    row = _freshness_row(seed, target)
    row.update(
        {
            "certified_definition_version": None,
            "certified_generation": None,
            "certified_projection_hash": None,
            "certified_derivation_id": None,
            "certified_receipt_key": None,
            "receipt_key": None,
            "receipt_definition_id": None,
            "receipt_definition_version": None,
            "receipt_target_json": None,
            "receipt_target_present": None,
            "receipt_generation": None,
            "receipt_projection_hash": None,
            "receipt_derivation_id": None,
        }
    )
    fake = ScriptedNeo4j(
        direct=[
            ("MATCH (w:ProjectionWorkState)", []),
            ("OPTIONAL MATCH (d:ProjectionDefinitionState", [row]),
        ]
    )
    repository = ProjectionLifecycleRepository(fake)

    assert repository.pending() == ()
    assessment = repository.assess_freshness(
        definition_id="scalar-state",
        target=target,
        current_projection_hash="projection-hash-2",
    )

    assert assessment.stale is True
    assert "generation_not_certified" in assessment.reason
    assert "derivation_receipt_missing" in assessment.reason


class _RawRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def data(self) -> dict[str, Any]:
        return dict(self._data)


class _RawResult:
    def __iter__(self):
        return iter([_RawRecord({"ok": 1})])


class _RawTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.committed = False
        self.rolled_back = False

    def run(self, query: str, **params: Any) -> _RawResult:
        self.calls.append((query, params))
        return _RawResult()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class _Session:
    def __init__(self) -> None:
        self.raw_tx = _RawTx()

    def __enter__(self):
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def begin_transaction(self) -> _RawTx:
        return self.raw_tx


class _Driver:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self, *, database: str):
        assert database == "neo4j"
        return self.session_obj


@pytest.mark.unit
def test_execute_write_exposes_repository_compatible_transaction_adapter() -> None:
    repository = Neo4jRepository(
        uri="bolt://unused",
        database="neo4j",
        user="neo4j",
        password="unused",
    )
    driver = _Driver()
    repository._driver = driver  # type: ignore[assignment]

    rows = repository.execute_write(
        lambda tx: tx.execute("RETURN $value AS ok", {"value": 1})
    )

    assert rows == [{"ok": 1}]
    assert driver.session_obj.raw_tx.calls == [
        ("RETURN $value AS ok", {"value": 1})
    ]
    assert driver.session_obj.raw_tx.committed is True
    assert driver.session_obj.raw_tx.rolled_back is False


@pytest.mark.online
def test_projection_lifecycle_live_publish_dirty_commit_and_freshness(test_neo4j_repo) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    definition = _definition(1)
    target = _target()

    publication = lifecycle.publish_definition(
        definition,
        semantic_hash="live-semantic-v1",
    )
    assert publication.changed is True

    token = lifecycle.dirty_targets(
        definition,
        [target],
        reason="live_assertion_written",
    )[0]

    installed_hash = hashlib.sha256(b"live-view-v1").hexdigest()

    def materialize(tx: Neo4jTransaction, work_token: Any) -> str:
        tx.execute(
            """
            MERGE (n:ProjectionLifecycleTestView {work_key:$work_key})
            SET n.installed_generation=$generation,
                n.target_present=$target_present
            RETURN n.work_key AS work_key
            """,
            {
                "work_key": work_token.work_key,
                "generation": work_token.generation,
                "target_present": work_token.target_present,
            },
        )
        return installed_hash

    certificate = lifecycle.commit(
        token,
        derivation_id="live-derive-1",
        materialize=materialize,
    )

    assert certificate.projection_hash == installed_hash
    assert lifecycle.pending() == ()
    assert lifecycle.assess_freshness(
        definition_id=definition.definition_id,
        target=target,
        current_projection_hash=installed_hash,
    ).fresh is True
    assert lifecycle.assess_freshness(
        definition_id=definition.definition_id,
        target=target,
        current_projection_hash=hashlib.sha256(b"tampered").hexdigest(),
    ).stale is True


@pytest.mark.online
def test_projection_lifecycle_live_stale_worker_cannot_install_view(test_neo4j_repo) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    definition = _definition(1)
    target = _target()
    lifecycle.publish_definition(definition, semantic_hash="live-semantic-v1")

    stale = lifecycle.dirty_targets(definition, [target], reason="first")[0]
    current = lifecycle.dirty_targets(definition, [target], reason="second")[0]
    assert current.generation == stale.generation + 1

    called = False

    def materialize(tx: Neo4jTransaction, work_token: Any) -> str:
        nonlocal called
        called = True
        tx.execute(
            "CREATE (:ProjectionLifecycleTestView {work_key:$work_key})",
            {"work_key": work_token.work_key},
        )
        return "should-not-install"

    with pytest.raises(StaleProjectionWorkError):
        lifecycle.commit(
            stale,
            derivation_id="stale-derive",
            materialize=materialize,
        )

    assert called is False
    rows = test_neo4j_repo.execute(
        "MATCH (n:ProjectionLifecycleTestView) RETURN count(n) AS count"
    )
    assert rows == [{"count": 0}]


@pytest.mark.online
def test_projection_lifecycle_live_definition_bump_invalidates_old_worker(test_neo4j_repo) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    v1 = _definition(1)
    target = _target()
    lifecycle.publish_definition(v1, semantic_hash="live-semantic-v1")
    old = lifecycle.dirty_targets(v1, [target], reason="v1-work")[0]

    v2 = _definition(2)
    publication = lifecycle.publish_definition(v2, semantic_hash="live-semantic-v2")
    assert publication.invalidated_targets == 1

    pending = lifecycle.pending()
    assert len(pending) == 1
    assert pending[0].definition_version == 2
    assert pending[0].generation == old.generation + 1

    with pytest.raises(StaleProjectionDefinitionError):
        lifecycle.commit(
            old,
            derivation_id="old-worker",
            materialize=lambda _tx, _token: "old-hash",
        )


@pytest.mark.online
def test_projection_lifecycle_live_reconcile_marks_removed_targets_as_work(test_neo4j_repo) -> None:
    lifecycle = ProjectionLifecycleRepository(test_neo4j_repo)
    lifecycle.activate()
    definition = _definition(1)
    lifecycle.publish_definition(definition, semantic_hash="live-semantic-v1")
    target_a = _target("ent-a")
    target_b = _target("ent-b")

    initial = lifecycle.reconcile_all_targets(definition, [target_a, target_b])
    assert len(initial) == 2
    assert all(token.target_present for token in initial)

    for token in initial:
        lifecycle.commit(
            token,
            derivation_id=f"initial-{token.target.subject_id}",
            materialize=lambda _tx, work: hashlib.sha256(
                f"present:{work.target.subject_id}".encode("utf-8")
            ).hexdigest(),
        )

    changed = lifecycle.reconcile_all_targets(definition, [target_b])
    assert len(changed) == 1
    retired = changed[0]
    assert retired.target == target_a
    assert retired.target_present is False
    assert lifecycle.pending() == (retired,)