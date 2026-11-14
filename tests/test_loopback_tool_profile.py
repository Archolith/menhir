"""#439: an unauthenticated local agent gets the core and curate tools, not every tool.

Covers both ways a keyless local agent connects: the backend's /mcp endpoints (auth mode NONE)
and the stdio bridge. Also #431: add_candidate and review_candidate are withheld by default.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from menhir.core.request_context import (
    get_request_tier,
    get_request_tool_allowlist,
    reset_request_tool_allowlist,
)
from menhir.mcp.local_profile import DEFAULT_DENY, loopback_tool_allowlist
from menhir.mcp.tool_profiles import (
    ADMIN,
    CORE,
    CURATE,
    OPERATE,
    PROFILES,
    production_tool_tiers,
)

pytestmark = pytest.mark.unit

TIERS = production_tool_tiers()
DEFAULT = loopback_tool_allowlist("default")
OPERATOR_ONLY = sorted(PROFILES[OPERATE] | PROFILES[ADMIN])


# ------------------------------------------------------------------ the default tool set


def test_default_is_core_and_curate_without_candidate_staging_or_review() -> None:
    assert DEFAULT == (PROFILES[CORE] | PROFILES[CURATE]) - {"add_candidate", "review_candidate"}
    assert DEFAULT_DENY == ("add_candidate", "review_candidate")
    assert "list_candidates" in DEFAULT


def test_default_excludes_every_operate_and_admin_tool() -> None:
    assert DEFAULT.isdisjoint(PROFILES[OPERATE] | PROFILES[ADMIN])
    for tool in ("pause_scheduler", "force_release_enrichment_lease", "recover_orphans",
                 "mint_client", "revoke_client", "list_clients", "delete_namespace"):
        assert tool not in DEFAULT


def test_operator_profile_means_every_tool() -> None:
    assert loopback_tool_allowlist("operator") is None
    assert loopback_tool_allowlist(" OPERATOR ") is None
    assert loopback_tool_allowlist(None) == DEFAULT
    assert loopback_tool_allowlist("") == DEFAULT


def test_an_unknown_profile_value_is_refused() -> None:
    with pytest.raises(ValueError, match="MENHIR_LOOPBACK_PROFILE"):
        loopback_tool_allowlist("admin")


def test_the_setting_is_read_and_validated_from_the_environment(monkeypatch) -> None:
    from menhir.config import MemorySettings

    monkeypatch.delenv("MENHIR_LOOPBACK_PROFILE", raising=False)
    assert MemorySettings.from_env().loopback_profile == "default"
    monkeypatch.setenv("MENHIR_LOOPBACK_PROFILE", "operator")
    assert MemorySettings.from_env().loopback_profile == "operator"
    monkeypatch.setenv("MENHIR_LOOPBACK_PROFILE", "everything")
    with pytest.raises(ValueError, match="MENHIR_LOOPBACK_PROFILE"):
        MemorySettings.from_env()


# ------------------------------------------------------------------ backend /mcp, auth mode NONE


def _scope(path: str, host: bytes | None = b"127.0.0.1:8100", *, forwarded: bool = False):
    headers = [(b"host", host)] if host is not None else []
    if forwarded:
        headers.append((b"x-forwarded-for", b"203.0.113.9"))
    return {"type": "http", "path": path, "method": "POST", "client": ("127.0.0.1", 51000),
            "headers": headers, "query_string": b""}


def _run(mw_kwargs: dict, scope: dict) -> dict:
    from menhir.api.auth import BearerAuthMiddleware

    seen: dict = {}

    async def app(scope, receive, send):
        seen["reached"] = True
        seen["tier"] = get_request_tier()
        seen["allowlist"] = get_request_tool_allowlist()

    async def receive():  # pragma: no cover
        return {}

    async def send(message):
        seen.setdefault("status", message.get("status"))

    asyncio.run(BearerAuthMiddleware(app, **mw_kwargs)(scope, receive, send))
    return seen


@pytest.mark.parametrize("path", ["/mcp", "/mcp/messages/", "/mcp-http"])
def test_a_keyless_loopback_mcp_call_gets_the_default_tool_set(path) -> None:
    seen = _run({"loopback_tool_allowlist": DEFAULT}, _scope(path))
    assert seen["reached"] and seen["allowlist"] == DEFAULT


def test_the_operator_opt_in_binds_no_allowlist_on_mcp() -> None:
    seen = _run({"loopback_tool_allowlist": None}, _scope("/mcp"))
    assert seen["reached"] and seen["allowlist"] is None and seen["tier"] == "operator"


def test_api_routes_keep_the_operator_tier_and_no_allowlist() -> None:
    """The CLI and the explorer use /api; the loopback profile applies to MCP only."""
    seen = _run({"loopback_tool_allowlist": DEFAULT}, _scope("/api/internal/backend/pause"))
    assert seen["reached"] and seen["tier"] == "operator" and seen["allowlist"] is None


@pytest.mark.parametrize("scope", [
    _scope("/mcp", b"evil.example:8100"),
    _scope("/mcp", b"127.0.0.1.evil.example"),
    _scope("/mcp", forwarded=True),
], ids=["rebound-host", "lookalike-host", "proxied"])
def test_non_loopback_or_relayed_mcp_calls_get_no_keyless_grant(scope) -> None:
    """#560's guard still decides first: neither the default nor the opt-in reaches them."""
    for allowlist in (DEFAULT, None):
        seen = _run({"loopback_tool_allowlist": allowlist}, dict(scope))
        assert not seen.get("reached") and seen["status"] == 401


def test_a_keyed_mcp_call_is_unaffected_by_the_loopback_profile() -> None:
    scope = _scope("/mcp")
    scope["headers"].append((b"authorization", b"Bearer operator-secret"))
    seen = _run({"operator_key": "operator-secret", "loopback_tool_allowlist": DEFAULT}, scope)
    assert seen["reached"] and seen["tier"] == "operator" and seen["allowlist"] is None


def test_the_allowlist_does_not_outlive_the_request() -> None:
    _run({"loopback_tool_allowlist": DEFAULT}, _scope("/mcp"))
    assert get_request_tool_allowlist() is None


def test_the_server_wires_the_setting_into_the_middleware(monkeypatch) -> None:
    from menhir.api import server_support
    from menhir.config import MemorySettings

    captured: dict = {}

    def fake_mw(app, **kwargs):
        captured.update(kwargs)
        return app

    monkeypatch.setattr(server_support, "BearerAuthMiddleware", fake_mw)
    monkeypatch.setattr(server_support, "RequestContextMiddleware", lambda app: app)
    for value, expected in (("default", DEFAULT), ("operator", None)):
        monkeypatch.setenv("MENHIR_LOOPBACK_PROFILE", value)
        captured.clear()
        server_support.wrap_server_middlewares(
            object(), settings=MemorySettings.from_env(), oauth_config=None,
            client_token_store=None,
        )
        assert captured["loopback_tool_allowlist"] == expected


# ------------------------------------------------------------------ stdio bridge


def _catalog(server) -> set[str]:
    """Registered tools, not the gateway's pinned list: what search_tools and call_tool reach."""

    async def present() -> set[str]:
        names = set()
        for name in TIERS:
            try:
                if await server.get_tool(name) is not None:
                    names.add(name)
            except Exception:  # noqa: BLE001, S112 - removed
                continue
        return names

    return asyncio.run(present())


def _fresh_gateway():
    from archolith_mcp_framework import create_gateway_server

    from menhir.mcp.tools import register_all_tools

    server = create_gateway_server("menhir-test", always_visible=["recall_memories"])
    register_all_tools(server)
    return server


async def _names_and_search(server, query: str) -> tuple[set[str], str]:
    from fastmcp import Client

    async with Client(server) as client:
        listed = {t.name for t in await client.list_tools()}
        result = await client.call_tool("search_tools", {"query": query}, raise_on_error=False)
        text = json.dumps([getattr(c, "text", "") for c in result.content])
        return listed, text


def test_a_keyless_stdio_bridge_keeps_only_the_default_tools() -> None:
    from menhir.mcp.server import apply_loopback_profile

    server = _fresh_gateway()
    token = apply_loopback_profile(server, backend_key="", loopback_profile="default")
    try:
        assert token is not None and get_request_tool_allowlist() == DEFAULT
    finally:
        reset_request_tool_allowlist(token)
    assert _catalog(server) == DEFAULT


def test_stdio_search_and_call_cannot_reach_a_removed_tool() -> None:
    from menhir.mcp.server import apply_loopback_profile

    server = _fresh_gateway()
    token = apply_loopback_profile(server, backend_key="", loopback_profile="default")
    try:
        listed, text = asyncio.run(_names_and_search(
            server, "pause scheduler lease orphans mint revoke client delete namespace candidate"))

        async def call():
            from fastmcp import Client

            async with Client(server) as client:
                return await client.call_tool(
                    "call_tool", {"name": "pause_scheduler", "arguments": {}},
                    raise_on_error=False)

        refused = asyncio.run(call())
    finally:
        reset_request_tool_allowlist(token)
    assert listed == {"recall_memories", "search_tools", "call_tool"}
    for tool in [*OPERATOR_ONLY, "add_candidate", "review_candidate"]:
        assert f'"{tool}"' not in text and f"\\\"{tool}\\\"" not in text, tool
    refusal = " ".join(getattr(c, "text", "") for c in refused.content)
    assert "Unknown tool: 'pause_scheduler'" in refusal


@pytest.mark.parametrize("backend_key,profile", [("agent-key", "default"), ("", "operator")])
def test_a_stdio_bridge_with_a_key_or_the_opt_in_keeps_every_tool(backend_key, profile) -> None:
    from menhir.mcp.server import apply_loopback_profile

    server = _fresh_gateway()
    assert apply_loopback_profile(server, backend_key=backend_key, loopback_profile=profile) is None
    assert _catalog(server) == set(TIERS)
    assert get_request_tool_allowlist() is None


def test_stdio_main_reads_the_key_and_the_setting(monkeypatch) -> None:
    from menhir.mcp import server as stdio

    calls: dict = {}
    monkeypatch.setattr(stdio, "configure_logging", lambda **_: None)
    monkeypatch.setattr("menhir.mcp.service_access.bind_stdio_local_trust", lambda: None)
    monkeypatch.setattr(stdio, "run_server", lambda server: calls.setdefault("ran", server))
    monkeypatch.setattr(stdio, "apply_loopback_profile",
                        lambda server, **kw: calls.setdefault("profile", kw))
    monkeypatch.setattr("menhir.core.backend_config.resolve_backend_auth_key", lambda: "")
    monkeypatch.setenv("MENHIR_LOOPBACK_PROFILE", "operator")
    stdio.main()
    assert calls["profile"] == {"backend_key": "", "loopback_profile": "operator"}
    assert calls["ran"] is stdio.mcp
