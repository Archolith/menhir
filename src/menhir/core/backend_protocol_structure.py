"""Structure (project scanning) methods of the MemoryBackend backend protocol.

Moved verbatim from :mod:`menhir.core.backend_protocol`, which composes this
class into the public ``MemoryBackend`` facade; import MemoryBackend from there.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackendStructure(Protocol):
    """Structure (project scanning) methods, composed into ``MemoryBackend`` in menhir.core.backend_protocol."""

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
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
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
        force_identity: bool = False,
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the project scanner server-side and write the structure graph.

        Returns {"counts": {"entities": N, "edges": M}, "narrative": str, "skipped": bool}.
        Skipped is True when the fingerprint is unchanged and force is False.
        """
        ...

    # DEPRECATED (CF-257): removed after phase 3, see `DEPRECATED_OPERATIONS` in routes_support.
    # Prefer `scan_and_write_project`, which scans server-side so the payload is not
    # caller-controlled. Requires operator tier as of phase 0.
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
