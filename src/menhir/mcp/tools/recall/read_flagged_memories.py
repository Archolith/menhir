"""MCP tool: read_flagged_memories."""

from __future__ import annotations

from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.structural_memory import is_structural_memory_row
from menhir.mcp.formatters import _compact_memory_item, _normalize_reader_id
from menhir.mcp.lifecycle import _remember_flagged_bootstrap_read
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def read_flagged_memories(
    reader_id: str = "default", limit: int = 10, workspace: str = "", namespace: str = ""
) -> str:
    """Read flagged memories for startup bootstrap.

    Bots should call this first before requesting broader context.

    Args:
        reader_id: Stable bot/client identifier used for bootstrap gating.
        limit: Max flagged memories to return (default: 10, max: 50).
        workspace: Registered workspace key. Empty selects general pins only.
        namespace: Restrict the read to a single namespace. A pinned client has
            it forced.

    Returns:
        Flagged memory list and bootstrap state for the reader.
    """

    return await ReadFlaggedMemoriesTool().execute(
        reader_id=reader_id, limit=limit, workspace=workspace, namespace=namespace
    )


class ReadFlaggedMemoriesTool(BaseJsonTool):
    name = "read_flagged_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Read Flagged Memories"
    oauth_scopes = ("menhir:read",)
    # Not purely read-only: this endpoint records a durable bootstrap receipt
    # (_remember_flagged_bootstrap_read) that gates recall_context_memories.
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Read flagged memories for startup bootstrap."

    async def endpoint(
        self,
        reader_id: str = "default",
        limit: int = 10,
        workspace: str = "",
        namespace: str = "",
    ) -> str:
        """Read flagged memories for startup bootstrap.

        Bots should call this first before requesting broader context.

        Args:
            namespace: Restrict the read to a single namespace. A pinned client
                has it forced.
        """
        backend = self.get_backend()
        normalized_reader_id = _normalize_reader_id(reader_id)
        # CF-238: normalized once here so the record side and `recall_context_memories`
        # derive the receipt key from byte-identical values.
        ws = workspace.strip() or None
        ns = namespace.strip() or None
        selection_key, _ = bootstrap_selection(ws)
        rows = await backend.fetch_flagged_memories(
            limit=limit, workspace=ws, namespace=ns
        )
        rows = [row for row in rows if not is_structural_memory_row(row)]
        flagged_version = await backend.fetch_flagged_memory_bootstrap_version(
            workspace=ws, namespace=ns
        )
        # CF-238: the receipt is keyed on the RAW workspace and the namespace, separately.
        # `recall_context_memories` must build its key from the same two values.
        _remember_flagged_bootstrap_read(
            normalized_reader_id, flagged_version, workspace=ws, namespace=ns
        )
        return self.render_recall_json(
            {
                "reader_id": normalized_reader_id,
                "bootstrap_selection": selection_key,
                "bootstrap_ready": True,
                "flagged_version": flagged_version,
                "flagged_count": len(rows),
                "items": [_compact_memory_item(row, tag="flagged") for row in rows],
            }
        )
