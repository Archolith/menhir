"""CF-165 regression tests for authoritative MCP telemetry lineage.

Exercises the real AddMemoryTool -> BaseTool.execute -> track_mcp_call seam. The telemetry store is
a tiny capture fake so the test can distinguish the payload used for ownership from the payload
used for the privacy-minimized request preview.
"""

from __future__ import annotations

import importlib

import pytest

from menhir.domain.namespace import DEFAULT_NAMESPACE
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool

pytestmark = [pytest.mark.unit]


class _CaptureStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, **kwargs) -> None:
        self.rows.append(dict(kwargs))


class _Backend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def queue_episode(self, text: str, **kwargs):
        self.calls.append({"text": text, **kwargs})
        return {
            "status": "pending",
            "episode_id": "ep-1",
            "nodes_touched": 0,
            "edges_touched": 0,
        }


def _install_capture(monkeypatch, *, store: _CaptureStore) -> None:
    import menhir.mcp.contracts as contracts
    import menhir.mcp.telemetry.tracker as tracker

    real_track = tracker.track_mcp_call

    async def _track(**kwargs):
        return await real_track(**kwargs, store=store)

    monkeypatch.setattr(contracts, "track_mcp_call", _track)
    monkeypatch.setattr(contracts, "request_uses_query_auth", lambda: False)
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())


@pytest.mark.asyncio
async def test_add_memory_telemetry_uses_server_pinned_namespace_after_policy(monkeypatch) -> None:
    import menhir.mcp.contracts as contracts
    import menhir.mcp.service_access as service_access

    add_memory_mod = importlib.import_module("menhir.mcp.tools.ingest.add_memory")
    store = _CaptureStore()
    backend = _Backend()
    _install_capture(monkeypatch, store=store)

    monkeypatch.setattr(contracts, "get_request_tier", lambda: "agent")
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: "server-tenant")
    monkeypatch.setattr(service_access, "get_pinned_namespace", lambda: "server-tenant")
    monkeypatch.setattr(AddMemoryTool, "get_backend", lambda self: backend)
    monkeypatch.setattr(
        add_memory_mod,
        "get_mcp_session",
        lambda: type("Session", (), {"user_id": "u", "session_id": "s"})(),
    )

    async def _queue_summary(_backend) -> str:
        return "queue_depth=0"

    monkeypatch.setattr(add_memory_mod, "_queue_summary", _queue_summary)

    result = await AddMemoryTool().execute(
        text="private memory body",
        namespace="caller-selected-tenant",
    )

    assert result.startswith("Queued.")
    assert backend.calls[0]["namespace"] == "server-tenant"
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["namespace"] == "server-tenant"
    assert "private memory body" not in (row["payload_preview"] or "")
    assert "[redacted]" in (row["payload_preview"] or "")


@pytest.mark.asyncio
async def test_denied_add_memory_does_not_treat_raw_namespace_as_ownership(monkeypatch) -> None:
    import menhir.mcp.contracts as contracts
    import menhir.mcp.service_access as service_access

    store = _CaptureStore()
    backend = _Backend()
    _install_capture(monkeypatch, store=store)

    # Denied by tier before _apply_pinned_namespace and before the endpoint is entered.
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    monkeypatch.setattr(contracts, "get_pinned_namespace", lambda: None)
    monkeypatch.setattr(service_access, "get_pinned_namespace", lambda: None)
    monkeypatch.setattr(AddMemoryTool, "get_backend", lambda self: backend)

    result = await AddMemoryTool().execute(
        text="private denied body",
        namespace="caller-forged-owner",
    )

    assert result.startswith("Error: PermissionError:")
    assert backend.calls == []
    assert len(store.rows) == 1
    row = store.rows[0]
    assert row["namespace"] == DEFAULT_NAMESPACE
    assert row["node_uuid"] is None
    assert "private denied body" not in (row["payload_preview"] or "")
    assert "[redacted]" in (row["payload_preview"] or "")
