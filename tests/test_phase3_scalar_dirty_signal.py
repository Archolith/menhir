"""CF-273: the phase3 dirty signals read the live scalar cursor, not the retired watermark.

The fake adapter models the scalar cursor contract (dirty when never consolidated, consolidated by
another perceiver version, or evidence newer than the cursor). The real Cypher is covered by the
online `test_turn_evidence_discovery_live.py` / `test_scalar_turnevidence_discovery_live.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router

NS = "ns-dirty"


class _ScalarCursorAdapter:
    def __init__(self) -> None:
        self.newest = 5
        self.cursor: int | None = None
        self.cursor_version: str | None = None
        self.seen_versions: list[str] = []

    def list_scalar_dirty_namespaces(self, *, perceiver_version: str, limit: int = 200):
        self.seen_versions.append(perceiver_version)
        if (self.cursor is None or self.cursor_version != perceiver_version
                or self.newest > self.cursor):
            return [NS]
        return []

    def count_turn_evidence(self, ns: str) -> int:
        return self.newest

    def advance(self, version: str) -> None:
        self.cursor, self.cursor_version = self.newest, version


def _client(adapter, *, scalar_on: bool = True, version: str = "v2") -> TestClient:
    settings = SimpleNamespace(
        personal_memory_consolidation_chat_model="m",
        personal_memory_scalar_state_enabled=scalar_on,
        personal_memory_scalar_state_perceiver_version=version,
    )
    ctx = SimpleNamespace(
        built=SimpleNamespace(graph_adapter=adapter, settings=settings),
        session=SimpleNamespace(session_id="s", user_id="u"),
        capabilities=SimpleNamespace(
            startup_mode="full", neo4j_ready=True, reads_ready=True, queue_writes_ready=True,
            enrichment_ready=True, scheduler_ready=True, llm_ready=True, embedder_ready=True,
            graphiti_ready=True, failures=[]),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = ctx
    return TestClient(app)


def _dirty(client: TestClient) -> bool:
    resp = client.get("/api/phase3/status", params={"namespace": NS})
    assert resp.status_code == 200, resp.text
    return resp.json()["dirty"]


@pytest.fixture(autouse=True)
def _backend():
    with patch.object(api_routes, "_get_backend", return_value=SimpleNamespace()):
        yield


def test_status_false_when_cursor_caught_up_at_the_same_version() -> None:
    adapter = _ScalarCursorAdapter()
    adapter.advance("v2")
    assert _dirty(_client(adapter)) is False
    assert adapter.seen_versions == ["v2"]


def test_status_true_when_evidence_is_past_the_cursor() -> None:
    adapter = _ScalarCursorAdapter()
    adapter.advance("v2")
    adapter.newest += 1
    assert _dirty(_client(adapter)) is True


def test_status_true_for_a_different_perceiver_version() -> None:
    adapter = _ScalarCursorAdapter()
    adapter.advance("v1")
    assert _dirty(_client(adapter, version="v2")) is True


def test_status_false_when_the_scalar_lane_is_off() -> None:
    adapter = _ScalarCursorAdapter()  # never consolidated: would be dirty if the lane were on
    assert _dirty(_client(adapter, scalar_on=False)) is False
    assert adapter.seen_versions == []


def test_run_dirty_after_flips_false_when_the_run_advances_the_cursor() -> None:
    adapter = _ScalarCursorAdapter()

    async def _consolidate(*args, **kwargs):
        adapter.advance(kwargs["scalar_state_perceiver_version"])
        return {}

    with patch("menhir.infrastructure.sync_llm.make_sync_chat", return_value=lambda s, u: "x"), \
            patch("menhir.infrastructure.view_embedder.make_view_embedder", return_value=None), \
            patch("menhir.services.scheduler_tasks.consolidate_personal_memory", new=_consolidate):
        data = _client(adapter).post("/api/phase3/run", json={"namespace": NS}).json()
    assert data["phase3_selected"] is True
    assert data["dirty_after"] is False
