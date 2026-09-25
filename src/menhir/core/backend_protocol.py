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

from typing import Protocol, runtime_checkable

from .backend_protocol_ingest import MemoryBackendIngest
from .backend_protocol_recall import MemoryBackendRecall
from .backend_protocol_structure import MemoryBackendStructure
from .backend_protocol_conflicts import MemoryBackendConflicts
from .backend_protocol_ops import MemoryBackendOps
from .backend_protocol_todos import MemoryBackendTodos
from .backend_protocol_temporal import MemoryBackendTemporal


@runtime_checkable
class MemoryBackend(
    MemoryBackendIngest,
    MemoryBackendRecall,
    MemoryBackendStructure,
    MemoryBackendConflicts,
    MemoryBackendOps,
    MemoryBackendTodos,
    MemoryBackendTemporal,
    Protocol,
):
    """Unified interface for all MCP tool/resource service access."""
