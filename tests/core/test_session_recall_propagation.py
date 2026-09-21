from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.core.backend_client import BackendClient
from menhir.core.backend_runtime import RuntimeProvider
from menhir.core.request_context import bind_request_session, reset_request_session
from menhir.domain.session import new_session
from menhir.mcp import service_access


class _RecallRecorder:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    async def recall(self, _query: str, **kwargs: object) -> dict[str, object]:
        self.kwargs = kwargs
        return {
            "query": "query",
            "preset": "knowledge",
            "results": [],
            "candidates_evaluated": 0,
        }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_recall_forwards_effective_caller_session() -> None:
    recorder = _RecallRecorder()
    built = SimpleNamespace(
        recall_service=recorder,
        settings=SimpleNamespace(retrieval_tuning=lambda: None, frontier_shadow=False),
    )
    provider = RuntimeProvider(
        built,
        process_session=SimpleNamespace(session_id="process-session"),
        caller_session=SimpleNamespace(session_id="caller-session"),
    )

    await provider.recall("query", include_session=True)

    assert recorder.kwargs["session_id"] == "caller-session"


@pytest.mark.unit
def test_backend_client_relays_request_session_headers() -> None:
    settings = SimpleNamespace(
        agent_key="",
        api_key="",
        mcp_client_user_id="",
        mcp_client_id="",
        mcp_client_name="",
    )
    client = BackendClient("http://localhost:8099", settings=settings)
    token = bind_request_session(
        new_session("request-user", session_id="request-session")
    )
    try:
        headers = client._default_headers()
    finally:
        reset_request_session(token)

    assert headers["x-menhir-session-id"] == "request-session"
    assert headers["x-menhir-user-id"] == "request-user"


@pytest.mark.unit
def test_backend_client_mode_can_resume_an_explicit_logical_session(monkeypatch) -> None:
    settings = SimpleNamespace(
        backend_url="http://localhost:8099",
        mcp_client_user_id="client-user",
        mcp_session_id="stable-session",
        mcp_client_id="client-device",
        mcp_client_name="test-client",
    )
    monkeypatch.setattr(service_access, "_client_session", None)
    monkeypatch.setattr(
        service_access,
        "backend_client_mode_enabled",
        lambda _settings: True,
    )

    first = service_access.get_mcp_session(settings)
    monkeypatch.setattr(service_access, "_client_session", None)
    restarted = service_access.get_mcp_session(settings)

    assert first.session_id == "stable-session"
    assert restarted.session_id == "stable-session"
