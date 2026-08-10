"""Wiring tests for the personal-memory event-history runtime seam.

Covers the maintenance scheduler fields/registration/forwarding and the core runtime
threading of the event-history settings into the scheduler. Uses generic synthetic
assertions; no benchmark ids, answers, or fixture-specific wording.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from menhir.config.settings_model import MemorySettings
from menhir.services.maintenance_scheduler import MaintenanceScheduler


class _Ingest:
    def get_queue_depth(self):
        return 0


def _llm(system: str, user: str) -> str:
    return "[]"


def _common():
    return dict(ingest_service=_Ingest(), graph_adapter=object())


def _capture_consolidate(monkeypatch) -> dict:
    captured: dict[str, object] = {}

    async def _fake_consolidate(*_args, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("menhir.services.maintenance_scheduler.consolidate_personal_memory", _fake_consolidate)
    return captured


# --------------------------------------------------------------------------- scheduler registration


def test_default_scheduler_registers_no_personal_memory_job() -> None:
    scheduler = MaintenanceScheduler(**_common())
    assert "consolidate_personal_memory" not in scheduler._jobs
    assert scheduler.event_history_enabled is False
    assert scheduler.event_history_perceiver_version == "v1"
    assert scheduler.event_history_batch_size == 500


def test_neither_path_registers_when_llm_is_none() -> None:
    scheduler = MaintenanceScheduler(
        **_common(),
        personal_memory_enabled=True,
        event_history_enabled=True,
        personal_memory_llm=None,
    )
    assert "consolidate_personal_memory" not in scheduler._jobs


def test_counter_only_registers_and_forwards_counter_on_event_off(monkeypatch) -> None:
    captured = _capture_consolidate(monkeypatch)
    scheduler = MaintenanceScheduler(
        **_common(),
        personal_memory_enabled=True,
        personal_memory_llm=_llm,
    )
    assert "consolidate_personal_memory" in scheduler._jobs
    asyncio.run(scheduler._make_consolidate_personal_memory())
    assert captured["enable_counter_state"] is True
    assert captured["enable_event_history"] is False


def test_event_only_registers_and_forwards_counter_off_event_on(monkeypatch) -> None:
    captured = _capture_consolidate(monkeypatch)
    scheduler = MaintenanceScheduler(
        **_common(),
        personal_memory_enabled=False,
        event_history_enabled=True,
        event_history_perceiver_version="v2",
        event_history_batch_size=777,
        personal_memory_llm=_llm,
    )
    assert "consolidate_personal_memory" in scheduler._jobs
    asyncio.run(scheduler._make_consolidate_personal_memory())
    assert captured["enable_counter_state"] is False
    assert captured["enable_event_history"] is True
    assert captured["event_history_perceiver_version"] == "v2"
    assert captured["event_batch_size"] == 777


# --------------------------------------------------------------------------- core runtime wiring


def test_runtime_threads_event_settings_when_event_on_even_if_counter_off(monkeypatch) -> None:
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
    monkeypatch.setattr(
        "menhir.infrastructure.sync_llm.make_sync_chat",
        lambda *a, **k: _llm,
    )
    settings = MemorySettings(
        personal_memory_consolidation_enabled=False,
        personal_memory_event_history_enabled=True,
        personal_memory_event_history_perceiver_version="v2",
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
    assert captured["personal_memory_enabled"] is False
    assert captured["event_history_enabled"] is True
    assert captured["event_history_perceiver_version"] == "v2"
    assert captured["personal_memory_llm"] is not None


def test_runtime_off_path_does_not_create_sync_chat_for_these_paths(monkeypatch) -> None:
    import menhir.core.runtime as runtime

    calls: list[dict] = []
    captured: dict[str, object] = {}

    def _fake_make_sync_chat(*args, **kwargs):
        calls.append((args, kwargs))
        return None

    class _FakeScheduler:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def start(self):
            return None

        def status_snapshot(self):
            return {"running": True}

    monkeypatch.setattr("menhir.infrastructure.sync_llm.make_sync_chat", _fake_make_sync_chat)
    monkeypatch.setattr(runtime, "MaintenanceScheduler", _FakeScheduler)
    monkeypatch.setattr(runtime, "_state", SimpleNamespace(scheduler=None))
    monkeypatch.setattr(runtime, "make_view_embedder", lambda settings: None)
    monkeypatch.setattr(runtime, "view_embedder_version", lambda settings: "test")
    settings = MemorySettings(
        personal_memory_consolidation_enabled=False,
        personal_memory_event_history_enabled=False,
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
    assert calls == []
    assert captured["event_history_enabled"] is False
    assert captured["personal_memory_llm"] is None
