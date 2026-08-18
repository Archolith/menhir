"""MCP remote transport — SSE mount for the combined FastAPI server."""

from __future__ import annotations

from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool
from starlette.types import ASGIApp

from menhir.mcp.contracts import _tier_allows
from menhir.mcp.resources import register_memory_resources
from menhir.mcp.service_access import get_client_tool_allowlist, get_request_tier
from menhir.mcp.tools import register_all_tools

_INSTRUCTIONS = "Long-term graph memory for AI agents. Store, recall, and manage memories."


@lru_cache(maxsize=1)
def _required_tier_by_name() -> dict[str, str]:
    """Map tool name -> required_tier, mirroring the invocation gate's default."""
    from menhir.mcp.tools import ALL_TOOLS

    return {
        tool_cls.name: getattr(tool_cls, "required_tier", "agent")
        for tool_cls in ALL_TOOLS
        if hasattr(tool_cls, "name")
    }


class TierFilteredFastMCP(FastMCP):
    """FastMCP whose advertised tool list is scoped to the caller's auth tier.

    Invocation was already gated in ``contracts.py`` (a low-tier token calling an
    operator tool raises PermissionError), but ``tools/list`` advertised all tools
    to every caller. Filtering the catalog too gives defense in depth and — the
    practical win — stops small models from spending prompt budget on, and
    mis-selecting from, tools they are not allowed to invoke.

    Note the SDK's ``FastMCP`` (``mcp.server.fastmcp``) has no tool-transform hook
    like the ``fastmcp`` v2 package used by archolith_mcp_framework's gateway, so we
    override ``list_tools`` directly — that is the only seam available here.
    """

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        # Per-client allowlist (MENHIR_CLIENT_TOOLS) narrows the catalog to a
        # fixed set for named clients, independent of tier. Applied first so it
        # composes with the tier filter below: a tool must survive BOTH gates.
        # Empty = unrestricted, so unnamed/unconfigured callers are untouched.
        allowlist = get_client_tool_allowlist()
        if allowlist:
            tools = [t for t in tools if t.name in allowlist]
        tier = get_request_tier()
        # Empty tier = local stdio trust or no-auth loopback (CT-002): the
        # invocation gate skips too, so the catalog must not be filtered either.
        if not tier:
            return tools
        required = _required_tier_by_name()
        # Unknown tools default to "agent", matching the invocation gate — an
        # unmapped tool is hidden from readonly rather than leaked.
        return [t for t in tools if _tier_allows(tier, required.get(t.name, "agent"))]


def create_remote_tool_only_mcp() -> FastMCP:
    """Build the remote MCP surface.

    Exposes every tool and resource, but ``tools/list`` is filtered per-request to
    the caller's auth tier (see :class:`TierFilteredFastMCP`).
    Lifecycle is NOT managed here; the parent FastAPI app's lifespan owns
    start/stop for both SSE and streamable-HTTP mounts.
    """

    remote_mcp = TierFilteredFastMCP(
        "menhir",
        instructions=_INSTRUCTIONS,
    )
    register_all_tools(remote_mcp)
    register_memory_resources(remote_mcp)
    return remote_mcp


def create_mcp_sse_app() -> ASGIApp:
    """Build a FastMCP instance for remote SSE transport and return its ASGI app.

    Remote MCP is tool-only by design. Lifecycle is NOT managed here — the
    parent FastAPI server's lifespan owns start/stop.
    """
    remote_mcp = create_remote_tool_only_mcp()
    return remote_mcp.sse_app(mount_path="/mcp")


def create_mcp_streamable_http_app() -> tuple[ASGIApp, "FastMCP"]:
    """Build a FastMCP instance for Streamable HTTP transport.

    Returns (starlette_app, fastmcp_instance) so the caller can manage
    the session manager lifecycle in the parent app's lifespan.

    DNS rebinding protection is disabled because the parent server handles
    auth via BearerAuthMiddleware and the endpoint is intended for remote
    access through tunnels/proxies.
    """
    remote_mcp = TierFilteredFastMCP(
        "menhir",
        instructions=_INSTRUCTIONS,
        host="0.0.0.0",  # disables DNS rebinding protection — auth via BearerAuthMiddleware
        stateless_http=True,
    )
    register_all_tools(remote_mcp)
    register_memory_resources(remote_mcp)
    return remote_mcp.streamable_http_app(), remote_mcp
