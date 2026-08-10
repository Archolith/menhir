"""MCP tool: unflag_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def unflag_memory(node_uuid: str) -> str:
    """Remove the permanent-retention flag from a memory node. The node will
    be subject to normal lifecycle decay unless re-flagged.

    Args:
        node_uuid: The UUID of the memory node to unflag. Get this from recall_memories results.

    Returns:
        Confirmation or failure message.
    """

    return await UnflagMemoryTool().execute(node_uuid=node_uuid)


class UnflagMemoryTool(BaseTextTool):
    name = "unflag_memory"
    description = "Remove the permanent-retention flag from a memory node."

    async def endpoint(self, node_uuid: str) -> str:
        """Remove the permanent-retention flag from a memory node."""
        backend = self.get_backend()
        try:
            unflagged = await backend.unflag_memory(node_uuid)
        except ValueError as exc:
            return f"Cannot unflag: {exc}"
        if unflagged:
            return f"Removed retention flag from memory {node_uuid}."
        return f"No memory found with uuid={node_uuid}."
