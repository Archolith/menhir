"""Internal backend dispatch policy: the method allowlist, per-operation tiers, and the
deprecation bridge (see routes_support)."""

_BACKEND_METHODS = {
    "queue_episode",
    "flag_memory",
    "unflag_memory",
    "promote_memory",
    "delete_memory",
    "erase_memory",
    "delete_namespace",
    "enqueue_pending_episode",
    "recall",
    "build_context",
    "view_entropy",
    "fetch_memory_by_uuid",
    "fetch_node_receipts",
    "fetch_recent_memories",
    "fetch_flagged_memories",
    "fetch_flagged_memory_bootstrap_version",
    "fetch_memories_by_scope",
    "fetch_memories_by_type",
    "get_scan_fingerprint",
    "ingest_document",
    "scan_and_write_project",
    "write_project_structure",
    "query_structure",
    "list_conflict_groups",
    "resolve_conflict_group",
    "requeue_conflicts_for_llm_review",
    "scan_for_conflicts",
    "confirm_pending_conflicts",
    "fetch_episode_processing",
    "list_episode_processing",
    "get_queue_depth",
    "get_failed_enrichment_count",
    "force_reset_failed_episode",
    "force_release_episode_lease",
    "fetch_stale_enriching_episodes",
    "recover_stale_enrichment_leases",
    "get_max_enrichment_attempts",
    "recover_orphans",
    "fetch_session_entities",
    "scheduler_force_takeover",
    "scheduler_status_snapshot",
    "scheduler_pause",
    "scheduler_resume",
    "fetch_operation_stats",
    "fetch_failure_summary",
    "fetch_enrichment_rate",
    "fetch_lifecycle_summary",
    "fetch_episode_task_events",
    "fetch_recent_failures",
    "fetch_recent_lifecycle_events",
    "record_conflict_resolution",
    "fetch_memory_overview",
    "circuit_breaker_snapshots",
    "embedding_cache_stats",
    "get_provider_config",
    "create_todo",
    "list_todos",
    "get_todo",
    "get_artifact",
    "list_artifacts",
    "list_artifact_questions",
    "get_artifact_relationships",
    "link_artifacts",
    "supersede_artifact",
    "transition_artifact_status",
    # Named `fetch_` rather than `audit_` so the read-only remainder rule below
    # classifies it by convention instead of by exception. The MCP tool it backs
    # is still called `audit_artifact_corpus`: the agent-facing name describes
    # the task, the dispatch name has to obey the tier-naming policy.
    "fetch_artifact_corpus_audit",
    "relocate_artifact_source",
    "close_todo",
    "delete_todo",
    "close_stale_todos",
    "supersede_todo",
    "resolve_todo",
    "reopen_todo",
    "link_memory_to_todo",
    "create_temporal",
    "list_temporal_in_window",
    "complete_temporal",
    "create_candidate",
    "list_candidates",
    "fetch_candidate",
    "promote_candidate",
    "reject_candidate",
    "approve_candidate",
}

# Per-operation minimum tier for the internal dispatch. THE POLICY IS TOTAL: every operation is
# either listed below or read-only by the explicit remainder rule — adding an op to
# _BACKEND_METHODS without deciding its tier is caught by the test suite (implicit policy is
# forbidden — memory-governance.md §4).
_OP_TIER_OPERATOR = {
    "delete_memory", "erase_memory", "delete_namespace",
    "resolve_conflict_group", "requeue_conflicts_for_llm_review", "scan_for_conflicts",
    "confirm_pending_conflicts", "record_conflict_resolution",
    "force_reset_failed_episode", "force_release_episode_lease",
    "recover_stale_enrichment_leases", "recover_orphans",
    "scheduler_force_takeover", "scheduler_pause", "scheduler_resume",
    "promote_candidate", "reject_candidate", "approve_candidate",
    "promote_memory",
    # CF-257 phase 0. Raised from agent tier. This op writes a structure payload the CALLER
    # produced, and judges it using a `root_path` the same caller supplied -- so the server
    # classifies a path string, never the directory that actually produced the payload. A remote
    # worktree that reports the server's canonical path is classified as the canonical clone and
    # accepted, and the write carries the per-project stale prune. No metadata check can close
    # that, because every input to it is attacker-controlled; the only sound options are a
    # trusted server-side scan (`scan_and_write_project`, which is the modern path) or operator
    # tier. It also always belonged here by this file's own rule: the sets below reserve operator
    # for what deletes, and the stale prune deletes.
    "write_project_structure",
}
_OP_TIER_AGENT = {
    "queue_episode", "flag_memory", "unflag_memory", "enqueue_pending_episode",
    "ingest_document", "scan_and_write_project",
    "create_todo", "close_todo", "delete_todo", "close_stale_todos",
    # Todo lifecycle and lineage. Same standing as the artifact writes below: each moves a
    # lifecycle state or declares a relationship, so none is readonly, and none is operator
    # either -- all are reversible and none deletes anything.
    "supersede_todo", "resolve_todo", "reopen_todo", "link_memory_to_todo",
    "create_temporal", "complete_temporal", "create_candidate",
    # Artifact writes. Declaring a relationship or moving a lifecycle state is
    # an assertion about engineering history, so it is not readonly. None are
    # operator-tier: they are all reversible and none deletes anything.
    "link_artifacts", "supersede_artifact", "transition_artifact_status",
    # Relocation changes a source locator, which is an assertion about where a
    # document is -- reversible, and never a lifecycle or relationship change,
    # so agent rather than operator. `fetch_artifact_corpus_audit` is absent
    # deliberately: it writes nothing and falls to the readonly remainder.
    "relocate_artifact_source",
}
assert _OP_TIER_OPERATOR <= _BACKEND_METHODS and _OP_TIER_AGENT <= _BACKEND_METHODS, (
    "op tier map references unknown backend operations"
)


#: Operations kept alive only as a migration bridge, with the message a caller should act on.
#:
#: CF-257. `write_project_structure` writes a structure payload the CALLER produced and judges it
#: with a `root_path` the same caller supplied, so the server classifies a path string rather than
#: the directory that produced the payload -- and the write carries the per-project stale prune.
#: No metadata check closes that, because every input to it is caller-controlled. The operator gate
#: is a bridge, NOT the permanent design: the endpoint is removed once phase 3 makes project ids
#: authoritative and an observation window shows no legitimate use.
#:
#: Failing loudly beats silently accepting an unverifiable payload that deletes rows, so the
#: refusal must say what to do instead rather than only what went wrong.
DEPRECATED_OPERATIONS: dict[str, str] = {
    "write_project_structure": (
        "write_project_structure is DEPRECATED and will be removed. It accepts a structure "
        "payload the caller produced and cannot verify which directory produced it, so a stale "
        "or secondary checkout can overwrite a project and prune its files. "
        "Use scan_and_write_project instead -- it scans server-side, so the structure is "
        "verified against a directory the server actually read. If you must submit a "
        "pre-computed payload, call this operation with an operator-tier credential."
    ),
}


def deprecated_operation_notice(operation: str) -> str | None:
    """The actionable message for a deprecated operation, or None if it is current."""
    return DEPRECATED_OPERATIONS.get(operation)


def _required_tier_for_operation(operation: str) -> str:
    if operation in _OP_TIER_OPERATOR:
        return "operator"
    if operation in _OP_TIER_AGENT:
        return "agent"
    return "readonly"  # recall/context/fetch_*/list_*/get_*/snapshots — the explicit remainder
