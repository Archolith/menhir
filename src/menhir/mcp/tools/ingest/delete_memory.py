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
        outcome = await backend.erase_memory(node_uuid)
        reason = outcome.get("reason")
        purged = sum((outcome.get("purged") or {}).values())

        if reason == "nothing_to_erase":
            return f"No memory found with uuid={node_uuid}, and no stored content for it."
        if reason == "graph_already_absent":
            # Not "not found": the node was gone from the graph -- which is how a merge leaves
            # the node it absorbed -- but its stored content was still there and is now erased.
            return (
                f"Memory {node_uuid} was already absent from the graph, but {purged} stored "
                "record(s) still held its content. Those are now erased."
            )
        if reason == "residual_content_after_purge":
            return (
                f"Erasure of {node_uuid} did NOT complete: content remains after the purge and "
                "the operation is quarantined for review."
            )
        message = (
            f"Erased memory {node_uuid}: the graph node, all its relationships, and {purged} "
            "stored record(s) of its content."
        )
        unaddressable = outcome.get("unaddressable") or []
        if unaddressable:
            # Never let an erasure claim a completeness it does not have.
            message += (
                f" NOTE: {len(unaddressable)} content location(s) could not be addressed and "
                "were left untouched."
            )
        return message
