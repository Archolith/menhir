"""MCP server: tool registration and process wiring."""

from __future__ import annotations

import logging
import os

from menhir.env_file import load_menhir_env

import anyio

from archolith_mcp_framework import create_gateway_server, run_server

from menhir.infrastructure.logging_config import configure_logging
from menhir.mcp.instructions import SERVER_INSTRUCTIONS
from menhir.mcp.lifecycle import (
    _mcp_lifespan,
    _state,
)
from menhir.mcp.resources import register_memory_resources
from menhir.mcp.tools import register_all_tools

__all__ = [
    "mcp",
    "_state",
    "register_all_tools",
    "register_memory_resources",
]

load_menhir_env()
logger = logging.getLogger(__name__)

# Core tools pinned visible for LLM discovery; others found via search_tools/call_tool
mcp = create_gateway_server(
    "menhir",
    instructions=SERVER_INSTRUCTIONS,
    lifespan=_mcp_lifespan,
    always_visible=[
        "recall_memories",
        "add_memory",
        "query_structure",
        "build_context",
        "read_flagged_memories",
        "recall_context_memories",
        "list_todos",
        "add_todo",
        # The instructions send agents here for a recorded reason's exact wording; otherwise a
        # stdio agent would first have to find it through search_tools.
        "get_provenance",
    ],
)

register_all_tools(mcp)
register_memory_resources(mcp)


def apply_loopback_profile(server, *, backend_key: str, loopback_profile: str | None):
    """#439: with no backend key, offer a keyless local agent only the loopback tool set.

    Removed tools are invisible to search_tools and unreachable through call_tool. The bound
    allowlist makes BaseTool.execute refuse them too. Returns the allowlist token, or None
    when nothing was restricted (a key is set, so the backend's tier decides, or the profile is
    "operator").
    """
    from menhir.core.request_context import bind_request_tool_allowlist
    from menhir.mcp.local_profile import loopback_tool_allowlist
    from menhir.mcp.tools import ALL_TOOLS

    allowlist = loopback_tool_allowlist(loopback_profile)
    if backend_key or allowlist is None:
        return None
    registered = {getattr(tool_cls, "name", "") for tool_cls in ALL_TOOLS} - {""}
    for name in sorted(registered - allowlist):
        server.remove_tool(name)
    return bind_request_tool_allowlist(allowlist)


def main() -> None:
    import sys
    from menhir.core.backend_config import resolve_backend_auth_key
    from menhir.mcp.service_access import bind_stdio_local_trust

    configure_logging(include_console=False)
    print("[menhir] MCP stdio starting (pid={})".format(os.getpid()), file=sys.stderr, flush=True)
    # stdio MCP is a trusted local process; bind operator tier explicitly so
    # operator tools remain callable and the local trust decision is visible
    # rather than relying on the implicit empty-tier bypass (CT-002). The
    # context set here is inherited by the server's request-handling tasks.
    bind_stdio_local_trust()
    apply_loopback_profile(
        mcp,
        backend_key=resolve_backend_auth_key(),
        loopback_profile=os.environ.get("MENHIR_LOOPBACK_PROFILE"),
    )
    run_server(mcp)


if __name__ == "__main__":
    main()
