from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from menhir.mcp import contracts
from menhir.mcp.contracts import BaseTextTool, _compute_query_auth_allowed_tools
from menhir.mcp.service_access import (
    bind_request_auth_mode,
    bind_request_session,
    bind_request_tier,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)
from menhir.mcp.tools import ALL_TOOLS


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


def test_query_auth_allowlist_equals_readonly_tools_plus_add_memory():
    """Verify the allowlist is exactly {readonly tools} ∪ {add_memory}."""
    expected = {"add_memory"}
    for tool_cls in ALL_TOOLS:
        if hasattr(tool_cls, "required_tier") and tool_cls.required_tier == "readonly":
            expected.add(tool_cls.name)

    actual = _compute_query_auth_allowed_tools()
    assert actual == frozenset(expected), (
        f"QUERY_AUTH_ALLOWED_TOOLS mismatch. "
        f"Expected: {expected}, "
        f"Got: {set(actual)}"
    )


class _AllowedTool(BaseTextTool):
    name = "recall_memories"
    description = "allowed under query auth"

    async def endpoint(self) -> str:
        return "ok"


class _BlockedTool(BaseTextTool):
    name = "add_memory"
    description = "blocked under query auth"

    async def endpoint(self) -> str:
        return "should-not-run"


class _AddMemoryTool(BaseTextTool):
    name = "add_memory"
    description = "rate-limited under query auth"

    async def endpoint(self) -> str:
        return "queued"


def test_query_auth_allows_read_only_tool_execution():
    token = bind_request_auth_mode("query")
    session_token = bind_request_session("user-1", "sess-1", client_id="client-1", client_name="web")
    try:
        result = _AllowedTool()
        output = asyncio.run(result.execute())
    finally:
        reset_request_session(session_token)
        reset_request_auth_mode(token)
    assert output == "ok"


def test_query_auth_allows_add_memory_tool_execution():
    """add_memory is allowed via query auth (has rate budget)."""
    token = bind_request_auth_mode("query")
    session_token = bind_request_session("user-1", "sess-2", client_id="client-2", client_name="web")
    try:
        result = _BlockedTool()  # name="add_memory", which is in the allowlist
        output = asyncio.run(result.execute())
    finally:
        reset_request_session(session_token)
        reset_request_auth_mode(token)
    assert output == "should-not-run"


def test_query_auth_blocks_non_allowed_write_tool_execution():
    """Non-add_memory write tools are blocked via query auth."""
    class _WriteTool(BaseTextTool):
        name = "flag_memory"
        description = "blocked under query auth"
        required_tier = "agent"

        async def endpoint(self) -> str:
            return "should-not-run"

    token = bind_request_auth_mode("query")
    session_token = bind_request_session("user-1", "sess-3", client_id="client-3", client_name="web")
    try:
        result = _WriteTool()
        output = asyncio.run(result.execute())
    finally:
        reset_request_session(session_token)
        reset_request_auth_mode(token)
    assert "PermissionError" in output
    assert "query-string auth cannot invoke" in output


def test_header_auth_keeps_write_tool_available():
    class _WriteTool(BaseTextTool):
        name = "flag_memory"
        description = "allowed under header auth"
        required_tier = "agent"

        async def endpoint(self) -> str:
            return "ok"

    token = bind_request_auth_mode("header")
    session_token = bind_request_session("user-1", "sess-4", client_id="client-4", client_name="web")
    tier_token = bind_request_tier("agent")
    try:
        result = _WriteTool()
        output = asyncio.run(result.execute())
    finally:
        reset_request_tier(tier_token)
        reset_request_session(session_token)
        reset_request_auth_mode(token)
    assert output == "ok"


def test_query_auth_rate_limits_add_memory_by_client(monkeypatch):
    contracts._query_add_memory_events.clear()
    timestamps = iter(float(i) for i in range(20))
    monkeypatch.setattr(contracts, "time", SimpleNamespace(time=lambda: next(timestamps)))
    token = bind_request_auth_mode("query")
    session_token = bind_request_session("user-1", "sess-4", client_id="client-4", client_name="web")
    try:
        tool = _AddMemoryTool()
        for _ in range(contracts.QUERY_AUTH_ADD_MEMORY_LIMIT):
            assert asyncio.run(tool.execute()) == "queued"
        blocked = asyncio.run(tool.execute())
    finally:
        reset_request_session(session_token)
        reset_request_auth_mode(token)
        contracts._query_add_memory_events.clear()
    assert "PermissionError" in blocked
    assert "rate limit exceeded for `add_memory`" in blocked


class _ReadonlyTierTool(BaseTextTool):
    name = "recall_memories"
    required_tier = "readonly"
    description = "accessible to all tiers"

    async def endpoint(self) -> str:
        return "ok"


class _AgentTierTool(BaseTextTool):
    name = "add_memory"
    required_tier = "agent"
    description = "accessible to agent and operator"

    async def endpoint(self) -> str:
        return "ok"


class _OperatorTierTool(BaseTextTool):
    name = "delete_memory"
    required_tier = "operator"
    description = "accessible to operator only"

    async def endpoint(self) -> str:
        return "ok"


def _run_with_tier(tool: BaseTextTool, tier: str) -> str:
    tier_token = bind_request_tier(tier)
    try:
        return asyncio.run(tool.execute())
    finally:
        reset_request_tier(tier_token)


def test_readonly_tier_allows_readonly_tool():
    assert _run_with_tier(_ReadonlyTierTool(), "readonly") == "ok"


def test_readonly_tier_blocks_agent_tool():
    result = _run_with_tier(_AgentTierTool(), "readonly")
    assert "cannot invoke" in result


def test_readonly_tier_blocks_operator_tool():
    result = _run_with_tier(_OperatorTierTool(), "readonly")
    assert "cannot invoke" in result


def test_agent_tier_allows_readonly_tool():
    assert _run_with_tier(_ReadonlyTierTool(), "agent") == "ok"


def test_agent_tier_allows_agent_tool():
    assert _run_with_tier(_AgentTierTool(), "agent") == "ok"


def test_agent_tier_blocks_operator_tool():
    result = _run_with_tier(_OperatorTierTool(), "agent")
    assert "cannot invoke" in result


def test_operator_tier_allows_readonly_tool():
    assert _run_with_tier(_ReadonlyTierTool(), "operator") == "ok"


def test_operator_tier_allows_agent_tool():
    assert _run_with_tier(_AgentTierTool(), "operator") == "ok"


def test_operator_tier_allows_operator_tool():
    assert _run_with_tier(_OperatorTierTool(), "operator") == "ok"


def test_no_tier_set_does_not_enforce():
    # Empty tier → bypass enforcement entirely
    assert _run_with_tier(_OperatorTierTool(), "") == "ok"


def test_query_auth_add_memory_limit_is_per_client(monkeypatch):
    contracts._query_add_memory_events.clear()
    timestamps = iter(float(i) for i in range(40))
    monkeypatch.setattr(contracts.time, "time", lambda: next(timestamps))
    first_token = bind_request_auth_mode("query")
    first_session = bind_request_session("user-1", "sess-5", client_id="client-a", client_name="web")
    try:
        tool = _AddMemoryTool()
        for _ in range(contracts.QUERY_AUTH_ADD_MEMORY_LIMIT):
            assert asyncio.run(tool.execute()) == "queued"
    finally:
        reset_request_session(first_session)
        reset_request_auth_mode(first_token)

    second_token = bind_request_auth_mode("query")
    second_session = bind_request_session("user-2", "sess-6", client_id="client-b", client_name="web")
    try:
        tool = _AddMemoryTool()
        output = asyncio.run(tool.execute())
    finally:
        reset_request_session(second_session)
        reset_request_auth_mode(second_token)
        contracts._query_add_memory_events.clear()
    assert output == "queued"
