"""Wiring of the structured turn role (Archolith/menhir#271) through production paths.

The pure resolution/normalisation rules live in tests/test_turn_role_*.py. These tests pin the
seams between them: enrichment -> add_episode_with_timeout -> extraction receipt, and
REST -> backend -> bridge -> ingest service. Only graph/model I/O is replaced.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.core.backend_client_ops import BackendClientOpsMixin
from menhir.services import enrichment_steps as steps
from menhir.services.ingest_intake import IngestIntakeMixin
from tests.test_api_routes import fake_backend, fake_runtime_ctx  # noqa: F401  (fixtures)
from tests.test_backend_roundtrip import (  # noqa: F401  (fixtures)
    backend_client_like_uvicorn,
    server_app,
)
from tests.test_evidence_publication_intents import _pipeline_context

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. run_graphiti_extraction resolves the role and hands it to add_episode_with_timeout
# ---------------------------------------------------------------------------

_TEXT = "I own 37 coins"


async def _role_passed_to_dispatch(monkeypatch: pytest.MonkeyPatch, claimed: dict[str, Any]) -> Any:
    seen: list[dict[str, Any]] = []

    async def capture(*_args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(episode=SimpleNamespace(uuid="remote-1"))

    monkeypatch.setattr(steps, "add_episode_with_timeout", capture)
    monkeypatch.setattr(steps, "record_lifecycle_event", lambda **_kwargs: None)
    base = _pipeline_context([])
    ctx = replace(base, claimed={**base.claimed, **claimed})
    await steps.run_graphiti_extraction(ctx, finalize_under_gate=False)
    assert len(seen) == 1
    # Fails if the call site stops passing turn_role at all (KeyError) or passes a constant.
    return seen[0]["turn_role"]


@pytest.mark.asyncio
async def test_extraction_passes_proven_evidence_role(monkeypatch) -> None:
    role = await _role_passed_to_dispatch(monkeypatch, {
        "content": _TEXT, "turn_evidence_count": 1, "turn_evidence_text": _TEXT,
        "turn_evidence_role": "user", "turn_evidence_namespace": "tenant-a",
    })
    assert role == "user"


@pytest.mark.asyncio
async def test_extraction_ignores_evidence_from_another_namespace(monkeypatch) -> None:
    # Review F6: the pipeline context claims namespace tenant-a.
    role = await _role_passed_to_dispatch(monkeypatch, {
        "content": _TEXT, "turn_evidence_count": 1, "turn_evidence_text": _TEXT,
        "turn_evidence_role": "user", "turn_evidence_namespace": "tenant-b",
    })
    assert role == "unknown"


@pytest.mark.asyncio
async def test_extraction_passes_declared_role_when_no_evidence(monkeypatch) -> None:
    role = await _role_passed_to_dispatch(monkeypatch, {"turn_role": "assistant"})
    assert role == "assistant"


@pytest.mark.asyncio
async def test_extraction_passes_unknown_when_nothing_is_recorded(monkeypatch) -> None:
    assert await _role_passed_to_dispatch(monkeypatch, {}) == "unknown"


# ---------------------------------------------------------------------------
# 2. add_episode_with_timeout puts the role on the receipt the extraction child task reads
# ---------------------------------------------------------------------------

def _add_episode_kwargs() -> dict[str, Any]:
    from datetime import datetime, timezone

    return dict(
        name="ep", episode_body="user: hello", source_description="claude-code",
        reference_time=datetime.now(timezone.utc), episode_uuid="ep-turn-role",
    )


async def _receipt_role_seen_in_child(**extra: Any) -> str:
    pytest.importorskip("graphiti_core")
    from menhir.infrastructure.graphiti_extraction_policy import (
        clear_extraction_receipt,
        get_extraction_receipt,
    )

    clear_extraction_receipt()
    observed: list[str] = []

    class FakeClient:
        async def add_episode(self, **kwargs: Any) -> str:
            receipt = get_extraction_receipt()
            assert receipt is not None
            observed.append(receipt.turn_role)
            return "result"

    try:
        await steps.add_episode_with_timeout(
            FakeClient(), timeout_s=5.0, **_add_episode_kwargs(), **extra
        )
    finally:
        clear_extraction_receipt()
    assert len(observed) == 1
    return observed[0]


@pytest.mark.asyncio
async def test_receipt_carries_passed_turn_role_into_child_task() -> None:
    assert await _receipt_role_seen_in_child(turn_role="user") == "user"
    assert await _receipt_role_seen_in_child(turn_role="assistant") == "assistant"


@pytest.mark.asyncio
async def test_receipt_turn_role_defaults_to_unknown() -> None:
    assert await _receipt_role_seen_in_child() == "unknown"


# ---------------------------------------------------------------------------
# 3. REST POST /api/memory forwards (or rejects) role
# ---------------------------------------------------------------------------

@pytest.fixture
def client(fake_backend, fake_runtime_ctx):  # noqa: F811
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = fake_runtime_ctx
    with patch.object(api_routes, "_get_backend", return_value=fake_backend):
        yield TestClient(app)


def test_rest_forwards_role_to_backend(client, fake_backend) -> None:  # noqa: F811
    resp = client.post("/api/memory", json={"episode": "I said hi", "role": "assistant"})
    assert resp.status_code == 200
    assert fake_backend.queue_episode.await_args.kwargs["role"] == "assistant"


def test_rest_omits_role_when_not_supplied(client, fake_backend) -> None:  # noqa: F811
    resp = client.post("/api/memory", json={"episode": "no role here"})
    assert resp.status_code == 200
    assert "role" not in fake_backend.queue_episode.await_args.kwargs


@pytest.mark.parametrize("bad_role", ["User", "human"])
def test_rest_rejects_unknown_or_miscased_role(client, fake_backend, bad_role) -> None:  # noqa: F811
    resp = client.post("/api/memory", json={"episode": "x", "role": bad_role})
    assert resp.status_code == 422
    fake_backend.queue_episode.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Bridge (BackendClient) sends role on the wire only when set
# ---------------------------------------------------------------------------

class _CapturingOps(BackendClientOpsMixin):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def _request(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, params))
        return {}


@pytest.mark.asyncio
async def test_bridge_sends_role_param() -> None:
    ops = _CapturingOps()
    await ops.queue_episode("t", user_id="u", session_id="s", role="assistant")
    operation, params = ops.calls[0]
    assert operation == "queue_episode"
    assert params["role"] == "assistant"


@pytest.mark.asyncio
async def test_bridge_omits_role_param_when_none() -> None:
    ops = _CapturingOps()
    await ops.queue_episode("t", user_id="u", session_id="s")
    assert "role" not in ops.calls[0][1]


# ---------------------------------------------------------------------------
# 5. Real /api/internal/backend dispatch over ASGI (500-style transport, as in #132)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_round_trip_delivers_role_to_ingest_service(backend_client_like_uvicorn) -> None:  # noqa: F811
    bc, ctx = backend_client_like_uvicorn
    ctx.built.ingest_service.queue_episode_for_enrichment = AsyncMock(return_value={"episode_id": "q"})
    await bc.queue_episode("t", user_id="u", session_id="s", role="assistant")
    assert ctx.built.ingest_service.queue_episode_for_enrichment.await_args.kwargs["role"] == "assistant"
    await bc.queue_episode("t", user_id="u", session_id="s")
    assert "role" not in ctx.built.ingest_service.queue_episode_for_enrichment.await_args.kwargs


@pytest.mark.asyncio
async def test_dispatch_maps_backend_role_value_error_to_400(backend_client_like_uvicorn) -> None:  # noqa: F811
    bc, ctx = backend_client_like_uvicorn
    ctx.built.ingest_service.queue_episode_for_enrichment = AsyncMock(
        side_effect=ValueError("invalid role: 'User'")
    )
    # Unmapped, the ValueError would be a 500 (HTTPStatusError); mapped, the client re-raises it.
    with pytest.raises(ValueError, match="invalid role: 'User'"):
        await bc.queue_episode("t", user_id="u", session_id="s", role="User")


@pytest.mark.asyncio
async def test_real_intake_rejects_miscased_role_over_http(backend_client_like_uvicorn) -> None:  # noqa: F811
    bc, ctx = backend_client_like_uvicorn
    ctx.built.ingest_service = IngestIntakeMixin()
    with pytest.raises(ValueError, match="invalid role"):
        await bc.queue_episode("t", user_id="u", session_id="s", role="User")
