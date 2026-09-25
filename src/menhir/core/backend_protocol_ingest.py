"""Ingest methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendIngest(Protocol):
    """Ingest methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

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
