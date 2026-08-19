"""MCP tool registration — collects all tool classes and wires them to FastMCP."""

from __future__ import annotations

from menhir.mcp.contracts import assert_tool_scopes_declared

from typing import TYPE_CHECKING

from .conflict import CONFLICT_TOOLS
from .ingest import INGEST_TOOLS
from .ops import OPS_TOOLS
from .recall import RECALL_TOOLS

if TYPE_CHECKING:
    from fastmcp import FastMCP

ALL_TOOLS = INGEST_TOOLS + RECALL_TOOLS + CONFLICT_TOOLS + OPS_TOOLS
__all__ = ["ALL_TOOLS", "register_all_tools"]


def register_all_tools(mcp: FastMCP) -> None:
    """Instantiate every tool class and register its handler on *mcp*."""
    # CF-33: refuse to start if any tool has not declared how it relates to tenant data. This is
    # what turns "nobody considered tenancy for this tool" from a silent gap into a failed deploy.
    assert_tool_scopes_declared(ALL_TOOLS)
    for tool_cls in ALL_TOOLS:
        tool_cls().register(mcp)
