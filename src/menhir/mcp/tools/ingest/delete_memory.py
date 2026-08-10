"""MCP tool: delete_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def delete_memory(node_uuid: str) -> str:
    """Delete a specific memory node and all its relationships.

    Args:
        node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.

    Returns:
        Confirmation or failure message.
    """

    return await DeleteMemoryTool().execute(node_uuid=node_uuid)


class DeleteMemoryTool(BaseTextTool):
    name = "delete_memory"
    required_tier = "operator"
    description = "Delete a specific memory node and all its relationships."

    async def endpoint(self, node_uuid: str) -> str:
        """Delete a specific memory node and all its relationships.

        Args:
            node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        deleted = await backend.delete_memory(node_uuid)
        if deleted:
            return f"Deleted memory {node_uuid} and all its relationships."
        return f"No memory found with uuid={node_uuid}."
