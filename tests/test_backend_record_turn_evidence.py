"""#717: record_turn_evidence on MemoryBackend / RuntimeProvider / BackendClient.

The bench (and any BackendClient caller) records a turn and then queues its episode with
turn_evidence_uuid. These tests pin the three things that make that safe: the HTTP client sends
the same operation the server dispatches, the in-process provider makes the same adapter call as
POST /api/turn-evidence, and the internal dispatch keeps the agent tier and the namespace pin.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from menhir.api import routes_handlers
from menhir.api.routes_handlers import backend_invoke_impl
from menhir.api.routes_support import _BACKEND_METHODS, _required_tier_for_operation
from menhir.config import MemorySettings
from menhir.core.backend_impl import BackendClient, RuntimeProvider
from menhir.core.backend_protocol import MemoryBackend
from menhir.core.backend_scrub import wrap_backend

pytestmark = pytest.mark.unit

RESULT = {"turn_id": "te-1", "created": True, "recorded_at": "2026-11-20T00:00:00+00:00",
          "occurred_at": None}


def _client(handler) -> BackendClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(base_url="http://menhir.test", transport=transport)
    return BackendClient("http://menhir.test", client=http, settings=MemorySettings())


def test_backend_client_posts_the_internal_operation_with_every_field() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=RESULT)

    out = asyncio.run(_client(handler).record_turn_evidence(
        "I moved to Lisbon", role="user", namespace="ns-a", turn_key="k1",
        occurred_at="2024-01-02T03:04:05Z", source_kind="bench",
    ))
    assert out == RESULT
    assert seen["path"] == "/api/internal/backend/record_turn_evidence"
    body = seen["body"]
    assert body["text"] == "I moved to Lisbon" and body["namespace"] == "ns-a"
    assert body["turn_key"] == "k1" and body["source_kind"] == "bench"
    expected = set(inspect.signature(MemoryBackend.record_turn_evidence).parameters) - {"self"}
    assert set(body) == expected


def test_backend_client_turns_a_400_back_into_value_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "record_turn_evidence requires non-empty text"})

    with pytest.raises(ValueError, match="non-empty text"):
        asyncio.run(_client(handler).record_turn_evidence("  "))


class _Adapter:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exc = exc

    def record_turn_evidence(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return dict(RESULT)


def _provider(adapter: _Adapter) -> RuntimeProvider:
    return RuntimeProvider(SimpleNamespace(graph_adapter=adapter), process_session=None)


def test_runtime_provider_makes_the_rest_route_adapter_call() -> None:
    adapter = _Adapter()
    out = asyncio.run(_provider(adapter).record_turn_evidence(
        "  hello  ", role="assistant", namespace="ns-a", prompt_id="p1",
    ))
    assert out == RESULT
    (call,) = adapter.calls
    assert call["text"] == "hello" and call["role"] == "assistant"
    assert call["namespace"] == "ns-a" and call["prompt_id"] == "p1"
    assert call["source_kind"] == "unknown"
    # Same keyword set the REST route passes (routes.py record_turn_evidence).
    expected = set(inspect.signature(MemoryBackend.record_turn_evidence).parameters) - {"self"}
    assert set(call) == expected


def test_runtime_provider_lets_the_repository_value_error_through() -> None:
    adapter = _Adapter(ValueError("role must be one of ..."))
    with pytest.raises(ValueError):
        asyncio.run(_provider(adapter).record_turn_evidence("x", role="robot"))


def test_dispatch_tier_is_agent() -> None:
    assert "record_turn_evidence" in _BACKEND_METHODS
    assert _required_tier_for_operation("record_turn_evidence") == "agent"


def _invoke(backend: Any, body: dict[str, Any], *, tiers: list[str]):
    return asyncio.run(backend_invoke_impl(
        object(), "record_turn_evidence", body,
        backend_methods=_BACKEND_METHODS,
        required_tier_for_operation=_required_tier_for_operation,
        require_tier=tiers.append,
        try_record_destructive_op_rest=lambda _op: None,
        resolve_caller_session=lambda _req: SimpleNamespace(session_id="s"),
        get_backend=lambda _req: backend,
        drain_background_errors=lambda _sid: [],
        logger=logging.getLogger("menhir.test.turn_evidence"),
    ))


@pytest.fixture
def pinned(monkeypatch):
    def _bind(pin: str) -> None:
        monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: pin)
        monkeypatch.setattr(routes_handlers, "get_request_tool_allowlist", lambda: None)
        monkeypatch.setattr(routes_handlers, "get_client_tool_allowlist", lambda: frozenset())

    return _bind


def test_pinned_client_cannot_write_evidence_into_another_namespace(pinned) -> None:
    """Through the real ScrubbingBackend(RuntimeProvider): the pin overrides the body namespace."""
    pinned("silo-a")
    adapter = _Adapter()
    tiers: list[str] = []
    out = _invoke(wrap_backend(_provider(adapter)), {"text": "t", "namespace": "silo-b"}, tiers=tiers)
    assert tiers == ["agent"]
    assert out["turn_id"] == "te-1"
    assert [c["namespace"] for c in adapter.calls] == ["silo-a"]


def test_pinned_client_omitting_namespace_still_gets_its_silo(pinned) -> None:
    pinned("silo-a")
    adapter = _Adapter()
    _invoke(wrap_backend(_provider(adapter)), {"text": "t"}, tiers=[])
    assert [c["namespace"] for c in adapter.calls] == ["silo-a"]


def test_repository_refusal_maps_to_400(pinned) -> None:
    pinned("")
    adapter = _Adapter(ValueError("record_turn_evidence requires non-empty text"))
    with pytest.raises(HTTPException) as err:
        _invoke(wrap_backend(_provider(adapter)), {"text": ""}, tiers=[])
    assert err.value.status_code == 400
