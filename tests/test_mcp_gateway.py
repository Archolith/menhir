"""Tests for MCP tool registration — replaces the old gateway dispatch tests.

After migrating to cth-mcp-framework, the gateway.py with its hand-rolled
dispatch is gone. These tests verify that all tools are properly registered
on the MCP server and can be called directly.
"""

import asyncio
import json

import pytest

from menhir.mcp import server as mcp_server


def test_mcp_server_lists_all_expected_tools():
    """Verify all key tools are registered on the MCP server."""
    mcp_tools = asyncio.run(mcp_server.mcp._list_tools())
    registered_names = {t.name for t in mcp_tools}

    expected_tools = [
        "recall_memories",
        "add_memory",
        "query_structure",
        "build_context",
        "read_flagged_memories",
        "recall_context_memories",
        "list_todos",
        "add_todo",
        "flag_memory",
        "unflag_memory",
        "delete_memory",
        "ingest_project",
        "ingest_document",
        "close_todo",
        "get_client_context",
        "get_enrichment_status",
        "watch_enrichment",
        "list_enrichment_queue",
        "list_conflicts",
        "resolve_conflict",
    ]

    for name in expected_tools:
        assert name in registered_names, f"{name} not registered in MCP server"


def test_mcp_server_has_search_and_call_tools():
    """Verify search_tools and call_tool are present (from Search Transform).

    These synthetic tools are added by the Search Transform at the protocol
    level, so they appear in list_tools() but not in _list_tools().
    """
    tools = asyncio.run(mcp_server.mcp.list_tools())
    visible_names = {t.name for t in tools}

    assert "search_tools" in visible_names
    assert "call_tool" in visible_names


def test_always_visible_tools_appear_in_list_tools():
    """Pinned tools should appear in list_tools alongside search_tools/call_tool."""
    tools = asyncio.run(mcp_server.mcp.list_tools())
    visible_names = {t.name for t in tools}

    # Always-visible tools from server.py
    assert "recall_memories" in visible_names
    assert "add_memory" in visible_names
    assert "query_structure" in visible_names
    assert "read_flagged_memories" in visible_names

    # Synthetic tools
    assert "search_tools" in visible_names
    assert "call_tool" in visible_names
