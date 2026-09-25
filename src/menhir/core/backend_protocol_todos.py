"""System / Metadata methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendTodos(Protocol):
    """System / Metadata methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

    # ------------------------------------------------------------------
    # System / Metadata
    # ------------------------------------------------------------------

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        """Fetch high-level memory graph overview (node/edge counts, etc.).

        ``namespace=None`` counts every silo (operational callers); an explicit namespace scopes
        the graph counts to that tenant (CF-33).
        """
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
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
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
        namespace: str | None = None,
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

    async def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        """Close one todo and record the todo that replaced it, in one statement."""
        ...

    async def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Close a todo and record the memory that resolved it, in one statement."""
        ...

    async def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        """Reopen a todo and record the memory that reopened it, in one statement."""
        ...

    async def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        """Point a memory at a todo with `mentions` or `addresses`."""
        ...

    async def delete_todo(self, uuid: str) -> bool:
        """Hard-delete a todo node regardless of status."""
        ...

    async def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        """Close todos older than N days. Returns summary with closed/preview counts."""
        ...
