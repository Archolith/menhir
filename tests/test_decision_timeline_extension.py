"""The synthetic decision timeline through the actual HTTP runtime lifespan."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import date
from threading import Event
from unittest.mock import AsyncMock

import pytest

from examples.decision_timeline import EXTENSION, Decision, DecisionTimeline
from menhir import extensions as extension_module
from menhir.api import server
from menhir.config import MemorySettings
from menhir.core import bootstrap, runtime
from menhir.core.backend_runtime import RuntimeProvider
from menhir.core.runtime_preflight import RuntimeCapabilities
from menhir.extensions import RuntimeExtensions
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.services.projection_reconciliation import ProjectionReconciler
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION
from menhir.services.scheduler_lease import SchedulerLeaseStore


@pytest.fixture(autouse=True)
def isolated_process_registry(monkeypatch) -> None:
    monkeypatch.setattr(extension_module, "_process_registry", extension_module._UNBOUND)


@pytest.mark.online
def test_decision_timeline_d5_lookup(test_neo4j_repo) -> None:
    """Produce the timeline arm of the D5 lookup comparison."""
    trial = (
        Decision(
            "d5-open", date(2026, 11, 20),
            "Open question: settle how to handle older pending projection work before scheduling extensions.",
            "https://github.com/Archolith/menhir/blob/9a39ce12b46b8d337bdbaccb294c9ea8ce17a4ba"
            "/.agent/roadmaps/shared-abstractions-and-consolidation-2026-11-20.md#L58",
        ),
        Decision(
            "d5-approved", date(2026, 11, 26),
            "Approved: the extension explicitly migrates or retires old pending work. "
            "With neither action, the work stays pending for review.",
            "https://github.com/Archolith/menhir/issues/725#decisions-owner-d1-and-d6-first",
        ),
        Decision(
            "d5-implemented", date(2026, 11, 26),
            "Implementation: the local lifecycle repository can migrate or retire one exact retained generation.",
            "Local commit 8af56552 (git show 8af56552)",
        ),
    )
    registry = RuntimeExtensions((EXTENSION,))
    timeline = DecisionTimeline(test_neo4j_repo, registry.view_repository(test_neo4j_repo))
    timeline.initialize()
    for decision in reversed(trial):
        timeline.put(namespace="usefulness", project="menhir", decision=decision)

    queue = ProjectionLifecycleRepository(test_neo4j_repo)
    (worker,) = registry.projection_workers()
    reconciler = ProjectionReconciler(queue, worker.definition, materializer=worker.materializer)
    (materialized,) = reconciler.drain_pending(operation_id="d5-lookup")
    assert materialized.token.target.namespace == "usefulness"
    current = timeline.current(namespace="usefulness", project="menhir")
    assert current is not None
    entries = current["entries"]
    assert entries == [
        {"id": row.decision_id, "when": row.when.isoformat(), "decision": row.decision, "source": row.source}
        for row in trial
    ]

    print("\nMenhir #725 decision timeline")
    for entry in entries:
        print(f"{entry['when']} | {entry['decision']}\n  {entry['source']}")


def test_duplicate_extension_fails_before_server_resources(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid registrations must fail before server resources are created")

    monkeypatch.setattr(server, "create_mcp_streamable_http_app", unexpected)
    with pytest.raises(ValueError, match="already registered"):
        server.create_app(extensions=(EXTENSION, EXTENSION))
    with pytest.raises(ValueError, match="match its projection output kind"):
        server.create_app(extensions=(replace(
            EXTENSION, definition=replace(EXTENSION.definition, output_view_kind="wrong_kind"),
        ),))
    with pytest.raises(ValueError, match="duplicate projection definition_id"):
        server.create_app(extensions=(replace(
            EXTENSION, definition=replace(EXTENSION.definition, definition_id=SCALAR_STATE_PROJECTION.definition_id),
        ),))
    async def async_materialize(_views, _token):
        return "hash"

    with pytest.raises(TypeError, match="must be synchronous"):
        server.create_app(extensions=(replace(EXTENSION, materialize=async_materialize),))
    monkeypatch.setattr(EXTENSION.view_kind, "read_fields", "")
    with pytest.raises(ValueError, match="requires non-blank read_fields"):
        server.create_app(extensions=(EXTENSION,))


@pytest.fixture
def graph_only_runtime_settings(test_neo4j_repo, monkeypatch, tmp_path) -> MemorySettings:
    monkeypatch.setenv("MENHIR_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime, "collect_runtime_capabilities",
        lambda *_a, **_kw: RuntimeCapabilities(
            venv_ready=True, neo4j_ready=True, graphiti_dependency_ready=True,
            graphiti_llm_ready=True, reranker_ready=True, embedder_ready=True, failures=(),
        ),
    )
    # This example has no model calls. Give the runtime its regular maintenance
    # capability while keeping external model clients out of the fixture.
    monkeypatch.setattr(
        bootstrap.GraphitiClient, "from_settings_with_capabilities",
        lambda *_a, **_kw: bootstrap.UnavailableGraphitiClient("synthetic example"),
    )
    monkeypatch.setattr(
        bootstrap.LLMAdapter, "from_settings",
        lambda *_a, **_kw: bootstrap.UnavailableLLMAdapter("synthetic example"),
    )
    monkeypatch.setattr("menhir.services.cross_encoder_rerank.start_model_fetch", lambda *_a, **_kw: None)
    monkeypatch.setattr(runtime, "_run_startup_saga_observe", AsyncMock())
    return replace(
        MemorySettings.from_env(), artifact_reconcile_mode="off", structure_watcher_enabled=False,
        experience_counter_enabled=False, personal_memory_scalar_state_enabled=False,
        personal_memory_counting_enabled=False, verifier_sync_enabled=False,
    )


@pytest.mark.asyncio
@pytest.mark.online
async def test_registered_decision_timeline_corrects_erases_and_recovers_on_restart(
    test_neo4j_repo, graph_only_runtime_settings,
) -> None:
    settings = graph_only_runtime_settings
    project = "menhir"
    alpha, beta = "timeline-alpha", "timeline-beta"
    day = date(2026, 11, 26)

    async def entries(timeline, namespace, expected):
        async with asyncio.timeout(12):
            while True:
                result = await asyncio.to_thread(timeline.current, namespace=namespace, project=project)
                if result is not None and [entry["decision"] for entry in result["entries"]] == expected:
                    return result["entries"]
                await asyncio.sleep(0.05)

    registry = RuntimeExtensions((EXTENSION,))
    app = server.create_app(settings=settings, extensions=registry).app.app
    async with app.router.lifespan_context(app):
        ctx = app.state.runtime_ctx
        scheduler = ctx.scheduler
        assert ctx.built.graph_adapter.view_kinds is registry.view_kinds
        assert app.state.candidate_service.graph_adapter is ctx.built.graph_adapter
        lane = f"reconcile_projection:{EXTENSION.definition.definition_id}"
        assert scheduler is not None and lane in scheduler.status_snapshot()["jobs"]
        scheduler._projection_lanes[0]._jobs[lane].interval_s = 0
        timeline = DecisionTimeline(ctx.built.neo4j, ctx.built.graph_adapter._views)
        await asyncio.to_thread(timeline.initialize)
        for namespace, decision in (
            (alpha, Decision("later", day, "Approve v1", "#725")),
            (alpha, Decision("earlier", day.replace(day=25), "Draft v1", "#724")),
            (beta, Decision("later", day, "Independent decision", "#12")),
        ):
            await asyncio.to_thread(timeline.put, namespace=namespace, project=project, decision=decision)
        ordered = await entries(timeline, alpha, ["Draft v1", "Approve v1"])
        assert [entry["source"] for entry in ordered] == ["#724", "#725"]
        await entries(timeline, beta, ["Independent decision"])

        await asyncio.to_thread(
            timeline.put, namespace=alpha, project=project,
            decision=Decision("later", day, "Approve v2", "#725"),
        )
        await entries(timeline, alpha, ["Draft v1", "Approve v2"])
        await asyncio.to_thread(timeline.erase, namespace=alpha, project=project, decision_id="later")
        await entries(timeline, alpha, ["Draft v1"])
        await entries(timeline, beta, ["Independent decision"])
        assert not test_neo4j_repo.execute(
            """
            MATCH (n:Entity {view_kind:'project_decision_timeline', group_id:$namespace})
            WHERE n.view_payload CONTAINS 'Approve'
            RETURN n.uuid AS uuid
            """,
            {"namespace": alpha},
        )
        assert not test_neo4j_repo.execute(
            "MATCH (n:ExampleProjectDecision {namespace:$namespace, decision_id:'later'}) RETURN n",
            {"namespace": alpha},
        )

    views = registry.view_repository(test_neo4j_repo)
    offline = DecisionTimeline(test_neo4j_repo, views)
    offline.put(namespace=alpha, project=project, decision=Decision("new", day, "Resume after restart", "#726"))
    assert len(ProjectionLifecycleRepository(test_neo4j_repo).pending()) == 1
    restarted = server.create_app(settings=settings, extensions=registry).app.app
    async with restarted.router.lifespan_context(restarted):
        built = restarted.state.runtime_ctx.built
        recovered = DecisionTimeline(built.neo4j, built.graph_adapter._views)
        await entries(recovered, alpha, ["Draft v1", "Resume after restart"])
        await entries(recovered, beta, ["Independent decision"])
    assert ProjectionLifecycleRepository(test_neo4j_repo).pending() == ()


@pytest.mark.asyncio
@pytest.mark.online
async def test_http_projection_lifecycle_after_failed_startup_and_on_standby(
    graph_only_runtime_settings, test_neo4j_repo, monkeypatch,
) -> None:
    settings = graph_only_runtime_settings
    rejected = server.create_app(settings=settings).app.app
    healthy_preflight = runtime.collect_runtime_capabilities()
    with monkeypatch.context() as failing:
        failing.setattr(runtime, "collect_runtime_capabilities", lambda *_a, **_kw: replace(
            healthy_preflight, neo4j_ready=False, failures=("probe failed",),
        ))
        with pytest.raises(RuntimeError, match="probe failed"):
            async with rejected.router.lifespan_context(rejected):
                pytest.fail("the runtime accepted failed graph preflight")
    assert extension_module._process_registry is extension_module._UNBOUND

    materializer_entered = Event()
    release_materializer = Event()

    def materialize(views, token):
        materializer_entered.set()
        assert release_materializer.wait(15), "shutdown test did not release the materializer"
        return EXTENSION.materialize(views, token)

    registry = RuntimeExtensions((replace(EXTENSION, materialize=materialize),))
    app = server.create_app(settings=settings, extensions=registry).app.app
    lease = SchedulerLeaseStore()
    holder = {"lease_name": "maintenance_scheduler", "owner_id": "external-maintenance"}
    assert lease.try_acquire(**holder, owner_pid=os.getpid(), lease_duration_s=120)

    started = asyncio.Event()
    stop_requested = asyncio.Event()

    async def serve():
        async with app.router.lifespan_context(app):
            started.set()
            await stop_requested.wait()

    app_task = asyncio.create_task(serve())
    try:
        await asyncio.wait_for(started.wait(), 15)
        ctx = app.state.runtime_ctx
        scheduler = ctx.scheduler
        provider = RuntimeProvider(ctx.built, ctx.session)
        assert ctx.built.graph_adapter.view_kinds is registry.view_kinds
        assert scheduler.status_snapshot()["maintenance_running"] is False
        assert scheduler.status_snapshot()["running"] is True
        assert await provider.scheduler_pause() is True
        assert scheduler.status_snapshot()["running"] is False
        assert await provider.scheduler_pause() is False
        assert await provider.scheduler_resume() is True
        assert scheduler.status_snapshot()["maintenance_running"] is False
        assert scheduler.status_snapshot()["running"] is True

        (lane,) = scheduler._projection_lanes
        job = f"reconcile_projection:{EXTENSION.definition.definition_id}"
        lane._jobs[job].interval_s = 0
        stopping_lane = asyncio.Event()
        graph_closed = Event()
        stop_lane = lane.stop
        close_graph = ctx.built.neo4j.close

        async def observe_stop():
            stopping_lane.set()
            return await stop_lane()

        def observe_close():
            graph_closed.set()
            close_graph()

        monkeypatch.setattr(lane, "stop", observe_stop)
        monkeypatch.setattr(ctx.built.neo4j, "close", observe_close)
        timeline = DecisionTimeline(ctx.built.neo4j, ctx.built.graph_adapter._views)
        await asyncio.to_thread(timeline.initialize)
        await asyncio.to_thread(
            timeline.put, namespace="lifecycle", project="menhir",
            decision=Decision("one", date(2026, 11, 27), "Drain before graph close", "#725"),
        )
        assert await asyncio.to_thread(materializer_entered.wait, 8)
        stop_requested.set()
        await asyncio.wait_for(stopping_lane.wait(), 8)
        assert not graph_closed.is_set()
        release_materializer.set()
        await asyncio.wait_for(asyncio.shield(app_task), 15)
        assert graph_closed.is_set()
        persisted = DecisionTimeline(test_neo4j_repo, registry.view_repository(test_neo4j_repo))
        current = await asyncio.to_thread(persisted.current, namespace="lifecycle", project="menhir")
        assert current is not None
        assert [entry["id"] for entry in current["entries"]] == ["one"]
    finally:
        release_materializer.set()
        stop_requested.set()
        try:
            await asyncio.wait_for(app_task, 15)
        finally:
            lease.release(**holder)
