"""MCP tool: read_flagged_memories."""

from __future__ import annotations

from menhir.domain.bootstrap_scope import bootstrap_selection
from menhir.domain.structural_memory import is_structural_memory_row
from menhir.mcp.formatters import _compact_memory_item, _normalize_reader_id
from menhir.mcp.lifecycle import _remember_flagged_bootstrap_read
from menhir.mcp.tools.base import BaseJsonTool


async def read_flagged_memories(
    reader_id: str = "default", limit: int = 10, workspace: str = ""
) -> str:
    """Read flagged memories for startup bootstrap.

    Bots should call this first before requesting broader context.

    Args:
        reader_id: Stable bot/client identifier used for bootstrap gating.
        limit: Max flagged memories to return (default: 10, max: 50).
        workspace: Registered workspace key. Empty selects general pins only.

    Returns:
        Flagged memory list and bootstrap state for the reader.
    """

    return await ReadFlaggedMemoriesTool().execute(
        reader_id=reader_id, limit=limit, workspace=workspace
    )


class ReadFlaggedMemoriesTool(BaseJsonTool):
    name = "read_flagged_memories"
    required_tier = "readonly"
    description = "Read flagged memories for startup bootstrap."

    async def endpoint(
        self, reader_id: str = "default", limit: int = 10, workspace: str = ""
    ) -> str:
        """Read flagged memories for startup bootstrap.

        Bots should call this first before requesting broader context.
        """
        backend = self.get_backend()
        normalized_reader_id = _normalize_reader_id(reader_id)
        selection_key, _ = bootstrap_selection(workspace)
        rows = await backend.fetch_flagged_memories(
            limit=limit, workspace=workspace or None
        )
        rows = [row for row in rows if not is_structural_memory_row(row)]
        flagged_version = await backend.fetch_flagged_memory_bootstrap_version(
            workspace=workspace or None
        )
        _remember_flagged_bootstrap_read(
            normalized_reader_id, flagged_version, workspace=workspace or None
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
