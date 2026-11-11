"""Scheduler-lease wiring: runtime -> build_memory_services -> IngestService (offline).

CF-340 (part). The single-process rule makes an enrichment worker strict about the scheduler lease only
where the same runtime starts the maintenance scheduler. That is decided in one expression in
``runtime._initialize_services`` and forwarded by ``bootstrap.build_memory_services`` into
``IngestService.require_scheduler_lease``. ``tests/test_degraded_startup.py`` stubs
``build_memory_services`` with a lambda that drops its kwargs, so nothing else pins the wiring.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from menhir.config import MemorySettings
from menhir.core import build_memory_services
from menhir.core import runtime as runtime_mod
from menhir.core.runtime_preflight import RuntimeCapabilities


def _caps(**overrides: Any) -> RuntimeCapabilities:
    defaults = dict(
        venv_ready=True,
        graphiti_dependency_ready=True,
        neo4j_ready=True,
        graphiti_llm_ready=True,
        embedder_ready=True,
        reranker_ready=True,
        failures=(),
    )
    defaults.update(overrides)
    return RuntimeCapabilities(**defaults)


async def _drive_initialize_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: MemorySettings,
    caps: RuntimeCapabilities,
    candidate_readonly: bool,
) -> dict[str, Any]:
    """Run the real ``_initialize_services`` with a capturing ``build_memory_services`` stub."""
    captured: dict[str, Any] = {}
    session = SimpleNamespace(session_id="session-lease-wiring")
    built = SimpleNamespace(
        settings=settings,
        neo4j=object(),
        ingest_service=SimpleNamespace(resume_pending_episodes=AsyncMock()),
        lifecycle_service=SimpleNamespace(
            recover_orphans=AsyncMock(return_value=SimpleNamespace(promoted=0, deleted=0))
        ),
    )

    def capturing_build(*args: Any, **kwargs: Any) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return built

    async def fake_prepare(_built: object) -> dict[str, object]:
        return {"graphiti": "ok"}

    class _ReadonlyAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def phase_one_schema_ready(self) -> bool:
            return True

    try:
        runtime_mod._state.clear_all()
        monkeypatch.setattr(runtime_mod.MemorySettings, "from_env", classmethod(lambda cls: settings))
        monkeypatch.setattr(runtime_mod, "collect_runtime_capabilities", lambda *a, **k: caps)
        monkeypatch.setattr(runtime_mod, "build_memory_services", capturing_build)
        monkeypatch.setattr(runtime_mod, "prepare_memory_runtime", fake_prepare)
        monkeypatch.setattr(runtime_mod, "new_session", lambda user_id: session)
        monkeypatch.setattr(runtime_mod, "_start_scheduler", AsyncMock())
        monkeypatch.setattr(
            "menhir.services.cross_encoder_rerank.start_model_fetch", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter", _ReadonlyAdapter
        )

        await runtime_mod._initialize_services(candidate_readonly=candidate_readonly)

        orphan_task = runtime_mod._state.orphan_recovery_task
        if orphan_task is not None:
            await orphan_task
    finally:
        runtime_mod._state.clear_all()
    return captured


class TestRuntimePassesRequireSchedulerLease:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_true_when_serving_with_enrichment_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = await _drive_initialize_services(
            monkeypatch,
            settings=MemorySettings(benchmark_mode=False),
            caps=_caps(),
            candidate_readonly=False,
        )

        assert captured["kwargs"]["require_scheduler_lease"] is True
        assert captured["kwargs"]["read_only"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_false_when_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = await _drive_initialize_services(
            monkeypatch,
            settings=MemorySettings(benchmark_mode=False),
            caps=_caps(),
            candidate_readonly=True,
        )

        assert captured["kwargs"]["read_only"] is True
        assert captured["kwargs"]["require_scheduler_lease"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_false_in_benchmark_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = await _drive_initialize_services(
            monkeypatch,
            settings=MemorySettings(benchmark_mode=True),
            caps=_caps(),
            candidate_readonly=False,
        )

        assert captured["kwargs"]["require_scheduler_lease"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_false_when_enrichment_is_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        caps = _caps(graphiti_llm_ready=False)
        assert caps.enrichment_ready is False  # precondition: the degraded mode under test

        captured = await _drive_initialize_services(
            monkeypatch,
            settings=MemorySettings(benchmark_mode=False),
            caps=caps,
            candidate_readonly=False,
        )

        assert captured["kwargs"]["require_scheduler_lease"] is False


class TestBootstrapForwardsRequireSchedulerLease:
    @pytest.mark.unit
    @pytest.mark.parametrize("flag", [True, False])
    def test_flag_reaches_the_ingest_service(self, monkeypatch: pytest.MonkeyPatch, flag: bool) -> None:
        monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: object())
        caps = _caps(graphiti_llm_ready=False, embedder_ready=False, reranker_ready=False)

        built = build_memory_services(
            settings=MemorySettings(), capabilities=caps, require_scheduler_lease=flag
        )

        assert built.ingest_service.require_scheduler_lease is flag

    @pytest.mark.unit
    def test_default_is_not_strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("menhir.core.bootstrap.Neo4jRepository", lambda **kwargs: object())
        caps = _caps(graphiti_llm_ready=False, embedder_ready=False, reranker_ready=False)

        built = build_memory_services(settings=MemorySettings(), capabilities=caps)

        assert built.ingest_service.require_scheduler_lease is False
