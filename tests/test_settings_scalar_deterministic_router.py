"""Default-off wiring for the personal-memory deterministic scalar router flag."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from menhir.config.settings_model import MemorySettings
from menhir.services.maintenance_scheduler import MaintenanceScheduler
from menhir.services.scalar_consolidation import ScalarConsolidationConfig, run_scalar_consolidation


def test_router_flag_defaults_off_and_parses_exact_env_name(monkeypatch):
    assert MemorySettings().personal_memory_scalar_deterministic_router is False
    assert MemorySettings().personal_memory_scalar_deterministic_classes == ()
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER", "true")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_router is True
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_ROUTER", "false")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_router is False
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_DETERMINISTIC_CLASSES", " C_COUNT, C_MONEY,c_count,, C_MONEY ")
    assert MemorySettings.from_env().personal_memory_scalar_deterministic_classes == ("c_count", "c_money")


def test_runtime_forwards_router_flag_into_scheduler(monkeypatch):
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
        personal_memory_scalar_deterministic_router=True,
        personal_memory_scalar_deterministic_classes=("c_count", "c_money"),
        personal_memory_consolidation_enabled=False,
        verifier_sync_enabled=False,
        structure_watcher_enabled=False,
        personal_memory_consolidation_audit_enabled=False,
        personal_memory_recall_audit_enabled=False,
    )
    built = SimpleNamespace(settings=settings, ingest_service=object(), graph_adapter=SimpleNamespace(neo4j=None), lifecycle_service=None)

    async def _run():
        return await runtime._start_scheduler(built)

    asyncio.run(_run())
    assert captured["scalar_deterministic_router_enabled"] is True
    assert captured["scalar_deterministic_router_promoted_classes"] == ("c_count", "c_money")


def test_scheduler_forwards_router_flag_to_consolidation(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_consolidate(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("menhir.services.maintenance_scheduler.consolidate_personal_memory", _fake_consolidate)
    scheduler = MaintenanceScheduler(
        ingest_service=object(), graph_adapter=object(), scalar_state_enabled=True,
        scalar_deterministic_router_enabled=True,
        scalar_deterministic_router_promoted_classes=("c_count", "c_money"),
    )
    asyncio.run(scheduler._make_consolidate_personal_memory())
    assert captured["scalar_deterministic_router_enabled"] is True
    assert captured["scalar_deterministic_router_promoted_classes"] == ("c_count", "c_money")
    default_scheduler = MaintenanceScheduler(ingest_service=object(), graph_adapter=object())
    assert default_scheduler.scalar_deterministic_router_enabled is False
    assert default_scheduler.scalar_deterministic_router_promoted_classes == ()


def test_consolidation_forwards_router_flag_into_scalar_config(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_run(*_args, **kwargs):
        captured["config"] = kwargs["config"]
        return {}

    monkeypatch.setattr("menhir.services.scheduler_tasks.run_scalar_consolidation", _fake_run)

    async def _run():
        from menhir.services.scheduler_tasks import consolidate_personal_memory

        return await consolidate_personal_memory(
            graph_adapter=SimpleNamespace(),
            llm_complete=lambda _system, _user: "[]",
            namespaces=["ns-x"],
            enable_counter_state=False,
            enable_scalar_state=True,
            scalar_deterministic_router_enabled=True,
            scalar_deterministic_router_promoted_classes=("c_count", "c_money"),
        )

    asyncio.run(_run())
    assert captured["config"].deterministic_router_enabled is True
    assert captured["config"].deterministic_router_promoted_classes == ("c_count", "c_money")
    assert ScalarConsolidationConfig().deterministic_router_enabled is False
    assert ScalarConsolidationConfig().deterministic_router_promoted_classes == ()


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
        return [{"uuid": "row-1", "content": "I have 37 coins.", "valid_at": "2026-01-01T00:00:00Z", "cursor_at": "2026-01-01T00:00:00Z"}]

    def advance_scalar_cursor(self, namespace, *, cursor_at, cursor_uuid, perceiver_version, at):
        return None

    def retire_counters_superseded_by_scalar(self, *, namespace):
        return 0


def test_scalar_config_forwards_router_flag_into_perception_service(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeService:
        def __init__(self, adapter, scalar_state_service, **kwargs):
            captured.update(kwargs)

        def ensure_activated(self):
            return None

        def repair_pending_bindings(self, **kwargs):
            return {}

    monkeypatch.setattr("menhir.services.typed_scalar_perception.TypedScalarPerceptionService", _FakeService)
    run_scalar_consolidation(
        graph_adapter=_FakeScalarGraph(),
        scalar_targets=["ns-x"],
        namespaces=None,
        counting_llm=SimpleNamespace(calls=0),
        build_episodes=lambda rows: [],
        k=3,
        threshold=1.0,
        call_budget=None,
        embed=None,
        config=ScalarConsolidationConfig(
            deterministic_router_enabled=True,
            deterministic_router_promoted_classes=("c_count", "c_money"),
        ),
    )
    assert captured["deterministic_router_enabled"] is True
    assert captured["deterministic_router_promoted_classes"] == ("c_count", "c_money")


def test_phase3_api_forwards_router_flag(monkeypatch):
    import menhir.services.scheduler_tasks as scheduler_tasks

    captured: dict[str, object] = {}

    async def _fake_consolidate(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(scheduler_tasks, "consolidate_personal_memory", _fake_consolidate)
    monkeypatch.setattr("menhir.infrastructure.sync_llm.make_sync_chat", lambda *a, **k: lambda _s, _u: "[]")
    monkeypatch.setattr("menhir.infrastructure.view_embedder.make_view_embedder", lambda settings: None)
    monkeypatch.setattr("menhir.infrastructure.view_embedder.view_embedder_version", lambda settings: "test")
    settings = MemorySettings(
        personal_memory_scalar_deterministic_router=True,
        personal_memory_scalar_deterministic_classes=("c_count", "c_money"),
        personal_memory_scalar_state_enabled=True,
        personal_memory_consolidation_chat_model="",
        personal_memory_consolidation_max_tokens=2048,
    )
    from menhir.api.routes_handlers import phase3_run_impl
    from menhir.api.routes_support import Phase3RunRequest

    adapter = SimpleNamespace(list_dirty_namespaces=lambda limit: ["ns-x"])
    built = SimpleNamespace(settings=settings)

    async def _run():
        return await phase3_run_impl(
            request=SimpleNamespace(),
            body=Phase3RunRequest(namespace="ns-x", k=3),
            require_tier=lambda tier: None,
            require_phase3_adapter=lambda request: (SimpleNamespace(built=built), adapter),
        )

    asyncio.run(_run())
    assert captured["scalar_deterministic_router_enabled"] is True
    assert captured["scalar_deterministic_router_promoted_classes"] == ("c_count", "c_money")
