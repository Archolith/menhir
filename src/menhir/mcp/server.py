"""MCP server: tool registration and process wiring."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

import anyio

from archolith_mcp_framework import create_gateway_server, run_server

from menhir.infrastructure.logging_config import configure_logging
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

load_dotenv(os.getenv("ENV_FILE") or None)
logger = logging.getLogger(__name__)

# Core tools pinned visible for LLM discovery; others found via search_tools/call_tool
mcp = create_gateway_server(
    "menhir",
    instructions="Long-term graph memory for AI agents. Store, recall, and manage memories.",
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
    ],
)

register_all_tools(mcp)
register_memory_resources(mcp)


def main() -> None:
    import sys
    from menhir.mcp.service_access import bind_stdio_local_trust

    configure_logging(include_console=False)
    print("[menhir] MCP stdio starting (pid={})".format(os.getpid()), file=sys.stderr, flush=True)
    # stdio MCP is a trusted local process; bind operator tier explicitly so
    # operator tools remain callable and the local trust decision is visible
    # rather than relying on the implicit empty-tier bypass (CT-002). The
    # context set here is inherited by the server's request-handling tasks.
    bind_stdio_local_trust()
    run_server(mcp)


if __name__ == "__main__":
    main()
