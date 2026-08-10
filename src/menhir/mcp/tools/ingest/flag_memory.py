"""MCP tool: flag_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def flag_memory(node_uuid: str, bootstrap_scope: str = "") -> str:
    """Flag a memory node for permanent retention. Flagged nodes survive lifecycle decay.

    Args:
        node_uuid: The UUID of the memory node to flag. Get this from recall_memories results.
        bootstrap_scope: Optional startup pin: general, workspace:<key>, or none.

    Returns:
        Confirmation or failure message.
    """

    return await FlagMemoryTool().execute(
        node_uuid=node_uuid, bootstrap_scope=bootstrap_scope
    )


class FlagMemoryTool(BaseTextTool):
    name = "flag_memory"
    description = "Flag a memory node for permanent retention."

    async def endpoint(self, node_uuid: str, bootstrap_scope: str = "") -> str:
        """Flag a memory node for permanent retention. Flagged nodes survive lifecycle decay.

        Args:
            node_uuid: The UUID of the memory node to flag. Get this from recall_memories results.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        try:
            scope_arg = bootstrap_scope if bootstrap_scope.strip() else None
            flagged = await backend.flag_memory(
                node_uuid, bootstrap_scope=scope_arg
            )
        except ValueError as exc:
            return f"Cannot flag: {exc}"
        if flagged:
            row = await backend.fetch_memory_by_uuid(node_uuid)
            effective_scope = (row or {}).get("bootstrap_scope")
            return (
                f"Flagged memory {node_uuid} for permanent retention; "
                f"bootstrap_scope={effective_scope or 'none'}."
            )
        return f"No memory found with uuid={node_uuid}."
