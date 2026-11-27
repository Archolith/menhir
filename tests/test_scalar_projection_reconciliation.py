from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import threading
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from examples.decision_timeline import EXTENSION, Decision, DecisionTimeline
from menhir import extensions as extension_module
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessCertificate,
    ProjectionWorkAlreadyCompletedError,
    ProjectionWorkToken,
    StaleProjectionDefinitionError,
)
from menhir.extensions import RuntimeExtensions
from menhir.infrastructure.neo4j import Neo4jRepository, SagaOwnershipRevoked
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.services.maintenance_scheduler import MaintenanceScheduler
from menhir.services.projection_reconciliation import ProjectionReconciler
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION, scalar_projection_target
from menhir.services.scalar_projection_reconciliation import ScalarProjectionReconciler
from menhir.services.scheduler_lease import SchedulerLeaseStore


class _FakeLifecycle:
    def __init__(self) -> None:
        self.generation = 0
        self.dirty_calls: list[tuple[Any, tuple[Any, ...], str]] = []
        self.commit_calls: list[tuple[ProjectionWorkToken, str]] = []
        self.pending_tokens: list[ProjectionWorkToken] = []
        self.fail_work_key: str | None = None

    def dirty_targets(self, definition: Any, targets: Any, *, reason: str):
        targets = tuple(targets)
        self.dirty_calls.append((definition, targets, reason))
        tokens = []
        for target in targets:
            self.generation += 1
            token = ProjectionWorkToken(
                work_key=f"work-{self.generation}",
                definition_id=definition.definition_id,
                definition_version=definition.version,
                target=target,
                generation=self.generation,
                target_present=True,
                reason=reason,
            )
            tokens.append(token)
            self.pending_tokens.append(token)
        return tuple(tokens)

    def commit(self, token: ProjectionWorkToken, *, derivation_id: str, materialize: Any):
        self.commit_calls.append((token, derivation_id))
        if token.work_key == self.fail_work_key:
            raise RuntimeError("materialization failed")
        projection_hash = materialize(object(), token)
        self.pending_tokens.remove(token)
        return ProjectionFreshnessCertificate(
            definition_id=token.definition_id,
            definition_version=token.definition_version,
            target=token.target,
            generation=token.generation,
            target_present=token.target_present,
            projection_hash=projection_hash,
            derivation_id=derivation_id,
        )

    def pending(self, *, definition_id: str, definition_version: int, limit: int = 100):
        tokens = (
            token for token in self.pending_tokens
            if token.definition_id == definition_id and token.definition_version == definition_version
        )
        return tuple(list(tokens)[:limit])


class _Materializer:
    def __init__(self) -> None:
        self.tokens: list[ProjectionWorkToken] = []

    def __call__(self, _tx: Any, token: ProjectionWorkToken) -> str:
        self.tokens.append(token)
        return f"hash-{token.work_key}"


def _row(*, attribute: str = "height", namespace: str | None = "alpha") -> dict[str, Any]:
    return {
        "assertion_id": f"a-{attribute}",
        "subject_uuid": "entity-1",
        "namespace": namespace,
        "attribute": attribute,
        "scope": "",
        "value_kind": "number",
        "unit": "cm",
        "operation": "absolute",
    }


def test_reconcile_assertions_dirties_unique_exact_slots_then_certifies() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)

    result = reconciler.reconcile_assertions(
        [_row(), _row(), _row(attribute="weight")],
        operation_id="ingest-42",
    )

    assert len(lifecycle.dirty_calls) == 1
    definition, targets, reason = lifecycle.dirty_calls[0]
    assert definition is SCALAR_STATE_PROJECTION
    assert reason == "typed_scalar_mutation"
    assert [target.key[0] for target in targets] == ["height", "weight"]
    assert len(result) == 2
    assert materializer.tokens == [item.token for item in result]
    assert all(item.certificate.target == item.token.target for item in result)
    assert lifecycle.pending_tokens == []


def test_failed_materialization_leaves_generation_pending_for_recovery() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)
    tokens = reconciler.dirty_assertions([_row()])
    lifecycle.fail_work_key = tokens[0].work_key

    with pytest.raises(RuntimeError, match="materialization failed"):
        reconciler.commit_tokens(tokens, operation_id="ingest-43")

    assert lifecycle.pending_tokens == [tokens[0]]

    lifecycle.fail_work_key = None
    recovered = reconciler.drain_pending(operation_id="repair-1")
    assert [item.token for item in recovered] == [tokens[0]]
    assert lifecycle.pending_tokens == []


def test_drain_pending_ignores_other_projection_definitions() -> None:
    lifecycle = _FakeLifecycle()
    materializer = _Materializer()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=materializer)
    scalar = reconciler.dirty_assertions([_row()])[0]
    other = ProjectionWorkToken(
        work_key="other-work",
        definition_id="other.projection",
        definition_version=1,
        target=scalar.target,
        generation=1,
        target_present=True,
        reason="other",
    )
    # Foreign work fills the first page, so a post-LIMIT filter would starve scalar.
    lifecycle.pending_tokens.insert(0, other)

    recovered = reconciler.drain_pending(operation_id="repair-2", limit=1)

    assert [item.token for item in recovered] == [scalar]
    assert lifecycle.pending_tokens == [other]


def test_derivation_id_is_stable_for_same_operation_and_generation() -> None:
    lifecycle = _FakeLifecycle()
    reconciler = ScalarProjectionReconciler(lifecycle, materializer=_Materializer())
    token = reconciler.dirty_assertions([_row(namespace=None)])[0]

    first = reconciler.commit_tokens([token], operation_id="same-op")[0]
    # Reinsert the same generation to model an idempotent certification replay in a repository fake.
    lifecycle.pending_tokens.append(token)
    second = reconciler.commit_tokens([token], operation_id="same-op")[0]

    original = f"scalar-reconcile-v1\0same-op\0{token.work_key}\0{token.generation}".encode()
    assert first.certificate.derivation_id == second.certificate.derivation_id == hashlib.sha256(original).hexdigest()


def test_registered_lanes_use_their_own_materializers_and_leave_unsupported_work_pending() -> None:
    lifecycle = _FakeLifecycle()
    scalar_materializer = _Materializer()
    other_materializer = _Materializer()
    scalar_lane = ScalarProjectionReconciler(lifecycle, materializer=scalar_materializer)
    other_definition = replace(SCALAR_STATE_PROJECTION, definition_id="other.projection", version=2)
    other_lane = ProjectionReconciler(lifecycle, other_definition, materializer=other_materializer)

    scalar = scalar_lane.dirty_assertions([_row()])[0]
    previous = replace(other_definition, version=1)
    (old,) = lifecycle.dirty_targets(previous, [scalar.target], reason="previous_version")
    (other,) = lifecycle.dirty_targets(other_definition, [scalar.target], reason="registered")
    (unknown,) = lifecycle.dirty_targets(
        replace(other_definition, definition_id="unknown.projection"), [scalar.target], reason="unregistered",
    )
    with pytest.raises(ValueError, match="another projection version"):
        other_lane.commit_tokens([old], operation_id="wrong-version")
    with pytest.raises(ValueError, match="another projection definition"):
        other_lane.commit_tokens([unknown], operation_id="wrong-definition")
    assert lifecycle.commit_calls == []

    assert [item.token for item in scalar_lane.drain_pending(operation_id="scalar")] == [scalar]
    assert [item.token for item in other_lane.drain_pending(operation_id="other", limit=1)] == [other]
    assert scalar_materializer.tokens == [scalar]
    assert other_materializer.tokens == [other]
    assert lifecycle.pending_tokens == [old, unknown]


def _projection_scheduler(tmp_path: Path, *workers: ProjectionReconciler) -> MaintenanceScheduler:
    scheduler = MaintenanceScheduler(
        ingest_service=SimpleNamespace(get_queue_depth=lambda: 0),
        graph_adapter=SimpleNamespace(),
        lease_store=SchedulerLeaseStore(db_path=tmp_path / "scheduler.db"),
        lease_heartbeat_s=0.05,
        tick_interval_s=0.01,
        projection_workers=workers,
    )
    scheduler._jobs = {
        name: job for name, job in scheduler._jobs.items()
        if name.startswith("reconcile_projection:")
    }
    return scheduler


async def _wait_for_job(scheduler: MaintenanceScheduler, definition_id: str) -> dict[str, object]:
    name = f"reconcile_projection:{definition_id}"
    async with asyncio.timeout(5):
        while True:
            status = scheduler.status_snapshot()["jobs"][name]
            if status["runs"]:
                return status
            await asyncio.sleep(0.01)


def test_scheduler_rejects_two_workers_for_same_definition(tmp_path: Path) -> None:
    queue = _FakeLifecycle()
    first = ProjectionReconciler(queue, SCALAR_STATE_PROJECTION, materializer=_Materializer())
    second = ProjectionReconciler(
        queue, replace(SCALAR_STATE_PROJECTION, version=2), materializer=_Materializer(),
    )
    with pytest.raises(ValueError, match="duplicate projection worker"):
        _projection_scheduler(tmp_path, first, second)


@pytest.mark.asyncio
async def test_scheduler_runs_separate_bounded_lanes_even_when_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _FakeLifecycle()
    failed_definition = replace(SCALAR_STATE_PROJECTION, definition_id="extension.failing")
    healthy_definition = replace(SCALAR_STATE_PROJECTION, definition_id="extension.healthy")
    unknown_definition = replace(SCALAR_STATE_PROJECTION, definition_id="extension.unregistered")
    target = scalar_projection_target(_row())
    (failed,) = queue.dirty_targets(failed_definition, [target], reason="test")
    (unknown,) = queue.dirty_targets(unknown_definition, [target], reason="test")
    healthy_tokens = queue.dirty_targets(
        healthy_definition, [replace(target, subject_id=f"subject-{i}") for i in range(101)], reason="test",
    )
    queue.fail_work_key = failed.work_key
    real_commit = queue.commit

    def commit(token: ProjectionWorkToken, **kwargs: Any) -> ProjectionFreshnessCertificate:
        if token == healthy_tokens[0]:
            queue.pending_tokens.remove(token)
            raise ProjectionWorkAlreadyCompletedError("already applied by another worker")
        return real_commit(token, **kwargs)

    monkeypatch.setattr(queue, "commit", commit)
    materializer = _Materializer()
    scheduler = _projection_scheduler(
        tmp_path,
        ProjectionReconciler(queue, failed_definition, materializer=_Materializer()),
        ProjectionReconciler(queue, healthy_definition, materializer=materializer),
    )
    await scheduler.start()
    try:
        healthy_status = await _wait_for_job(scheduler, healthy_definition.definition_id)
        failed_status = await _wait_for_job(scheduler, failed_definition.definition_id)
    finally:
        await scheduler.stop()

    assert failed_status["last_success"] is False
    assert healthy_status["last_result"] == {"applied": 99, "superseded": 1}
    assert materializer.tokens == list(healthy_tokens[1:100])
    assert queue.pending_tokens == [failed, unknown, healthy_tokens[-1]]


@pytest.mark.asyncio
async def test_projection_lane_stops_dispatching_after_lease_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _FakeLifecycle()
    definition = SCALAR_STATE_PROJECTION
    scalar = ScalarProjectionReconciler(queue, materializer=_Materializer())
    first, second = scalar.dirty_assertions([_row(), _row(attribute="weight")])
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    completed = asyncio.Event()
    refused = threading.Event()
    real_commit = queue.commit
    neo4j = Neo4jRepository(uri="bolt://127.0.0.1:1", database="neo4j", user="test", password="test")

    def driver_must_not_open():
        pytest.fail("a former owner tried to open a Neo4j transaction")

    monkeypatch.setattr(neo4j, "_get_driver", driver_must_not_open)

    def commit(token: ProjectionWorkToken, **kwargs: Any) -> ProjectionFreshnessCertificate:
        try:
            certificate = real_commit(token, **kwargs)
            if token == first:
                with pytest.raises(SagaOwnershipRevoked):
                    neo4j.execute_write(lambda _tx: None)
                refused.set()
            return certificate
        finally:
            if token == first:
                loop.call_soon_threadsafe(completed.set)

    monkeypatch.setattr(queue, "commit", commit)

    def materialize(_tx: Any, token: ProjectionWorkToken) -> str:
        if token == first:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=10):
                raise TimeoutError("test never released first transaction")
        return "applied"

    scheduler = _projection_scheduler(tmp_path, ProjectionReconciler(queue, definition, materializer=materialize))
    await scheduler.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        scheduler.lease_store.force_acquire(
            lease_name=scheduler._projection_lanes[0].lease_name, owner_id="next-owner", owner_pid=os.getpid(),
            lease_duration_s=30,
        )
        status = await _wait_for_job(scheduler, definition.definition_id)
        assert status["last_result"]["abandoned"] == "lease_lost"
        assert [token for token, _ in queue.commit_calls] == [first]
    finally:
        release.set()
        if started.is_set():
            await asyncio.wait_for(completed.wait(), timeout=5)
        await scheduler.stop()
    assert queue.pending_tokens == [second]
    assert refused.is_set()


@pytest.mark.asyncio
async def test_blocked_projection_lane_does_not_delay_another_definition(tmp_path: Path) -> None:
    queue = _FakeLifecycle()
    stalled = replace(SCALAR_STATE_PROJECTION, definition_id="extension.stalled")
    healthy = replace(SCALAR_STATE_PROJECTION, definition_id="extension.healthy")
    target = scalar_projection_target(_row())
    (pending,) = queue.dirty_targets(stalled, [target], reason="test")
    queue.dirty_targets(healthy, [target], reason="test")
    entered, release = threading.Event(), threading.Event()

    def stalled_materializer(_tx: Any, _token: ProjectionWorkToken) -> str:
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("test never released the stalled lane")
        return "released"

    scheduler = _projection_scheduler(
        tmp_path,
        ProjectionReconciler(queue, stalled, materializer=stalled_materializer),
        ProjectionReconciler(queue, healthy, materializer=_Materializer()),
    )
    await scheduler.start()
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await _wait_for_job(scheduler, healthy.definition_id))["last_result"]["applied"] == 1
        assert queue.pending_tokens == [pending]
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("older_installed", [False, True])
async def test_projection_lease_is_independent_of_older_host_and_exclusive_for_the_version(
    tmp_path: Path, older_installed: bool,
) -> None:
    queue = _FakeLifecycle()
    older = SCALAR_STATE_PROJECTION
    current = replace(older, version=older.version + 1)
    queue.dirty_targets(current, [scalar_projection_target(_row())], reason="only newer host can drain")
    first_workers = (ProjectionReconciler(queue, older, materializer=_Materializer()),) if older_installed else ()
    first = _projection_scheduler(tmp_path, *first_workers)
    capable = _projection_scheduler(tmp_path, ProjectionReconciler(queue, current, materializer=_Materializer()))
    duplicate = _projection_scheduler(tmp_path, ProjectionReconciler(queue, current, materializer=_Materializer()))
    assert await first.start()
    try:
        assert await capable.start()
        assert capable.status_snapshot()["maintenance_running"] is False
        assert not await duplicate.start()
        assert (await _wait_for_job(capable, current.definition_id))["last_result"]["applied"] == 1
        lane = f"reconcile_projection:{current.definition_id}"
        assert capable.status_snapshot()["projection_leases"][lane]["acquired"]
        assert duplicate.status_snapshot()["projection_leases"][lane]["standby"]
        assert duplicate.status_snapshot()["jobs"][lane]["runs"] == 0
    finally:
        await asyncio.gather(first.stop(), capable.stop(), duplicate.stop())
    assert queue.pending_tokens == []


@pytest.mark.online
@pytest.mark.asyncio
async def test_projection_scheduler_restart_recovers_registered_work(tmp_path: Path, test_neo4j_repo) -> None:
    queue = ProjectionLifecycleRepository(test_neo4j_repo)
    queue.activate()
    first = replace(SCALAR_STATE_PROJECTION, definition_id="extension.first")
    second = replace(SCALAR_STATE_PROJECTION, definition_id="extension.second")
    for definition in (first, second):
        queue.publish_definition(definition, semantic_hash=definition.definition_id)
        queue.dirty_targets(definition, [scalar_projection_target(_row())], reason="scheduled_recovery")

    def materialize(tx: Any, token: ProjectionWorkToken) -> str:
        tx.execute(
            "CREATE (:ProjectionSchedulerTestOutput {definition_id:$definition_id, namespace:$namespace})",
            {"definition_id": token.definition_id, "namespace": token.target.namespace},
        )
        return f"hash-{token.definition_id}"

    before = _projection_scheduler(tmp_path, ProjectionReconciler(queue, first, materializer=materialize))
    await before.start()
    try:
        assert (await _wait_for_job(before, first.definition_id))["last_result"]["applied"] == 1
    finally:
        await before.stop()
    assert [token.definition_id for token in queue.pending()] == [second.definition_id]

    after = _projection_scheduler(
        tmp_path,
        ProjectionReconciler(queue, first, materializer=materialize),
        ProjectionReconciler(queue, second, materializer=materialize),
    )
    await after.start()
    try:
        assert (await _wait_for_job(after, first.definition_id))["last_result"]["applied"] == 0
        assert (await _wait_for_job(after, second.definition_id))["last_result"]["applied"] == 1
    finally:
        await after.stop()
    assert queue.pending() == ()
    assert test_neo4j_repo.execute(
        """
        MATCH (v:ProjectionSchedulerTestOutput)
        RETURN v.definition_id AS definition_id, v.namespace AS namespace
        ORDER BY definition_id
        """
    ) == [
        {"definition_id": first.definition_id, "namespace": "alpha"},
        {"definition_id": second.definition_id, "namespace": "alpha"},
    ]


def _hold_previous_projection_batch(pipe, graph_options: dict[str, str], namespaces: tuple[str, ...]) -> None:
    graph = Neo4jRepository(**graph_options)
    try:
        queue = ProjectionLifecycleRepository(graph)
        registry = RuntimeExtensions((EXTENSION,))
        timeline = DecisionTimeline(graph, registry.view_repository(graph))
        timeline.initialize()
        (registration,) = registry.projection_workers()
        seed = ProjectionReconciler(queue, registration.definition, materializer=registration.materializer)
        for decision_text in ("Before rollout", "Pending revision"):
            for namespace in namespaces:
                timeline.put(
                    namespace=namespace, project="menhir",
                    decision=Decision("one", date(2026, 11, 27), decision_text, "#725"),
                )
            if decision_text == "Before rollout":
                assert len(seed.drain_pending(operation_id="before-rollout")) == len(namespaces)

        def must_not_materialize(_tx, _token):
            raise AssertionError("a stale worker reached the materializer")

        worker = ProjectionReconciler(queue, registration.definition, materializer=must_not_materialize)
        batch = worker.pending_tokens(limit=10)
        pipe.send(batch)
        assert pipe.poll(30) and pipe.recv() == "published"
        for token in batch:
            with pytest.raises(StaleProjectionDefinitionError):
                worker.commit_tokens((token,), operation_id="previous-host")
        pipe.send(worker.pending_tokens(limit=10))
        assert pipe.poll(30) and pipe.recv() == "stop"
    finally:
        graph.close()
        pipe.close()


@pytest.mark.online
@pytest.mark.asyncio
async def test_projection_rollout_preserves_pending_work_across_upgrade_and_removal(
    tmp_path: Path, test_neo4j_repo: Neo4jRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extension_module, "_process_registry", extension_module._UNBOUND)
    graph = test_neo4j_repo
    queue = ProjectionLifecycleRepository(graph)
    new_definition = replace(EXTENSION.definition, version=2)
    v2 = RuntimeExtensions((replace(EXTENSION, definition=new_definition),))
    timeline = DecisionTimeline(graph, v2.view_repository(graph))
    namespaces = ("rollout-migrate", "rollout-retire", "rollout-hold")

    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    graph_options = {name: getattr(graph, name) for name in ("uri", "database", "user", "password")}
    old_host = ctx.Process(target=_hold_previous_projection_batch, args=(child, graph_options, namespaces), daemon=True)
    old_host.start()
    child.close()
    uninstalled = _projection_scheduler(tmp_path)
    upgraded = None
    try:
        assert await asyncio.to_thread(parent.poll, 25), "old worker did not read its queue"
        pending = {token.target.namespace: token for token in parent.recv()}
        assert set(pending) == set(namespaces)
        assert set(queue.pending()) == set(pending.values())
        await asyncio.to_thread(queue.publish_definition, new_definition, semantic_hash="example-project-decisions-v2")
        parent.send("published")
        assert await asyncio.to_thread(parent.poll, 15), "old worker did not report its stale commits"
        assert parent.recv() == ()

        # A host with the extension removed owns general maintenance.
        assert await uninstalled.start()
        assert not uninstalled.status_snapshot()["jobs"]
        assert set(queue.pending()) == set(pending.values())
        before = {namespace: timeline.current(namespace=namespace, project="menhir") for namespace in namespaces}
        assert all(view and view["entries"][0]["decision"] == "Before rollout" for view in before.values())

        await asyncio.to_thread(
            queue.migrate_retained_work, new_definition, pending["rollout-migrate"], reason="upgrade",
        )
        await asyncio.to_thread(
            queue.retire_retained_work, new_definition, pending["rollout-retire"], reason="retire obsolete projection",
        )
        (second,) = v2.projection_workers()
        upgraded = _projection_scheduler(
            tmp_path, ProjectionReconciler(queue, second.definition, materializer=second.materializer),
        )
        assert await upgraded.start()
        assert upgraded.status_snapshot()["maintenance_running"] is False
        status = await _wait_for_job(upgraded, new_definition.definition_id)
        assert status["last_result"] == {"applied": 2, "superseded": 0}
        await upgraded.stop()
        parent.send("stop")
        await asyncio.to_thread(old_host.join, 10)
        assert old_host.exitcode == 0

        # With both extension versions stopped, only the uninstalled host remains.
        assert uninstalled.is_running()
        assert queue.pending() == (pending["rollout-hold"],)
        migrated = timeline.current(namespace="rollout-migrate", project="menhir")
        assert migrated and migrated["entries"][0]["decision"] == "Pending revision"
        assert timeline.current(namespace="rollout-retire", project="menhir") is None
        assert timeline.current(namespace="rollout-hold", project="menhir") == before["rollout-hold"]
    finally:
        parent.close()
        if old_host.is_alive():
            old_host.terminate()
            await asyncio.to_thread(old_host.join, 10)
        old_host.close()
        if upgraded is not None:
            await upgraded.stop()
        await uninstalled.stop()
