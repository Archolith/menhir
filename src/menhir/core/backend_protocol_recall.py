"""Recall methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendRecall(Protocol):
    """Recall methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

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
        self,
        limit: int = 50,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch permanently flagged memory nodes."""
        ...

    async def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
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
