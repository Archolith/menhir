"""Feature taxonomy for the usage/effectiveness dashboard.

Maps each MCP operation (function) to a parent group (e.g. ``retrieve``,
``write``) so the explorer can roll usage and effectiveness up by parent while
still letting the operator drill down to individual functions.

The parent groups mirror ``.agent/mcp-tools.yaml`` and extend it to cover the
operations that appear in telemetry but are not listed there (structure graph,
todos, resource reads).

Kept in sync with ``menhir.mcp.tools.ALL_TOOLS`` (the runtime tool registry) by
``tests/test_feature_taxonomy.py::test_every_registered_tool_is_classified`` and
``test_no_phantom_tools_in_taxonomy`` (SSOT-10) -- this taxonomy previously
omitted 8 registered tools and listed 2 tools (``memory_gateway``,
``recover_memory``) that were never actually registered.
"""

from __future__ import annotations

from typing import Iterable

# Ordered parents -> member operations. Order controls display order of the
# rollup cards. ``retrieve`` and ``write`` are surfaced first because they are
# the primary read/write verbs the operator cares about.
PARENTS: dict[str, list[str]] = {
    "retrieve": [
        "recall_memories",
        "recall_context_memories",
        "read_flagged_memories",
        "build_context",
        "get_provenance",
    ],
    "write": [
        "add_memory",
        "add_memory_and_track",
        "ingest_document",
        "ingest_project",
        "add_candidate",
    ],
    "structure": [
        "query_structure",
    ],
    "conflicts": [
        "list_conflicts",
        "resolve_conflict",
        "scan_for_conflicts",
        "run_llm_conflict_review",
        "requeue_conflicts_for_llm_review",
    ],
    "processing": [
        "get_enrichment_status",
        "watch_enrichment",
        "get_episode_trace",
        "list_enrichment_queue",
        "repair_stale_enrichment",
        "force_reenrich",
        "force_release_enrichment_lease",
    ],
    "todos": [
        "add_todo",
        "list_todos",
        "get_todo",
        "close_todo",
        "close_stale_todos",
    ],
    "artifacts": [
        "get_artifact",
        "list_artifacts",
        "list_artifact_questions",
        "get_artifact_relationships",
        "link_artifacts",
        "supersede_artifact",
        "transition_artifact",
    ],
    "stats": [
        "get_memory_stats",
        "get_client_context",
        "rate_recall",
        "view_entropy",
    ],
    "lifecycle": [
        "flag_memory",
        "unflag_memory",
        "promote_memory",
        "delete_memory",
        "delete_namespace",
        "close_memory",
        "recover_orphans",
        "force_scheduler_takeover",
        "pause_scheduler",
        "resume_scheduler",
    ],
    "clients": [
        "mint_client",
        "list_clients",
        "revoke_client",
    ],
}

_OP_TO_PARENT: dict[str, str] = {op: parent for parent, ops in PARENTS.items() for op in ops}


def classify(operation: str, kind: str) -> str:
    """Return the parent group for an operation.

    Resource reads (``memory://...``) collapse into a synthetic ``resources``
    parent; anything unmapped falls back to ``other`` so new tools still appear
    on the dashboard instead of vanishing.
    """
    if operation in _OP_TO_PARENT:
        return _OP_TO_PARENT[operation]
    if kind in {"resource", "resource_template"} or operation.startswith("memory://"):
        return "resources"
    return "other"


def parent_order() -> Iterable[str]:
    """Preferred display order of parents, retrieval-facing groups first."""
    return [*PARENTS.keys(), "resources", "other"]
