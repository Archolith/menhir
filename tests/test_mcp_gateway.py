"""Tests for MCP tool registration — replaces the old gateway dispatch tests.

After migrating to archolith-mcp-framework, the gateway.py with its hand-rolled
dispatch is gone. These tests verify that all tools are properly registered
on the MCP server and can be called directly.
"""

import asyncio
import json

import pytest

from menhir.mcp import server as mcp_server


# Registration is where several per-tool properties live (namespace threading,
# ownership guards, pinned-namespace application), and a membership-only subset
# assert cannot see any of them regress by deletion. Equality asserts both that
# nothing expected was dropped and that nothing was added without updating this list.
EXPECTED_TOOL_NAMES = frozenset({
    "add_candidate",
    "add_memory",
    "add_memory_and_track",
    "add_todo",
    "audit_artifact_corpus",
    "build_context",
    "close_memory",
    "close_stale_todos",
    "close_todo",
    "link_memory_to_todo",
    "reopen_todo",
    "resolve_todo",
    "supersede_todo",
    "delete_memory",
    "delete_namespace",
    "flag_memory",
    "force_reenrich",
    "force_release_enrichment_lease",
    "force_scheduler_takeover",
    "get_artifact",
    "get_artifact_relationships",
    "get_client_context",
    "get_enrichment_status",
    "get_episode_trace",
    "get_memory_stats",
    "get_provenance",
    "get_todo",
    "ingest_document",
    "ingest_project",
    "link_artifacts",
    "list_artifact_questions",
    "list_artifacts",
    "list_clients",
    "list_conflicts",
    "list_enrichment_queue",
    "list_todos",
    "mint_client",
    "pause_scheduler",
    "promote_memory",
    "query_structure",
    "rate_recall",
    "read_flagged_memories",
    "recall_context_memories",
    "recall_memories",
    "recover_orphans",
    "relocate_artifact_source",
    "repair_stale_enrichment",
    "requeue_conflicts_for_llm_review",
    "resolve_conflict",
    "resume_scheduler",
    "revoke_client",
    "run_llm_conflict_review",
    "scan_for_conflicts",
    "supersede_artifact",
    "transition_artifact",
    "unflag_memory",
    "view_entropy",
    "watch_enrichment",
})


def test_mcp_server_lists_all_expected_tools():
    """Verify every expected tool is registered, and no extra tool is registered."""
    mcp_tools = asyncio.run(mcp_server.mcp._list_tools())
    registered_names = {t.name for t in mcp_tools}

    missing = sorted(EXPECTED_TOOL_NAMES - registered_names)
    unexpected = sorted(registered_names - EXPECTED_TOOL_NAMES)
    assert not missing, f"tool(s) dropped from registration: {missing}"
    assert not unexpected, f"tool(s) added without updating EXPECTED_TOOL_NAMES: {unexpected}"


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
