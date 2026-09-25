"""TEMPORAL memories methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendTemporal(Protocol):
    """TEMPORAL memories methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

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
        self, *, window_days: int = 30, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return open TEMPORAL nodes whose target_date is within ±window_days of today.

        ``namespace`` is opt-in and scopes the read to one silo.
        """
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
        namespace: str | None = None,
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
