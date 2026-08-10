"""Tests for capability-aware startup, degraded bootstrap, and sentinel classes.

Covers Phase 8 requirements:
- RuntimeCapabilities derived properties and startup modes
- UnavailableGraphitiClient / UnavailableLLMAdapter sentinel behavior
- build_memory_services conditional construction with capabilities
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from menhir.config import MemorySettings
from menhir.core import build_memory_services
from menhir.core.bootstrap import (
    UnavailableGraphitiClient,
    UnavailableLLMAdapter,
)
from menhir.core import runtime as runtime_mod
from menhir.core.runtime_preflight import RuntimeCapabilities


# ---------------------------------------------------------------------------
# RuntimeCapabilities derived properties
# ---------------------------------------------------------------------------


def _caps(**overrides: Any) -> RuntimeCapabilities:
    defaults = dict(
        venv_ready=True,
        graphiti_dependency_ready=True,
        neo4j_ready=True,
        graphiti_llm_ready=True,
        embedder_ready=True,
        reranker_ready=True,
        scheduler_required=False,
        failures=(),
    )
    defaults.update(overrides)
    return RuntimeCapabilities(**defaults)


class TestRuntimeCapabilities:
    @pytest.mark.unit
    def test_full_mode_when_all_ready(self):
        caps = _caps()
        assert caps.startup_mode == "full"
        assert caps.reads_ready is True
        assert caps.queue_writes_ready is True
        assert caps.enrichment_ready is True
        assert caps.scheduler_ready is True

    @pytest.mark.unit
    def test_degraded_reads_only_when_llm_down(self):
        caps = _caps(graphiti_llm_ready=False)
        assert caps.startup_mode == "degraded_reads_only"
        assert caps.reads_ready is True
        assert caps.enrichment_ready is False
        assert caps.llm_ready is False

    @pytest.mark.unit
    def test_degraded_queue_only_when_embedder_down(self):
        caps = _caps(graphiti_llm_ready=False, embedder_ready=False)
        assert caps.startup_mode == "degraded_queue_only"
        assert caps.reads_ready is False
        assert caps.queue_writes_ready is True
        assert caps.enrichment_ready is False

    @pytest.mark.unit
    def test_unavailable_when_neo4j_down(self):
        caps = _caps(neo4j_ready=False, graphiti_llm_ready=False, embedder_ready=False)
        assert caps.startup_mode == "unavailable"
        assert caps.queue_writes_ready is False

    @pytest.mark.unit
    def test_graphiti_ready_requires_neo4j_and_embedder(self):
        assert _caps().graphiti_ready is True
        assert _caps(embedder_ready=False).graphiti_ready is False
        assert _caps(neo4j_ready=False).graphiti_ready is False

    @pytest.mark.unit
    def test_scheduler_ready_when_not_required(self):
        caps = _caps(scheduler_required=False, graphiti_llm_ready=False, embedder_ready=False)
        assert caps.scheduler_ready is True

    @pytest.mark.unit
    def test_scheduler_not_ready_when_required_but_enrichment_down(self):
        caps = _caps(scheduler_required=True, graphiti_llm_ready=False)
        assert caps.scheduler_ready is False

    @pytest.mark.unit
    def test_scheduler_ready_when_required_and_enrichment_up(self):
        caps = _caps(scheduler_required=True)
        assert caps.scheduler_ready is True

    @pytest.mark.unit
    def test_llm_ready_aliases_graphiti_llm_ready(self):
        assert _caps(graphiti_llm_ready=True).llm_ready is True
        assert _caps(graphiti_llm_ready=False).llm_ready is False

    @pytest.mark.unit
    def test_failures_tuple_propagated(self):
        caps = _caps(failures=("Neo4j down", "LLM unreachable"))
        assert len(caps.failures) == 2
        assert caps.is_strictly_startable is False

    @pytest.mark.unit
    def test_no_failures_is_strictly_startable(self):
        caps = _caps()
        assert caps.is_strictly_startable is True


# ---------------------------------------------------------------------------
# UnavailableGraphitiClient sentinel
# ---------------------------------------------------------------------------


class TestUnavailableGraphitiClient:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_add_episode(self):
        client = UnavailableGraphitiClient("test reason")
        with pytest.raises(RuntimeError, match="test reason"):
            await client.add_episode(name="test")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_search(self):
        client = UnavailableGraphitiClient("unavailable")
        with pytest.raises(RuntimeError, match="unavailable"):
            await client.search("query")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_search_scored(self):
        client = UnavailableGraphitiClient("down")
        with pytest.raises(RuntimeError, match="down"):
            await client.search_scored("query")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_build_indices(self):
        client = UnavailableGraphitiClient("reason")
        with pytest.raises(RuntimeError, match="reason"):
            await client.build_indices_and_constraints()

    @pytest.mark.unit
    def test_circuit_breaker_returns_safe_defaults(self):
        client = UnavailableGraphitiClient("reason")
        snapshots = client.circuit_breaker_snapshots()
        assert snapshots["llm"]["state"] == "unavailable"
        assert snapshots["embed"]["state"] == "unavailable"
        assert snapshots["reranker"]["state"] == "unavailable"

    @pytest.mark.unit
    def test_embedding_cache_returns_zeros(self):
        client = UnavailableGraphitiClient("reason")
        stats = client.embedding_cache_stats()
        assert stats == {"hits": 0, "misses": 0, "size": 0}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_close_is_noop(self):
        client = UnavailableGraphitiClient("reason")
        result = await client.close()
        assert result is None

    @pytest.mark.unit
    def test_sentinel_attributes_are_safe_defaults(self):
        client = UnavailableGraphitiClient("reason")
        assert client.scheduler_fallback_base_url == ""
        assert client.scheduler_fallback_embed_base_url == ""
        assert client.scheduler_fallback_reranker_base_url == ""
        assert client.llm_client_ref is None
        assert client.embedder_ref is None
        assert client.reranker_ref is None


# ---------------------------------------------------------------------------
# UnavailableLLMAdapter sentinel
# ---------------------------------------------------------------------------


class TestUnavailableLLMAdapter:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_compress(self):
        adapter = UnavailableLLMAdapter("llm down")
        with pytest.raises(RuntimeError, match="llm down"):
            await adapter.compress_content("text")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_merge(self):
        adapter = UnavailableLLMAdapter("llm down")
        with pytest.raises(RuntimeError, match="llm down"):
            await adapter.merge_content("a", "b")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_confirm_contradiction(self):
        adapter = UnavailableLLMAdapter("offline")
        with pytest.raises(RuntimeError, match="offline"):
            await adapter.confirm_contradiction(name_a="a", content_a="c", name_b="b", content_b="d")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raises_on_repair_edge_facts(self):
        adapter = UnavailableLLMAdapter("offline")
        with pytest.raises(RuntimeError, match="offline"):
            await adapter.repair_edge_facts("content", [])

    @pytest.mark.unit
    def test_safe_model_name_defaults(self):
        adapter = UnavailableLLMAdapter("reason")
        assert adapter.chat_model_name() == ""
        assert adapter.embed_model_name() == ""

    @pytest.mark.unit
    def test_safe_attribute_defaults(self):
        adapter = UnavailableLLMAdapter("reason")
        assert adapter.base_url == ""
        assert adapter.api_key == ""
        assert adapter.chat_model == ""
        assert adapter.embed_model == ""
        assert adapter.backend is None


# ---------------------------------------------------------------------------
# build_memory_services capability-aware construction
# ---------------------------------------------------------------------------


class TestBuildMemoryServicesCapabilities:
    @pytest.mark.unit
    def test_queue_only_capabilities_produce_sentinels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: object())

        caps = _caps(graphiti_llm_ready=False, embedder_ready=False, reranker_ready=False)
        built = build_memory_services(settings=MemorySettings(), capabilities=caps)

        assert isinstance(built.graphiti_client, UnavailableGraphitiClient)
        assert isinstance(built.llm, UnavailableLLMAdapter)
        assert built.ingest_service.enrichment_enabled() is False
        assert built.capabilities is caps


# ---------------------------------------------------------------------------
# runtime._initialize_services degraded boot behavior
# ---------------------------------------------------------------------------


class TestInitializeServicesDegradedMode:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_initialize_services_allows_partial_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        caps = _caps(graphiti_llm_ready=False, embedder_ready=True, scheduler_required=True)
        settings = MemorySettings()
        session = SimpleNamespace(session_id="session-test")
        calls: list[str] = []

        built = SimpleNamespace(
            settings=settings,
            ingest_service=SimpleNamespace(
                resume_pending_episodes=AsyncMock(side_effect=lambda: calls.append("resume"))
            ),
            lifecycle_service=SimpleNamespace(
                recover_orphans=AsyncMock(side_effect=lambda: calls.append("orphans") or SimpleNamespace(promoted=0, deleted=0))
            ),
        )

        async def _fake_prepare(_built: object) -> dict[str, object]:
            calls.append("prepare")
            return {"graphiti": "ok"}

        try:
            runtime_mod._state.clear_all()
            monkeypatch.setattr(runtime_mod.MemorySettings, "from_env", classmethod(lambda cls: settings))
            monkeypatch.setattr(runtime_mod, "_uses_scheduler_managed_graphiti", lambda _s: False)
            monkeypatch.setattr(runtime_mod, "collect_runtime_capabilities", lambda *args, **kwargs: caps)
            monkeypatch.setattr(runtime_mod, "build_memory_services", lambda *args, **kwargs: built)
            monkeypatch.setattr(runtime_mod, "prepare_memory_runtime", _fake_prepare)
            monkeypatch.setattr(runtime_mod, "new_session", lambda user_id: session)
            start_scheduler = AsyncMock()
            monkeypatch.setattr(runtime_mod, "_start_scheduler", start_scheduler)

            resolved_built, resolved_session = await runtime_mod._initialize_services()

            # orphan recovery runs as a background task — drain it before asserting
            orphan_task = runtime_mod._state.orphan_recovery_task
            if orphan_task is not None:
                await orphan_task

            assert resolved_built is built
            assert resolved_session is session
            assert runtime_mod._state.built is built
            assert runtime_mod._state.session is session
            assert runtime_mod._state.capabilities is caps
            assert start_scheduler.await_count == 0
            assert calls == ["prepare", "resume", "orphans"]
        finally:
            runtime_mod._state.clear_all()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_initialize_services_starts_internal_scheduler_for_direct_provider_when_enrichment_ready(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Bug AR-01 regression: a direct OpenAI/Gemini Graphiti config does not use the external
        # yawn.scheduler process (uses_scheduler=False), but it IS enrichment-ready and therefore
        # still needs the in-process maintenance scheduler (stale-lease recovery, retry, conflict
        # work, structure refresh). It must start regardless of model-endpoint ownership.
        caps = _caps()
        settings = MemorySettings(
            graphiti_provider="openai",
            graphiti_embed_provider="openai",
            graphiti_reranker_provider="openai",
        )
        session = SimpleNamespace(session_id="session-openai")

        built = SimpleNamespace(
            settings=settings,
            ingest_service=SimpleNamespace(resume_pending_episodes=AsyncMock()),
            lifecycle_service=SimpleNamespace(
                recover_orphans=AsyncMock(return_value=SimpleNamespace(promoted=0, deleted=0))
            ),
        )

        async def _fake_prepare(_built: object) -> dict[str, object]:
            return {"graphiti": "ok"}

        try:
            runtime_mod._state.clear_all()
            monkeypatch.setattr(runtime_mod.MemorySettings, "from_env", classmethod(lambda cls: settings))
            monkeypatch.setattr(runtime_mod, "_uses_scheduler_managed_graphiti", lambda _s: False)
            monkeypatch.setattr(runtime_mod, "collect_runtime_capabilities", lambda *args, **kwargs: caps)
            monkeypatch.setattr(runtime_mod, "build_memory_services", lambda *args, **kwargs: built)
            monkeypatch.setattr(runtime_mod, "prepare_memory_runtime", _fake_prepare)
            monkeypatch.setattr(runtime_mod, "new_session", lambda user_id: session)
            start_scheduler = AsyncMock()
            monkeypatch.setattr(runtime_mod, "_start_scheduler", start_scheduler)

            resolved_built, resolved_session = await runtime_mod._initialize_services()

            orphan_task = runtime_mod._state.orphan_recovery_task
            if orphan_task is not None:
                await orphan_task

            assert resolved_built is built
            assert resolved_session is session
            assert start_scheduler.await_count == 1
        finally:
            runtime_mod._state.clear_all()


# ---------------------------------------------------------------------------
# Online degraded-startup integration placeholders
# ---------------------------------------------------------------------------


@pytest.mark.online
def test_server_starts_when_llm_endpoints_down() -> None:
    pytest.skip("TODO: degraded startup integration coverage for server boot without LLM endpoints.")


@pytest.mark.online
def test_reads_work_in_degraded_mode() -> None:
    pytest.skip("TODO: degraded-read integration coverage with Neo4j/embedder up and LLM down.")


@pytest.mark.online
def test_writes_queue_in_degraded_mode() -> None:
    pytest.skip("TODO: degraded queue-write integration coverage with Neo4j up and enrichment unavailable.")


# ---------------------------------------------------------------------------
# Edge-count sync degradation (prepare_memory_runtime)
# ---------------------------------------------------------------------------
#
# sync_edge_counts is a full-graph recount in ONE transaction, so a single damaged record
# fails the whole statement. It feeds decay-protection thresholds only and is not needed to
# serve reads, so a failure must degrade like the Graphiti/LLM sentinels above rather than
# abort startup -- trading a degraded feature for a total outage is the wrong trade.


def _artifacts_with_edge_sync(side_effect=None, value=7):
    """Minimal BuildArtifacts stand-in: prepare_memory_runtime only touches capabilities and
    graph_adapter on the path under test."""
    adapter = SimpleNamespace(
        phase_one_schema_ready=lambda: False,  # forces the real bootstrap branch below
        bootstrap_phase_one=lambda: SimpleNamespace(
            success=True, queries_executed=1, failures=[]
        ),
        sync_edge_counts=(
            (lambda: (_ for _ in ()).throw(side_effect)) if side_effect else (lambda: value)
        ),
    )
    return SimpleNamespace(
        # graphiti_ready is a DERIVED property on RuntimeCapabilities, not a constructor arg.
        # prepare_memory_runtime reads it with getattr, so a namespace both works and avoids
        # coupling this test to how the real capability is computed. False skips the Graphiti
        # branch, which is not what is under test here.
        capabilities=SimpleNamespace(graphiti_ready=False),
        graph_adapter=adapter,
        graphiti_client=None,
    )


@pytest.mark.asyncio
async def test_edge_count_sync_failure_does_not_abort_startup():
    """The regression this guards: an unhandled exception here refused to boot the server."""
    from menhir.core.bootstrap import prepare_memory_runtime

    result = await prepare_memory_runtime(
        _artifacts_with_edge_sync(side_effect=RuntimeError("corrupt property chain"))
    )

    assert result["edge_count_sync"] == 0
    assert "corrupt property chain" in str(result["edge_count_sync_error"])


@pytest.mark.asyncio
async def test_edge_count_sync_failure_is_reported_not_swallowed():
    """Degrading silently would be its own bug: the operator must be able to tell stale counts
    from a real zero. The error field is how startup says which one happened."""
    from menhir.core.bootstrap import prepare_memory_runtime

    degraded = await prepare_memory_runtime(
        _artifacts_with_edge_sync(side_effect=RuntimeError("neo4j down"))
    )
    genuine_zero = await prepare_memory_runtime(_artifacts_with_edge_sync(value=0))

    assert degraded["edge_count_sync"] == genuine_zero["edge_count_sync"] == 0
    assert degraded["edge_count_sync_error"] is not None
    assert genuine_zero["edge_count_sync_error"] is None, (
        "a successful sync must not report an error, or the field cannot distinguish them"
    )


@pytest.mark.asyncio
async def test_successful_edge_count_sync_is_passed_through():
    from menhir.core.bootstrap import prepare_memory_runtime

    result = await prepare_memory_runtime(_artifacts_with_edge_sync(value=42))
    assert result["edge_count_sync"] == 42
    assert result["edge_count_sync_error"] is None


@pytest.mark.asyncio
async def test_a_failing_edge_count_sync_still_reports_schema_bootstrap():
    """Degradation must be scoped to the failing step -- the rest of the startup report stays
    truthful, or an operator reading it cannot trust any of it."""
    from menhir.core.bootstrap import prepare_memory_runtime

    result = await prepare_memory_runtime(
        _artifacts_with_edge_sync(side_effect=RuntimeError("boom"))
    )
    assert result["schema"]["status"] == "ok"
    assert result["schema"]["skipped"] is False
