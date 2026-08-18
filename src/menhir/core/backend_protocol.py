"""Backend Protocol for menhir MCP tool/resource abstraction.

This Protocol defines the interface that both RuntimeProvider (in-process)
and BackendClient (HTTP-backed) must implement. MCP tools and resources
call methods on this interface instead of reaching into BuildArtifacts
or lifecycle._state directly.

Design principles:
- Grouped by domain, not by internal service decomposition
- All methods are async (HTTP-backed impl needs it; in-process can await trivially)
- No callbacks in signatures (HTTP can't serialize them; tools handle progress locally)
- Return types are serializable (dict, list, str, bool, int, None)
- Session is passed explicitly where needed, never inherited from process state

Implementation notes for RuntimeProvider:
- Domain objects (RecallResult, QueueResult, ContextResult, RecoveryResult) must be
  converted to dicts via .model_dump() or equivalent before returning.
- Enum parameters (preset, states) arrive as strings; convert to enum before
  calling the underlying service (e.g., QueryPreset(preset)).
- ProjectScanResult must be serialized by the caller before passing to
  write_project_structure(); RuntimeProvider can reconstruct if needed.
- recover_orphans on_progress callback is RuntimeProvider-only; Protocol
  returns the final result dict.

Implementation notes for BackendClient:
- All methods map to HTTP endpoints on the menhir serve API.
- Timeout/retry semantics may differ from in-process calls; the BackendClient
  implementation should document its defaults.

Usage (after Phase 3 migration):
    class SomeTool(BaseTool):
        async def endpoint(self, ...) -> str:
            backend = self.get_backend()  # returns MemoryBackend
            result = await backend.recall(query, preset="knowledge", limit=10)
            ...
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    """Unified interface for all MCP tool/resource service access."""

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def queue_episode(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        source: str = "mcp",
        diff: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        occurred_at: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Queue an episode for enrichment. Returns QueueResult-shaped dict.

        Args:
            turn_evidence_uuid: Optional UUID of a :TurnEvidence node for grounding
                               source='user'/'manual' claims at the top trust tier.
        """
        ...

    async def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        """Mark a memory node as permanently retained."""
        ...

    async def unflag_memory(self, node_uuid: str) -> bool:
        """Remove the permanent retention flag from a memory node."""
        ...

    async def promote_memory(self, node_uuid: str) -> bool:
        """Promote a PERSISTENT memory to PROMOTED: operator-curated, verified ground truth (SSOT-08)."""
        ...

    async def delete_memory(self, node_uuid: str) -> bool:
        """Erase a memory by UUID. True if anything was erased.

        Kept boolean for compatibility; use :meth:`erase_memory` when the caller needs to know
        WHICH outcome occurred.
        """
        ...

    async def erase_memory(self, node_uuid: str) -> dict[str, Any]:
        """Erase a memory and report the outcome (CF-165).

        Distinguishes ``erased`` from ``graph_already_absent`` (the node was gone from the
        graph but sidecar content was erased -- the state a merge leaves its absorbed
        participant in) and from ``nothing_to_erase``. A bare bool collapses the middle case
        into "not found", which is exactly wrong: something WAS erased.
        """
        ...

    async def delete_namespace(
        self,
        namespace: str,
        *,
        max_nodes: int = 200,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete all memory in a namespace silo. Refuses the default namespace.

        Args:
            max_nodes: Safety gate -- refuses (ValueError) when the namespace has more
                nodes than this, unless force=True.
            force: Bypass the max_nodes gate.
            dry_run: Report the node count and would-delete decision without deleting.
        """
        ...

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        """Re-queue a pending episode for enrichment processing."""
        ...

    # ------------------------------------------------------------------
    # Recall
    # ------------------------------------------------------------------

    async def recall(
        self,
        query: str,
        *,
        preset: str = "knowledge",
        limit: int = 10,
        include_session: bool = False,
        include_superseded: bool = False,
        wait_for_pending: bool = False,
        file_context: str | None = None,
        file_context_project: str | None = None,
        namespace: str | None = None,
        include_invalidated: bool = False,
        trace: bool = False,
    ) -> dict[str, Any]:
        """Search memory graph. Returns RecallResult-shaped dict."""
        ...

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 4000,
        preset: str = "knowledge",
        session_id: str | None = None,
        include_scores: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Build token-budgeted context pack. Returns ContextResult-shaped dict."""
        ...

    async def view_entropy(
        self,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        top_k: int = 20,
        max_views: int = 50,
    ) -> dict[str, Any]:
        """D0 view-reachability probe: for each current View, the rank + footprint at
        which recall returns the View's own canonical surface. Deterministic, LLM-free,
        read-only (does not bump access stats). Returns {"summary": ..., "views": [...]}."""
        ...

    async def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch a single memory node by UUID, optionally restricted to one namespace."""
        ...

    async def fetch_recent_memories(
        self, limit: int = 20, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch most recent memory nodes."""
        ...

    async def fetch_flagged_memories(
        self, limit: int = 50, workspace: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch permanently flagged memory nodes."""
        ...

    async def fetch_flagged_memory_bootstrap_version(
        self, workspace: str | None = None
    ) -> str:
        """Get the current version hash of flagged memories."""
        ...

    async def fetch_memories_by_scope(
        self, scope: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch memories filtered by scope, optionally restricted to one namespace."""
        ...

    async def fetch_memories_by_type(
        self, memory_type: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch memories filtered by type, optionally restricted to one namespace."""
        ...

    # ------------------------------------------------------------------
    # Structure (project scanning)
    # ------------------------------------------------------------------

    async def get_scan_fingerprint(self, project_name: str) -> str | None:
        """Get the last scan fingerprint for a project."""
        ...

    async def ingest_document(
        self,
        path: str,
        *,
        project: str | None,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
    ) -> dict[str, Any]:
        """Read a file and ingest it as a document entity + narrative episode.

        Returns {
            "entity_written": bool,
            "structure_project": str,
            "structure_path": str,
            "content_length": int,
            "narrative": str,  # full content for caller to queue as episode
        }
        """
        ...

    async def scan_and_write_project(
        self,
        path: str,
        *,
        name: str | None,
        force: bool,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Run the project scanner server-side and write the structure graph.

        Returns {"counts": {"entities": N, "edges": M}, "narrative": str, "skipped": bool}.
        Skipped is True when the fingerprint is unchanged and force is False.
        """
        ...

    async def write_project_structure(
        self,
        scan: dict[str, Any],
        *,
        session_id: str,
        user_id: str,
    ) -> dict[str, int]:
        """Write project structure scan to graph. Returns counts dict.

        RuntimeProvider: accepts ProjectScanResult.model_dump() or equivalent.
        BackendClient: sends the dict as JSON body.
        Callers must serialize ProjectScanResult before passing to this method.
        """
        ...

    async def query_structure(
        self,
        project: str,
        query_type: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Query project structure graph.

        params contains query-type-specific arguments (path_filter, file_path,
        file_paths, etc.) as a flat dict. Replaces **kwargs for serializability.
        """
        ...

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    async def list_conflict_groups(
        self, *, status: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        """List conflict groups, optionally filtered by status."""
        ...

    async def resolve_conflict_group(
        self,
        group_id: str,
        *,
        action: str,
        resolution_status: str,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        allow_promoted_removal: bool = False,
    ) -> dict[str, Any]:
        """Resolve a conflict group."""
        ...

    async def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "pending", limit: int = 50
    ) -> int:
        """Requeue conflict groups for LLM review. Returns count requeued."""
        ...

    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None
    ) -> dict[str, Any]:
        """Scan for new conflicts. Returns scan result dict."""
        ...

    async def confirm_pending_conflicts(
        self, *, limit: int = 10, verbose: bool = False
    ) -> dict[str, Any]:
        """Run LLM review on pending conflicts. Returns review result dict."""
        ...

    # ------------------------------------------------------------------
    # Enrichment / Ops
    # ------------------------------------------------------------------

    async def fetch_node_receipts(self, node_uuid: str) -> dict[str, Any] | None:
        """Fetch a node's provenance receipts: the source episodes that MENTIONS it, its
        first-class SUPPORTED_BY evidence, and its ANCHORED_TO structural paths. Returns None
        when no node has that uuid."""
        ...

    async def fetch_episode_processing(
        self, episode_uuid: str
    ) -> dict[str, Any] | None:
        """Fetch processing status for an episode."""
        ...

    async def list_episode_processing(
        self, *, states: list[str] | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        """List episodes in processing queue, optionally filtered by state."""
        ...

    async def get_queue_depth(self) -> int:
        """Get current in-memory enrichment queue depth."""
        ...

    async def get_failed_enrichment_count(self) -> int:
        """Get current failed enrichment count tracked by the ingest service."""
        ...

    async def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        """Reset a failed episode to pending state."""
        ...

    async def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int | None = None
    ) -> bool:
        """Force-release an enrichment lease."""
        ...

    async def fetch_stale_enriching_episodes(
        self, limit: int = 25
    ) -> list[dict[str, Any]]:
        """Fetch episodes stuck in ENRICHING state."""
        ...

    async def recover_stale_enrichment_leases(
        self, limit: int = 10
    ) -> dict[str, int]:
        """Recover stale enrichment leases. Returns {"recovered": N, "total_stale": N}."""
        ...

    async def get_max_enrichment_attempts(self) -> int:
        """Get configured max enrichment retry attempts."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def recover_orphans(
        self, *, max_age_hours: float = 24.0
    ) -> dict[str, Any]:
        """Recover orphaned entities. Returns RecoveryResult-shaped dict.

        Note: on_progress callback is NOT part of the Protocol — it's an
        implementation detail of RuntimeProvider. BackendClient returns
        the final result; tools handle progress display locally.
        """
        ...

    async def fetch_session_entities(
        self, *, session_id: str | None = None, max_age_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        """Fetch entities associated with a session."""
        ...

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    async def scheduler_force_takeover(self, reason: str) -> bool:
        """Force scheduler lease takeover. Returns success."""
        ...

    async def scheduler_status_snapshot(self) -> dict[str, Any] | None:
        """Get scheduler status. Returns None if scheduler unavailable."""
        ...

    async def scheduler_pause(self) -> bool:
        """Stop the maintenance scheduler loop. Returns True if it was running before stop."""
        ...

    async def scheduler_resume(self) -> bool:
        """Restart the maintenance scheduler loop. Returns True if start succeeded."""
        ...

    # ------------------------------------------------------------------
    # Telemetry (reads)
    # ------------------------------------------------------------------

    async def fetch_operation_stats(
        self, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch MCP operation statistics."""
        ...

    async def fetch_failure_summary(
        self, since_hours: int = 24
    ) -> dict[str, Any]:
        """Fetch failure summary."""
        ...

    async def fetch_enrichment_rate(
        self, since_hours: int = 24
    ) -> dict[str, Any]:
        """Fetch enrichment success/failure rates."""
        ...

    async def fetch_lifecycle_summary(
        self, since_hours: int = 24
    ) -> dict[str, Any]:
        """Fetch lifecycle event summary."""
        ...

    async def fetch_episode_task_events(
        self, episode_uuid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch LLM task events for an episode."""
        ...

    async def fetch_recent_failures(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch recent failure records."""
        ...

    async def fetch_recent_lifecycle_events(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch recent lifecycle events."""
        ...

    async def record_conflict_resolution(
        self,
        *,
        uuid_a: str,
        uuid_b: str,
        status: str,
        group_id: str,
        action: str,
        reviewed_by: str,
    ) -> None:
        """Record a conflict resolution in telemetry."""
        ...

    # ------------------------------------------------------------------
    # System / Metadata
    # ------------------------------------------------------------------

    async def fetch_memory_overview(self) -> dict[str, Any]:
        """Fetch high-level memory graph overview (node/edge counts, etc.)."""
        ...

    async def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        """Get circuit breaker state for LLM/embed/reranker clients."""
        ...

    async def embedding_cache_stats(self) -> dict[str, int]:
        """Get embedding cache hit/miss statistics."""
        ...

    async def get_provider_config(self) -> dict[str, Any]:
        """Get LLM/embedder provider configuration metadata."""
        ...

    # ------------------------------------------------------------------
    # TODOs
    # ------------------------------------------------------------------

    async def create_todo(
        self,
        *,
        content: str,
        code_ref: str | None = None,
        priority: str = "normal",
        source: str = "claude-code",
        episode_uuid: str | None = None,
        structure_project: str | None = None,
        due_date: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Create a persistent :Todo node. Returns the created todo dict.

        ``namespace`` defaults to the shared 'default' silo -- a stored todo
        always carries a non-null namespace.
        """
        ...

    async def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """List todos filtered by status (open|closed), sorted by priority then age.

        ``namespace`` is opt-in; supplying one narrows to that silo plus 'default'.
        """
        ...

    async def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        """Fetch one todo by uuid with full content and edges. None if absent."""
        ...

    async def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch one work artifact with embodiments and locations. None if absent."""
        ...

    async def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List work artifacts, newest first. ``namespace`` is opt-in and adds 'default'."""
        ...

    async def list_artifact_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Open questions across artifacts, or on one artifact."""
        ...

    async def get_artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        """Declared relationships in both directions, plus subjects and todos."""
        ...

    async def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Declare an artifact-to-artifact relationship. Never inferred."""
        ...

    async def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        """Supersede one artifact with another; edge and status move atomically."""
        ...

    async def transition_artifact_status(
        self, artifact_uuid: str, to_status: str
    ) -> dict[str, Any]:
        """Move an artifact to a new status if the transition is legal for its type."""
        ...

    async def fetch_artifact_corpus_audit(
        self,
        *,
        repo_path: str,
        repository: str,
        from_commit: str | None = None,
        conflict_limit: int = 25,
    ) -> dict[str, Any]:
        """Read-only graph/filesystem parity summary for one artifact corpus."""
        ...

    async def relocate_artifact_source(
        self,
        *,
        artifact_uuid: str,
        old_path: str,
        new_path: str,
        repository: str | None = None,
        medium: str = "markdown",
        expected_old_integrity: str = "",
        observed_integrity: str = "",
    ) -> dict[str, Any]:
        """Move one artifact source's locator, preserving identity and edges."""
        ...

    async def close_todo(self, uuid: str) -> bool:
        """Mark a todo as closed. Returns True if it was open and got closed."""
        ...

    async def delete_todo(self, uuid: str) -> bool:
        """Hard-delete a todo node regardless of status."""
        ...

    async def close_stale_todos(self, *, older_than_days: int = 60, dry_run: bool = True) -> dict[str, Any]:
        """Close todos older than N days. Returns summary with closed/preview counts."""
        ...

    # ------------------------------------------------------------------
    # TEMPORAL memories
    # ------------------------------------------------------------------

    async def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Create a TEMPORAL :Entity node with a target_date. Bypasses enrichment queue.

        Args:
            turn_evidence_uuid: Optional UUID of a :TurnEvidence node for grounding
                               source='user'/'manual' claims at the top trust tier.
        """
        ...

    async def list_temporal_in_window(
        self, *, window_days: int = 30
    ) -> list[dict[str, Any]]:
        """Return open TEMPORAL nodes whose target_date is within ±window_days of today."""
        ...

    async def complete_temporal(self, uuid: str) -> bool:
        """Mark a TEMPORAL node as completed. Suppresses hook surfacing."""
        ...

    async def create_candidate(
        self,
        *,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Create/refresh a CANDIDATE :Entity node (review tier). Bypasses enrichment queue.

        Idempotent on (source, cluster_id). Candidates are not recalled until approved.
        """
        ...

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return CANDIDATE nodes for review, newest activity first."""
        ...

    async def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        """Return a single CANDIDATE node by uuid, or None if not a candidate."""
        ...

    async def promote_candidate(self, uuid: str) -> bool:
        """Approve: flip a CANDIDATE node to PERSISTENT scope. Returns True on success."""
        ...

    async def reject_candidate(self, uuid: str) -> bool:
        """Reject: DETACH DELETE a CANDIDATE node. Returns True if one was removed."""
        ...

    async def approve_candidate(self, uuid: str) -> dict[str, Any]:
        """Approve via the service path: promote + synchronous contradiction check."""
        ...
