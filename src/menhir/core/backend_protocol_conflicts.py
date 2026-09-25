"""Conflicts methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendConflicts(Protocol):
    """Conflicts methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------

    async def list_conflict_groups(
        self, *, status: str | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """List conflict groups, optionally filtered by status and namespace."""
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
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a conflict group, optionally restricted to one namespace."""
        ...

    async def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "pending", limit: int = 50,
        namespace: str | None = None,
    ) -> int:
        """Requeue conflict groups for LLM review. Returns count requeued."""
        ...

    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Scan for new conflicts. Returns scan result dict."""
        ...

    async def confirm_pending_conflicts(
        self, *, limit: int = 10, verbose: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Run LLM review on pending conflicts. Returns review result dict."""
        ...
