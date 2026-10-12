"""MCP add_memory / add_memory_and_track accept the declared speaker role (review F11 of #272).

Same contract as REST `POST /api/memory` `role`: exact lowercase match, forwarded only when set.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.core.backend_runtime import RuntimeProvider
from menhir.domain.session import new_session
from menhir.domain.turn_role import TURN_ROLES
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
from menhir.mcp.tools.ingest.add_memory_and_track import AddMemoryAndTrackTool

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TOOLS = [AddMemoryTool, AddMemoryAndTrackTool]


def _fake_backend() -> SimpleNamespace:
    return SimpleNamespace(
        queue_episode=AsyncMock(return_value={"status": "failed"}),
        create_temporal=AsyncMock(return_value={"uuid": "t", "target_date": "2027-02-16"}),
    )


@pytest.fixture
def mcp_session(monkeypatch):
    session = SimpleNamespace(user_id="mcp-user", session_id="mcp-session")
    monkeypatch.setattr("menhir.mcp.tools.ingest.add_memory.get_mcp_session", lambda: session)
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.add_memory_and_track.get_mcp_session", lambda: session
    )
    return session


@pytest.mark.parametrize("tool_class", TOOLS)
@pytest.mark.parametrize("role", sorted(TURN_ROLES))
async def test_role_is_forwarded_to_the_backend(monkeypatch, mcp_session, tool_class, role) -> None:
    backend = _fake_backend()
    monkeypatch.setattr(tool_class, "get_backend", lambda self: backend)
    await tool_class().endpoint(text="I said hi", role=role)
    assert backend.queue_episode.await_args.kwargs["role"] == role


@pytest.mark.parametrize("tool_class", TOOLS)
async def test_role_is_omitted_when_not_supplied(monkeypatch, mcp_session, tool_class) -> None:
    backend = _fake_backend()
    monkeypatch.setattr(tool_class, "get_backend", lambda self: backend)
    await tool_class().endpoint(text="no role here")
    assert "role" not in backend.queue_episode.await_args.kwargs


@pytest.mark.parametrize("tool_class", TOOLS)
@pytest.mark.parametrize("bad_role", ["User", " user", "human", "unknown", "", 1, ["user"]])
async def test_invalid_role_is_refused_before_any_write(
    monkeypatch, mcp_session, tool_class, bad_role
) -> None:
    backend = _fake_backend()
    monkeypatch.setattr(tool_class, "get_backend", lambda self: backend)
    result = await tool_class().endpoint(text="x", role=bad_role)
    assert result.startswith("Cannot store memory: invalid role")
    backend.queue_episode.assert_not_awaited()


async def test_temporal_write_ignores_a_valid_role(monkeypatch) -> None:
    backend = _fake_backend()
    monkeypatch.setattr(AddMemoryTool, "get_backend", lambda self: backend)
    await AddMemoryTool().endpoint(
        text="dentist", type="TEMPORAL", valid_at="2027-02-16", role="user"
    )
    backend.create_temporal.assert_awaited_once()
    assert "role" not in backend.create_temporal.await_args.kwargs


async def test_temporal_write_still_refuses_an_invalid_role(monkeypatch) -> None:
    backend = _fake_backend()
    monkeypatch.setattr(AddMemoryTool, "get_backend", lambda self: backend)
    result = await AddMemoryTool().endpoint(
        text="dentist", type="TEMPORAL", valid_at="2027-02-16", role="human"
    )
    assert result.startswith("Cannot store memory: invalid role")
    backend.create_temporal.assert_not_awaited()


@pytest.mark.parametrize("tool_class", TOOLS)
async def test_role_reaches_the_ingest_service_through_the_runtime_backend(
    monkeypatch, mcp_session, tool_class
) -> None:
    ingest_service = SimpleNamespace(
        queue_episode_for_enrichment=AsyncMock(return_value={"status": "failed"})
    )
    backend = RuntimeProvider(
        SimpleNamespace(ingest_service=ingest_service, graph_adapter=SimpleNamespace()),
        new_session("process-user"),
    )
    monkeypatch.setattr(tool_class, "get_backend", lambda self: backend)
    await tool_class().endpoint(text="I said hi", role="assistant")
    kwargs = ingest_service.queue_episode_for_enrichment.await_args.kwargs
    assert kwargs["role"] == "assistant"
