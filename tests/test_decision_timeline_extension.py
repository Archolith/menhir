"""The synthetic decision timeline through the actual HTTP runtime lifespan."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock

import pytest

from examples.decision_timeline import EXTENSION, Decision, DecisionTimeline
from menhir.api import server
from menhir.config import MemorySettings
from menhir.core import bootstrap, runtime
from menhir.core.runtime_preflight import RuntimeCapabilities
from menhir.extensions import RuntimeExtensions
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION


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


@pytest.mark.asyncio
@pytest.mark.online
async def test_registered_decision_timeline_corrects_erases_and_recovers_on_restart(
    test_neo4j_repo, monkeypatch, tmp_path,
) -> None:
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
    settings = replace(
        MemorySettings.from_env(), artifact_reconcile_mode="off", structure_watcher_enabled=False,
        experience_counter_enabled=False, personal_memory_scalar_state_enabled=False,
        personal_memory_counting_enabled=False, verifier_sync_enabled=False,
    )
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

    app = server.create_app(settings=settings, extensions=(EXTENSION,)).app.app
    async with app.router.lifespan_context(app):
        ctx = app.state.runtime_ctx
        scheduler = ctx.scheduler
        lane = f"reconcile_projection:{EXTENSION.definition.definition_id}"
        assert scheduler is not None and lane in scheduler.status_snapshot()["jobs"]
        scheduler._jobs[lane].interval_s = 0
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

    views = RuntimeExtensions((EXTENSION,)).view_repository(test_neo4j_repo)
    offline = DecisionTimeline(test_neo4j_repo, views)
    offline.put(namespace=alpha, project=project, decision=Decision("new", day, "Resume after restart", "#726"))
    assert len(ProjectionLifecycleRepository(test_neo4j_repo).pending()) == 1
    restarted = server.create_app(settings=settings, extensions=(EXTENSION,)).app.app
    async with restarted.router.lifespan_context(restarted):
        built = restarted.state.runtime_ctx.built
        recovered = DecisionTimeline(built.neo4j, built.graph_adapter._views)
        await entries(recovered, alpha, ["Draft v1", "Resume after restart"])
        await entries(recovered, beta, ["Independent decision"])
    assert ProjectionLifecycleRepository(test_neo4j_repo).pending() == ()
