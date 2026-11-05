"""POST /api/phase3/run must say so when a lane failed closed for the requested namespace."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.routes import router
from menhir.services.lane_outcomes import failed_lanes


def _client(graph_adapter) -> TestClient:
    settings = SimpleNamespace(
        personal_memory_scalar_state_enabled=True,
        personal_memory_counting_enabled=True,
    )
    runtime_ctx = SimpleNamespace(
        built=SimpleNamespace(settings=settings, graph_adapter=graph_adapter),
        capabilities=None,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = runtime_ctx
    return TestClient(app)


def _adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.list_dirty_namespaces.return_value = ["ns-bench"]
    adapter.load_next_counting_batch.return_value = []
    adapter.claim_counting_namespace.return_value = {"incarnation": "i1", "epoch": 1}
    adapter.run_view_projection_repairs = lambda *a, **k: None
    return adapter


def _post(client: TestClient, consolidate=None):
    patches = [
        patch("menhir.infrastructure.sync_llm.make_sync_chat", return_value=lambda *a: "[]"),
        patch("menhir.infrastructure.view_embedder.make_view_embedder", return_value=None),
    ]
    if consolidate is not None:
        patches.append(
            patch("menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate))
    for p in patches:
        p.start()
    try:
        return client.post("/api/phase3/run", json={"namespace": "ns-bench"})
    finally:
        for p in patches:
            p.stop()


def test_counting_failure_inside_consolidation_is_reported_not_ok():
    adapter = _adapter()
    # The provider 500 surfaces as an exception inside the counting namespace body.
    adapter.mark_counting_flag_state.side_effect = RuntimeError("openai 500")
    stub_service = MagicMock()
    with patch("menhir.services.typed_scalar_perception.TypedScalarPerceptionService",
               return_value=stub_service), \
         patch("menhir.services.scheduler_tasks.select_scalar_targets",
               new=AsyncMock(return_value=[])), \
         patch("menhir.services.scheduler_tasks.run_scalar_consolidation", return_value={}), \
         patch("menhir.services.scheduler_tasks.sweep_counting_flag_state", return_value={}):
        resp = _post(_client(adapter))
    assert resp.status_code == 200
    data = resp.json()
    assert data["counting_namespaces_failed"] == 1
    assert data["ok"] is False
    assert data["failed_lanes"] == [
        {"lane": "counting", "namespaces": ["ns-bench"], "failed": 1}]


def test_scalar_failure_inside_consolidation_is_reported_not_ok():
    consolidate = AsyncMock(return_value={"scalar_namespaces_failed": 2})
    resp = _post(_client(_adapter()), consolidate)
    data = resp.json()
    assert data["ok"] is False
    assert data["failed_lanes"] == [
        {"lane": "scalar", "namespaces": ["ns-bench"], "failed": 2}]


def test_clean_run_is_ok_and_counter_shape_unchanged():
    consolidate = AsyncMock(return_value={
        "namespaces_dirty": 1, "namespaces_processed": 1, "counting_namespaces_failed": 0,
        "counting_namespaces_processed": 1, "scalar_namespaces_failed": 0})
    resp = _post(_client(_adapter()), consolidate)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["failed_lanes"] == []
    assert data["counting_namespaces_failed"] == 0
    assert data["counting_namespaces_processed"] == 1


def test_failed_lanes_ignores_lanes_without_a_failed_counter():
    assert failed_lanes({"namespaces_processed": 1}, ["a"]) == []


def _run_scalar(targets, *, refuse):
    from menhir.services.scalar_consolidation import (
        ScalarConsolidationConfig,
        run_scalar_consolidation,
    )
    from menhir.services.typed_scalar_perception import ScalarStateNotActivatedError

    stub = MagicMock()
    if refuse:
        stub.ensure_activated.side_effect = ScalarStateNotActivatedError("refused")
    with patch("menhir.services.typed_scalar_perception.TypedScalarPerceptionService",
               return_value=stub):
        return run_scalar_consolidation(
            MagicMock(), scalar_targets=targets, namespaces=None,
            counting_llm=MagicMock(calls=0), build_episodes=lambda rows: [], k=1,
            threshold=1.0, call_budget=None, embed=None,
            config=ScalarConsolidationConfig())


def test_scalar_activation_refusal_with_targets_counts_failed():
    out = _run_scalar(["ns-bench"], refuse=True)
    assert out["scalar_namespaces_failed"] == 1
    assert failed_lanes(out, ["ns-bench"]) == [
        {"lane": "scalar", "namespaces": ["ns-bench"], "failed": 1}]


def test_scalar_activation_refusal_without_targets_stays_ok():
    out = _run_scalar([], refuse=True)
    assert out["scalar_namespaces_failed"] == 0
    assert failed_lanes(out, ["ns-bench"]) == []


def test_normal_scalar_run_reports_zero_failed():
    out = _run_scalar([], refuse=False)
    assert out["scalar_namespaces_failed"] == 0


def test_rest_reports_scalar_activation_refusal_not_ok():
    consolidate = AsyncMock(return_value={"scalar_namespaces_failed": 1})
    data = _post(_client(_adapter()), consolidate).json()
    assert data["ok"] is False
    assert [f["lane"] for f in data["failed_lanes"]] == ["scalar"]
