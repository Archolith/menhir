"""MCP tool registration — collects all tool classes and wires them to FastMCP."""

from __future__ import annotations

from menhir.mcp.contracts import assert_tool_scopes_declared, validate_tool_metadata

from typing import TYPE_CHECKING

from .conflict import CONFLICT_TOOLS
from .ingest import INGEST_TOOLS
from .ops import OPS_TOOLS
from .recall import RECALL_TOOLS

if TYPE_CHECKING:
    from fastmcp import FastMCP

ALL_TOOLS = INGEST_TOOLS + RECALL_TOOLS + CONFLICT_TOOLS + OPS_TOOLS
__all__ = ["ALL_TOOLS", "register_all_tools", "registered_tools"]


def registered_tools() -> list[type]:
    """The tools this process will actually expose.

    `ALL_TOOLS` is the permanent surface. The P2A snapshot staging tools are NOT in it: they are
    a transport-measurement surface that must be neither advertised nor invocable unless an
    operator turned the mode on, and the cheapest way to guarantee both is to never register
    them. They still pass the same metadata and tenancy validation below, and a test validates
    them unconditionally, so a broken one cannot hide behind the flag.
    """
    from menhir.mcp.tools.ingest.snapshot_staging import (
        SNAPSHOT_STAGING_TOOLS,
        staging_enabled,
    )

    return [*ALL_TOOLS, *(SNAPSHOT_STAGING_TOOLS if staging_enabled() else [])]


def register_all_tools(mcp: FastMCP) -> None:
    """Instantiate every tool class and register its handler on *mcp*."""
    tools = registered_tools()
    # CF-33: refuse to start if any tool has not declared how it relates to tenant data. This is
    # what turns "nobody considered tenancy for this tool" from a silent gap into a failed deploy.
    assert_tool_scopes_declared(tools)
    validate_tool_metadata(tools)
    for tool_cls in tools:
        tool_cls().register(mcp)
