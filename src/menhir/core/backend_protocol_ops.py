"""Enrichment / Ops methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendOps(Protocol):
    """Enrichment / Ops methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

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
        self, *, states: list[str] | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """List episodes in processing queue, optionally filtered by state and namespace."""
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
