"""Tests for destructive operation audit logging (Phase 0, Item 4)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from menhir.mcp.contracts import BaseTextTool
from menhir.mcp.service_access import (
    bind_request_auth_mode,
    bind_request_session,
    bind_request_tier,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)


@pytest.fixture(autouse=True)
def _no_client_restrictions(monkeypatch):
    """Isolate these tests from per-client identity config (CF-32).

    This file exercises query-auth policy, not client identity. When CF-32's identity refusal
    landed, every test here failed on a developer machine whose `.env` configures
    MENHIR_CLIENT_NAMESPACES -- because an unrecognized client name is now refused, and these
    tests bind `client_name="web"`. The tests were silently depending on ambient environment,
    which is why the failure looked unrelated to what they test.
    """
    for var in ("MENHIR_CLIENT_NAMESPACES", "MENHIR_CLIENT_TOOLS", "MENHIR_KNOWN_CLIENTS"):
        monkeypatch.setenv(var, "")


class _ReadonlyTool(BaseTextTool):
    """Readonly tool should not emit audit log."""
    name = "recall_memories"
    description = "readonly tool"
    required_tier = "readonly"

    async def endpoint(self) -> str:
        return "ok"


class _AgentTool(BaseTextTool):
    """Agent-tier tool should not emit audit log."""
    name = "add_memory"
    description = "agent tier tool"
    required_tier = "agent"

    async def endpoint(self) -> str:
        return "ok"


class _OperatorTool(BaseTextTool):
    """Operator-tier tool should emit audit log."""
    name = "delete_memory"
    description = "operator tool"
    required_tier = "operator"

    async def endpoint(self) -> str:
        return "deleted"


def test_readonly_mcp_tool_does_not_emit_audit_log(monkeypatch):
    """Readonly MCP tool execution should not trigger audit log."""
    mock_record_destructive_op = MagicMock()
    monkeypatch.setattr(
        "menhir.mcp.contracts.record_destructive_op",
        mock_record_destructive_op,
    )

    tier_token = bind_request_tier("readonly")
    session_token = bind_request_session("user-1", "sess-1", client_id="client-1", client_name="web")
    try:
        tool = _ReadonlyTool()
        output = asyncio.run(tool.execute())
    finally:
        reset_request_session(session_token)
        reset_request_tier(tier_token)

    assert output == "ok"
    mock_record_destructive_op.assert_not_called()


def test_agent_mcp_tool_does_not_emit_audit_log(monkeypatch):
    """Agent-tier MCP tool execution should not trigger audit log."""
    mock_record_destructive_op = MagicMock()
    monkeypatch.setattr(
        "menhir.mcp.contracts.record_destructive_op",
        mock_record_destructive_op,
    )

    tier_token = bind_request_tier("agent")
    session_token = bind_request_session("user-1", "sess-2", client_id="client-2", client_name="web")
    try:
        tool = _AgentTool()
        output = asyncio.run(tool.execute())
    finally:
        reset_request_session(session_token)
        reset_request_tier(tier_token)

    assert output == "ok"
    mock_record_destructive_op.assert_not_called()


def test_operator_mcp_tool_emits_audit_log(monkeypatch):
    """Operator-tier MCP tool execution should emit audit log."""
    mock_record_destructive_op = MagicMock()
    monkeypatch.setattr(
        "menhir.mcp.contracts.record_destructive_op",
        mock_record_destructive_op,
    )

    tier_token = bind_request_tier("operator")
    session_token = bind_request_session("user-1", "sess-3", client_id="client-3", client_name="web")
    try:
        tool = _OperatorTool()
        output = asyncio.run(tool.execute())
    finally:
        reset_request_session(session_token)
        reset_request_tier(tier_token)

    assert output == "deleted"
    mock_record_destructive_op.assert_called_once()
    call_kwargs = mock_record_destructive_op.call_args.kwargs
    assert call_kwargs["surface"] == "mcp"
    assert call_kwargs["name"] == "delete_memory"
    assert call_kwargs["tier"] == "operator"
    assert call_kwargs["session_id"] == "sess-3"
    assert call_kwargs["user_id"] == "user-1"


def test_query_auth_logs_usage(monkeypatch):
    """Query-auth tool invocation should log usage."""
    mock_record_mcp_event = MagicMock()
    monkeypatch.setattr(
        "menhir.mcp.contracts.record_mcp_event",
        mock_record_mcp_event,
    )

    token = bind_request_auth_mode("query")
    session_token = bind_request_session("user-2", "sess-4", client_id="client-4", client_name="web")
    try:
        tool = _ReadonlyTool()
        output = asyncio.run(tool.execute())
    finally:
        reset_request_session(session_token)
        reset_request_auth_mode(token)

    assert output == "ok"
    # Should have been called for query_auth_usage
    calls = [call for call in mock_record_mcp_event.call_args_list
             if call.kwargs.get("operation") == "query_auth_usage"]
    assert len(calls) == 1, f"Expected 1 query_auth_usage call, got {len(calls)}"
    assert calls[0].kwargs["payload"]["tool"] == "recall_memories"
