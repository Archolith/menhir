"""Flag parsing and forwarding for MENHIR_SCALAR_DETERMINISTIC_SHADOW (Phase 2A).

Covers: default, env parse, scheduler bootstrap hop, manual API consolidation hop, and the
consolidation -> ScalarConsolidationConfig -> TypedScalarPerceptionService hops. Flag off is
byte/behavior equivalent by construction (every hop is additive and default-off).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from menhir.config.settings_model import MemorySettings
from menhir.services.maintenance_scheduler import MaintenanceScheduler
from menhir.services.scalar_consolidation import ScalarConsolidationConfig, run_scalar_consolidation


# ------------------------------------------------------------------------------------------------ #
# Setting default + exact env name
# ------------------------------------------------------------------------------------------------ #


def test_flag_defaults_off():
    assert MemorySettings().personal_memory_scalar_deterministic_shadow is False


def test_env_parses_exact_flag_name(monkeypatch):
    monkeypatch.setenv("MENHIR_SCALAR_DETERMINISTIC_SHADOW", "true")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_shadow is True
    monkeypatch.setenv("MENHIR_SCALAR_DETERMINISTIC_SHADOW", "false")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_shadow is False
    monkeypatch.setenv("MENHIR_SCALAR_DETERMINISTIC_SHADOW", "1")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_shadow is True


# ------------------------------------------------------------------------------------------------ #
# Scheduler bootstrap hop: runtime -> MaintenanceScheduler
# ------------------------------------------------------------------------------------------------ #


def test_runtime_forwards_flag_into_scheduler(monkeypatch):
    import menhir.core.runtime as runtime

    captured: dict[str, object] = {}

    class _FakeScheduler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

        def status_snapshot(self):
            return {"running": True}

    monkeypatch.setattr(runtime, "MaintenanceScheduler", _FakeScheduler)
    monkeypatch.setattr(runtime, "_state", SimpleNamespace(scheduler=None))
    monkeypatch.setattr(runtime, "make_view_embedder", lambda settings: None)
    monkeypatch.setattr(runtime, "view_embedder_version", lambda settings: "test")

    settings = MemorySettings(
        personal_memory_scalar_deterministic_shadow=True,
        personal_memory_consolidation_enabled=False,
        verifier_sync_enabled=False,
        structure_watcher_enabled=False,
        personal_memory_consolidation_audit_enabled=False,
        personal_memory_recall_audit_enabled=False,
    )
    built = SimpleNamespace(
        settings=settings,
        ingest_service=object(),
        graph_adapter=SimpleNamespace(neo4j=None),
        lifecycle_service=None,
    )

    async def _run():
        return await runtime._start_scheduler(built)

    asyncio.run(_run())
    assert captured["scalar_deterministic_shadow_enabled"] is True


# ------------------------------------------------------------------------------------------------ #
# Scheduler hop: MaintenanceScheduler -> consolidate_personal_memory
# ------------------------------------------------------------------------------------------------ #


def test_maintenance_scheduler_forwards_flag(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_consolidate(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.consolidate_personal_memory",
        _fake_consolidate,
    )
    scheduler = MaintenanceScheduler(
        ingest_service=object(),
        graph_adapter=object(),
        scalar_state_enabled=True,
        scalar_deterministic_shadow_enabled=True,
    )
    asyncio.run(scheduler._make_consolidate_personal_memory())
    assert captured["scalar_deterministic_shadow_enabled"] is True


def test_maintenance_scheduler_flag_default_off():
    scheduler = MaintenanceScheduler(ingest_service=object(), graph_adapter=object())
    assert scheduler.scalar_deterministic_shadow_enabled is False


# ------------------------------------------------------------------------------------------------ #
# Consolidation hop: consolidate_personal_memory -> ScalarConsolidationConfig
# ------------------------------------------------------------------------------------------------ #


def test_consolidation_forwards_flag_into_scalar_config(monkeypatch):
    from menhir.services.scheduler_tasks import consolidate_personal_memory

    captured: dict[str, object] = {}

    def _fake_run(graph_adapter, *, scalar_targets, namespaces, counting_llm, build_episodes,
                  k, threshold, call_budget, embed, config):
        captured["scalar_targets"] = scalar_targets
        captured["config"] = config
        return {}

    monkeypatch.setattr(
        "menhir.services.scheduler_tasks.run_scalar_consolidation", _fake_run)

    async def _run():
        return await consolidate_personal_memory(
            graph_adapter=SimpleNamespace(),
            llm_complete=lambda system, user: "[]",
            embed=None,
            namespaces=["ns-x"],
            k=3,
            enable_counter_state=False,
            enable_scalar_state=True,
            scalar_state_perceiver_version="v1",
            scalar_deterministic_shadow_enabled=True,
            call_budget=100,
        )

    asyncio.run(_run())
    assert captured["scalar_targets"] == ["ns-x"]
    assert captured["config"].deterministic_shadow_enabled is True


def test_scalar_config_default_off():
    assert ScalarConsolidationConfig().deterministic_shadow_enabled is False


# ------------------------------------------------------------------------------------------------ #
# Deep hop: ScalarConsolidationConfig -> TypedScalarPerceptionService
# ------------------------------------------------------------------------------------------------ #


class _FakeScalarGraph:
    def scalar_state_service(self, scalar_history_enabled=False):
        return SimpleNamespace(
            rebuild_scalar_state=lambda *a, **k: None,
            rebuild_scalar_projections=lambda *a, **k: None,
            activate_due_assertions=lambda **k: {"claimed": 0, "repaired": [], "failed": []},
            repair_incomplete_reconciliations=lambda **k: {"repaired": 0},
            repair_orphaned_assertions=lambda **k: {"repaired": [], "unresolved": []},
        )

    def load_next_scalar_batch(self, namespace, *, perceiver_version, limit):
        return [{
            "uuid": "row-1",
            "content": "I have 37 coins.",
            "valid_at": "2026-01-01T00:00:00Z",
            "cursor_at": "2026-01-01T00:00:00Z",
        }]

    def advance_scalar_cursor(self, namespace, *, cursor_at, cursor_uuid, perceiver_version, at):
        return None

    def retire_counters_superseded_by_scalar(self, *, namespace):
        return 0


def test_scalar_config_forwards_into_perception_service(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, adapter, scalar_state_service, **kwargs):
            captured.update(kwargs)

        def ensure_activated(self):
            return None

        def repair_pending_bindings(self, **kwargs):
            return {}

    monkeypatch.setattr(
        "menhir.services.typed_scalar_perception.TypedScalarPerceptionService",
        _FakeService,
    )
    result = run_scalar_consolidation(
        graph_adapter=_FakeScalarGraph(),
        scalar_targets=["ns-x"],
        namespaces=None,
        counting_llm=SimpleNamespace(calls=0),
        build_episodes=lambda rows: [],
        k=3,
        threshold=1.0,
        call_budget=None,
        embed=None,
        config=ScalarConsolidationConfig(deterministic_shadow_enabled=True),
    )
    assert captured["deterministic_shadow_enabled"] is True
    assert result["scalar_namespaces_processed"] == 1


# ------------------------------------------------------------------------------------------------ #
# Manual API consolidation hop: routes_handlers.phase3_run_impl -> consolidate_personal_memory
# ------------------------------------------------------------------------------------------------ #


def test_phase3_run_forwards_flag_from_settings(monkeypatch):
    import menhir.services.scheduler_tasks as scheduler_tasks

    captured: dict[str, object] = {}

    async def _fake_consolidate(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(scheduler_tasks, "consolidate_personal_memory", _fake_consolidate)
    monkeypatch.setattr(
        "menhir.infrastructure.sync_llm.make_sync_chat",
        lambda settings, model=None, max_tokens=None: lambda system, user: "[]",
    )
    monkeypatch.setattr(
        "menhir.infrastructure.view_embedder.make_view_embedder", lambda settings: None)
    monkeypatch.setattr(
        "menhir.infrastructure.view_embedder.view_embedder_version", lambda settings: "test")

    settings = MemorySettings(
        personal_memory_scalar_deterministic_shadow=True,
        personal_memory_consolidation_chat_model="",
        personal_memory_consolidation_max_tokens=2048,
        personal_memory_consolidation_verify_retries=0,
        personal_memory_consolidation_sum_grounding=False,
        personal_memory_scalar_state_enabled=True,
        personal_memory_scalar_state_perceiver_version="v1",
        personal_memory_scalar_reconcile_attribute=False,
        personal_memory_scalar_reconcile_scope=False,
        personal_memory_scalar_reconcile_subject=False,
        personal_memory_scalar_canonical_self=False,
        personal_memory_scalar_threshold=1.0,
        personal_memory_scalar_history_enabled=False,
    )
    from menhir.api.routes_handlers import phase3_run_impl
    from menhir.api.routes_support import Phase3RunRequest

    body = Phase3RunRequest(namespace="ns-x", k=3)
    adapter = SimpleNamespace(list_dirty_namespaces=lambda limit: ["ns-x"])
    built = SimpleNamespace(settings=settings)

    async def _run():
        return await phase3_run_impl(
            request=SimpleNamespace(),
            body=body,
            require_tier=lambda tier: None,
            require_phase3_adapter=lambda request: (SimpleNamespace(built=built), adapter),
        )

    asyncio.run(_run())
    assert captured["scalar_deterministic_shadow_enabled"] is True
