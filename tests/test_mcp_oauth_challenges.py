"""ChatGPT-facing OAuth challenge behavior for MCP tool results."""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from menhir.config.oauth import OAuthConfig
from menhir.core.request_context import (
    bind_request_auth_mode,
    bind_request_tier,
    reset_request_auth_mode,
    reset_request_session,
    reset_request_tier,
)
from menhir.mcp import contracts
from menhir.mcp.contracts import BaseTool, ToolScope
from menhir.mcp.service_access import (
    bind_request_oauth_context,
    bind_request_session,
    reset_request_oauth_context,
)
from menhir.mcp.telemetry.tracker import PREVIEW_UNAUTHORIZED, STAGE_DENIED

pytestmark = pytest.mark.unit


class _RecordingStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, **row: Any) -> None:
        self.rows.append(row)


class _AgentTool(BaseTool):
    name = "agent_tool"
    title = "Agent Tool"
    description = "Exercise an agent-tier operation."
    scope = ToolScope.GLOBAL
    required_tier = "agent"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(self) -> str:
        return "ok"


class _DomainRefusalTool(_AgentTool):
    name = "domain_refusal"
    title = "Domain Refusal"

    async def endpoint(self) -> str:
        raise PermissionError("domain policy refused this operation")


def _oauth_config() -> OAuthConfig:
    return OAuthConfig(
        enabled=True,
        public_base_url="https://memory.example.com",
        resource="https://memory.example.com/mcp-http",
        scopes_supported=("menhir:read", "menhir:write", "menhir:admin"),
    )


async def _execute_with_oauth_context(
    monkeypatch: pytest.MonkeyPatch,
    tool: BaseTool,
    *,
    tier: str,
    scopes: frozenset[str],
) -> tuple[Any, _RecordingStore]:
    from menhir.mcp.telemetry.tracker import track_mcp_call

    store = _RecordingStore()

    async def tracked(**kwargs: Any) -> Any:
        return await track_mcp_call(**kwargs, store=store)

    monkeypatch.setattr(contracts, "track_mcp_call", tracked)
    auth_token = bind_request_auth_mode("oauth")
    oauth_token = bind_request_oauth_context(_oauth_config(), scopes)
    session_token = bind_request_session(
        "owner",
        "session-1",
        client_id="client-1",
        client_name="chatgpt",
    )
    tier_token = bind_request_tier(tier)
    try:
        result = await tool.execute()
    finally:
        reset_request_tier(tier_token)
        reset_request_session(session_token)
        reset_request_oauth_context(oauth_token)
        reset_request_auth_mode(auth_token)
    return result, store


async def test_oauth_tier_denial_returns_serialized_tool_challenge(monkeypatch):
    result, store = await _execute_with_oauth_context(
        monkeypatch,
        _AgentTool(),
        tier="readonly",
        scopes=frozenset({"menhir:read"}),
    )

    assert isinstance(result, CallToolResult)
    payload = result.model_dump(by_alias=True, exclude_none=True)
    assert payload["isError"] is True
    assert payload["content"][0]["type"] == "text"
    challenge = payload["_meta"]["mcp/www_authenticate"]
    assert 'resource_metadata="https://memory.example.com/.well-known/oauth-protected-resource"' in challenge
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="menhir:write"' in challenge
    assert "offline_access" not in challenge

    assert len(store.rows) == 1
    assert store.rows[0]["stage"] == STAGE_DENIED
    assert store.rows[0]["payload_preview"] == PREVIEW_UNAUTHORIZED


async def test_registered_fastmcp_tool_preserves_auth_challenge_meta(monkeypatch):
    from menhir.mcp.telemetry.tracker import track_mcp_call

    store = _RecordingStore()

    async def tracked(**kwargs: Any) -> Any:
        return await track_mcp_call(**kwargs, store=store)

    monkeypatch.setattr(contracts, "track_mcp_call", tracked)
    mcp = FastMCP("oauth-challenge-wire")
    _AgentTool().register(mcp)
    auth_token = bind_request_auth_mode("oauth")
    oauth_token = bind_request_oauth_context(
        _oauth_config(),
        frozenset({"menhir:read"}),
    )
    session_token = bind_request_session(
        "owner",
        "session-wire",
        client_id="client-wire",
        client_name="chatgpt",
    )
    tier_token = bind_request_tier("readonly")
    try:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "agent_tool",
                {},
                raise_on_error=False,
            )
    finally:
        reset_request_tier(tier_token)
        reset_request_session(session_token)
        reset_request_oauth_context(oauth_token)
        reset_request_auth_mode(auth_token)

    assert result.is_error is True
    assert result.meta is not None
    challenge = result.meta["mcp/www_authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert 'scope="menhir:write"' in challenge


@pytest.mark.parametrize(
    "minimum_scope",
    ["menhir:read", "menhir:write", "menhir:admin"],
)
def test_challenge_uses_exact_minimum_permission_scope(minimum_scope):
    from menhir.mcp.service_access import oauth_tool_scope_denial

    auth_token = bind_request_auth_mode("oauth")
    oauth_token = bind_request_oauth_context(
        _oauth_config(),
        frozenset(),
    )
    try:
        denial = oauth_tool_scope_denial(
            tool_name="sample_tool",
            minimum_scope=minimum_scope,
        )
    finally:
        reset_request_oauth_context(oauth_token)
        reset_request_auth_mode(auth_token)

    assert denial is not None
    assert f'scope="{minimum_scope}"' in denial.challenge


async def test_domain_permission_error_is_not_an_oauth_challenge(monkeypatch):
    result, store = await _execute_with_oauth_context(
        monkeypatch,
        _DomainRefusalTool(),
        tier="agent",
        scopes=frozenset({"menhir:write"}),
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")
    assert "mcp/www_authenticate" not in result
    assert store.rows[0]["stage"] != STAGE_DENIED
