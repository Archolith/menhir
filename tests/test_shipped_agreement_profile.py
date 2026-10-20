"""Frozen shipped agreement profile (#170 item 7).

One snapshot of every value that decides when a personal-memory claim commits, and proof that the
two production entry points (the runtime scheduler and POST /api/phase3/run) pass the settings
values through instead of their older `getattr` fallbacks (threshold 1.0, reconciliation off).
Changing any value here is a policy change: update docs/agreement-settings.md with it, and bump the
lane's perceiver version when the change can alter slot identity.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.config.settings_model import MemorySettings
from menhir.core import runtime
from menhir.domain.count_decide import DECIDE_K, DECIDE_THRESHOLD
from menhir.domain.typed_assertion import perceiver_rank
from menhir.infrastructure.sync_llm import make_sync_chat
from menhir.services import perception, scheduler_tasks
from menhir.services.counting_consolidation import effective_counting_perceiver_version
from menhir.services.maintenance_scheduler import MaintenanceScheduler

#: MemorySettings defaults that decide agreement, versions and cost.
SHIPPED_SETTINGS = {
    "personal_memory_consolidation_k": 3,
    "personal_memory_consolidation_call_budget": 300,
    "personal_memory_consolidation_interval_s": 300.0,
    "personal_memory_consolidation_max_tokens": 8192,
    "personal_memory_consolidation_disable_reasoning": False,
    "personal_memory_consolidation_chat_model": "",
    "personal_memory_consolidation_verify_retries": 0,
    "personal_memory_consolidation_sum_grounding": True,
    "personal_memory_scalar_threshold": 2 / 3,
    "personal_memory_scalar_reconcile_attribute": True,
    "personal_memory_scalar_reconcile_scope": True,
    "personal_memory_scalar_reconcile_subject": True,
    "personal_memory_scalar_canonical_self": False,
    "personal_memory_scalar_verdict_min_similarity": 0.0,
    "personal_memory_scalar_state_perceiver_version": "v2",
    "personal_memory_event_history_perceiver_version": "v2",
    "personal_memory_counting_perceiver_version": "v1",
    "personal_memory_counting_extract_v2": False,
    "personal_memory_counting_match_enabled": False,
    "personal_memory_counting_lease_seconds": 900,
    # Defaults are not activation: every lane and authority path ships off.
    "personal_memory_consolidation_enabled": False,
    "personal_memory_scalar_state_enabled": False,
    "personal_memory_scalar_view_authority_enabled": False,
    "personal_memory_scalar_history_enabled": False,
    "personal_memory_scalar_deterministic_router": False,
    "personal_memory_event_history_enabled": False,
    "personal_memory_event_history_authority_enabled": False,
    "personal_memory_counting_enabled": False,
}

#: What the runtime must hand the scheduler for those settings.
SCHEDULER_KWARGS = {
    "personal_memory_k": 3,
    "personal_memory_call_budget": 300,
    "personal_memory_interval_s": 300.0,
    "personal_memory_verify_retries": 0,
    "personal_memory_sum_grounding": True,
    "scalar_threshold": 2 / 3,
    "scalar_reconcile_attribute": True,
    "scalar_reconcile_scope": True,
    "scalar_reconcile_subject": True,
    "scalar_canonical_self": False,
    "scalar_state_perceiver_version": "v2",
    "event_history_perceiver_version": "v2",
    "counting_perceiver_version": "v1",
    "counting_extract_v2": False,
    "counting_match": False,
    "counting_lease_seconds": 900,
}

#: What a consolidation pass must receive, from either entry point.
CONSOLIDATE_KWARGS = {
    "k": 3,
    "verify_retries": 0,
    "sum_grounding": True,
    "scalar_threshold": 2 / 3,
    "scalar_reconcile_attribute": True,
    "scalar_reconcile_scope": True,
    "scalar_reconcile_subject": True,
    "scalar_canonical_self": False,
    "scalar_state_perceiver_version": "v2",
    "counting_perceiver_version": "v1",
    "counting_extract_v2": False,
    "counting_match": False,
    "counting_lease_seconds": 900,
    "event_history_perceiver_version": "v2",
}


def _scalar_on() -> MemorySettings:
    # Scalar on so the runtime builds the consolidation LLM; everything else stays shipped.
    return dataclasses.replace(
        MemorySettings(),
        personal_memory_scalar_state_enabled=True,
        artifact_reconcile_mode="off",
        structure_watcher_enabled=False,
    )


@pytest.mark.unit
def test_memory_settings_defaults_match_the_frozen_profile() -> None:
    settings = MemorySettings()
    actual = {name: getattr(settings, name) for name in SHIPPED_SETTINGS}
    assert actual == SHIPPED_SETTINGS


@pytest.mark.unit
def test_from_env_with_no_personal_memory_env_matches_the_frozen_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in list(os.environ):
        if var.startswith("MENHIR_PERSONAL_MEMORY_"):
            monkeypatch.delenv(var)
    settings = MemorySettings.from_env()
    actual = {name: getattr(settings, name) for name in SHIPPED_SETTINGS}
    assert actual == SHIPPED_SETTINGS


@pytest.mark.unit
def test_hard_coded_agreement_constants() -> None:
    # Counting match-decide vote: not operator-settable.
    assert DECIDE_K == 3
    assert DECIDE_THRESHOLD == 2 / 3
    # Sampling temperature for every consolidation call: not operator-settable.
    assert inspect.signature(make_sync_chat).parameters["temperature"].default == 0.7
    # Counter lane: unanimous, and no caller passes `threshold`.
    params = inspect.signature(scheduler_tasks.consolidate_personal_memory).parameters
    assert params["threshold"].default == 1.0
    assert params["scalar_threshold"].default is None
    perceive = inspect.signature(perception.perceive_and_fold).parameters
    assert perceive["coref_k"].default == 3
    assert perceive["verify_k"].default == 3


@pytest.mark.unit
def test_shipped_perceiver_versions_rank_so_a_bump_can_supersede() -> None:
    # Scalar and event supersession is strict-rank; a version that ranks 0 can never replace a
    # stored CURRENT assertion, so a bump to it would be silently ignored.
    settings = MemorySettings()
    for version in (
        settings.personal_memory_scalar_state_perceiver_version,
        settings.personal_memory_event_history_perceiver_version,
    ):
        assert perceiver_rank(version) > perceiver_rank("v1.999999") > 0
    assert perceiver_rank("v3") > perceiver_rank("v2.1") > perceiver_rank("v2")
    # Counting compares the version string for equality, so suffixes are fine there.
    assert effective_counting_perceiver_version("v1", False) == "v1"
    assert effective_counting_perceiver_version("v1", True) == "v1+x2"


@pytest.mark.unit
def test_runtime_scheduler_receives_the_frozen_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    chat_kwargs: dict[str, object] = {}

    class _Scheduler:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def start(self) -> None:
            return None

        def status_snapshot(self) -> dict[str, object]:
            return {"running": False, "lease": {}}

    def _chat(_settings: object, **kwargs: object):
        chat_kwargs.update(kwargs)
        return lambda system, user: "[]"

    monkeypatch.setattr(runtime, "MaintenanceScheduler", _Scheduler)
    monkeypatch.setattr(runtime._state, "scheduler", None)
    monkeypatch.setattr("menhir.infrastructure.sync_llm.make_sync_chat", _chat)
    built = SimpleNamespace(
        settings=_scalar_on(),
        ingest_service=SimpleNamespace(),
        graph_adapter=SimpleNamespace(neo4j=None),
    )

    asyncio.run(runtime._start_scheduler(built))

    assert {name: captured[name] for name in SCHEDULER_KWARGS} == SCHEDULER_KWARGS
    assert chat_kwargs == {"model": None, "max_tokens": 8192, "disable_reasoning": False}


@pytest.mark.unit
def test_scheduler_pass_forwards_the_frozen_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _consolidate(*_args: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(
        "menhir.services.maintenance_scheduler.consolidate_personal_memory", _consolidate)
    scheduler = MaintenanceScheduler(
        ingest_service=SimpleNamespace(get_queue_depth=lambda: 0),
        graph_adapter=object(),
        scalar_state_enabled=True,
        personal_memory_llm=lambda system, user: "[]",
        **SCHEDULER_KWARGS,
    )

    asyncio.run(scheduler._make_consolidate_personal_memory())

    assert "threshold" not in captured  # the counter lane keeps its unanimous default
    assert captured["call_budget"] == 300
    expected = {k: v for k, v in CONSOLIDATE_KWARGS.items()
                if k != "event_history_perceiver_version"}
    assert {name: captured[name] for name in expected} == expected


@pytest.mark.unit
def test_phase3_run_forwards_the_frozen_profile() -> None:
    consolidate = AsyncMock(return_value={})
    adapter = SimpleNamespace(list_dirty_namespaces=lambda *, limit=500: [])
    runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter, settings=_scalar_on()),
        session=SimpleNamespace(session_id="s", user_id="u"),
        capabilities=SimpleNamespace(
            startup_mode="full", neo4j_ready=True, reads_ready=True, queue_writes_ready=True,
            enrichment_ready=True, scheduler_ready=True, llm_ready=True, embedder_ready=True,
            graphiti_ready=True, failures=[]),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = runtime_ctx
    with patch.object(api_routes, "_get_backend", return_value=SimpleNamespace()), patch(
        "menhir.infrastructure.sync_llm.make_sync_chat", return_value=lambda s, u: "[]",
    ), patch(
        "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None,
    ), patch(
        "menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate,
    ):
        resp = TestClient(app).post("/api/phase3/run", json={"namespace": "ns-freeze"})

    assert resp.status_code == 200, resp.text
    kwargs = consolidate.await_args.kwargs
    assert "threshold" not in kwargs
    assert {name: kwargs[name] for name in CONSOLIDATE_KWARGS} == CONSOLIDATE_KWARGS
