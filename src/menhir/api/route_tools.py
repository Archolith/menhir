"""Which MCP tool each named REST route corresponds to, and the allowlist check built on it.

A client allowlist names tools. The named REST routes make the same service calls as those
tools, so a client restricted to one tool must not reach the rest through REST. The router
applies :func:`enforce_route_tool_allowlist` to every route; a route with no entry here is
refused to an allowlisted client (fail closed), and a test fails until it gets one.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from menhir.mcp import contracts

#: (HTTP method, full route path template) -> MCP tool name, or None when no tool serves it.
REST_ROUTE_TOOLS: dict[tuple[str, str], str | None] = {
    ("POST", "/api/recall"): "recall_memories",
    ("POST", "/api/recall/history"): "recall_history",
    ("POST", "/api/recall/timeline"): "recall_timeline",
    ("GET", "/api/scalar-authority/{view_uuid}/contributors"): "get_scalar_view_items",
    ("GET", "/api/scalar-views/{view_uuid}/items"): "get_scalar_view_items",
    ("GET", "/api/bootstrap/flagged"): "read_flagged_memories",
    ("POST", "/api/bootstrap/context"): "recall_context_memories",
    ("POST", "/api/context"): "build_context",
    ("POST", "/api/memory"): "add_memory",
    ("POST", "/api/turn-evidence"): None,
    ("POST", "/api/episode-admission"): None,
    ("POST", "/api/tool-events"): None,
    ("GET", "/api/tool-events/dirty"): None,
    ("GET", "/api/tool-events/stale"): None,
    ("POST", "/api/tool-events/stale-verifications"): None,
    ("GET", "/api/tool-events/stale-verifications"): None,
    ("DELETE", "/api/memory/{uuid}"): "delete_memory",
    ("DELETE", "/api/namespace/{namespace}"): "delete_namespace",
    ("POST", "/api/memory/{uuid}/flag"): "flag_memory",
    ("POST", "/api/memory/{uuid}/unflag"): "unflag_memory",
    ("GET", "/api/stats"): "get_memory_stats",
    ("POST", "/api/phase3/run"): None,
    ("GET", "/api/phase3/status"): None,
    ("GET", "/api/views"): None,
    ("POST", "/api/phase3/reset"): "delete_namespace",
    ("POST", "/api/admin/clients"): "mint_client",
    ("GET", "/api/admin/clients"): "list_clients",
    ("POST", "/api/admin/clients/{client_id}/revoke"): "revoke_client",
}

#: Always allowed: liveness probes carry no data.
EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/api/health"), ("GET", "/api/ready")}
)

#: The generic backend dispatch route refuses allowlisted clients itself
#: (routes_handlers._refuse_restricted_client).
INTERNAL_DISPATCH_PATH = "/api/internal/backend/{operation}"


def _is_restricted() -> bool:
    return contracts.get_request_tool_allowlist() is not None or bool(
        contracts.get_client_tool_allowlist()
    )


async def enforce_route_tool_allowlist(request: Request) -> None:
    """Router dependency: refuse a REST route the caller's tool allowlist does not cover."""
    if not _is_restricted():
        return
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    key = (request.method.upper(), path)
    if key in EXEMPT_ROUTES or path == INTERNAL_DISPATCH_PATH:
        return
    tool = REST_ROUTE_TOOLS.get(key)
    if tool is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"`{key[0]} {path}`: this route has no tool mapping and is refused "
                "for clients with a tool allowlist"
            ),
        )
    if not contracts.tool_allowed_for_request(tool):
        raise HTTPException(
            status_code=403,
            detail=f"`{key[0]} {path}`: this client's tool allowlist does not include `{tool}`",
        )
