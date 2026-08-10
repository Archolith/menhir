"""MCP tool: promote_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def promote_memory(node_uuid: str) -> str:
    """Promote a memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).

    Distinct from flag_memory (marks a memory as important to the user, but still
    an ordinary claim): PROMOTED means "this is verified and cannot be false." A
    promoted node is immune to being merged into/out of another identity during
    correlation, its confidence is pinned at 1.0, and conflicting future claims are
    routed to manual review rather than treated as an ordinary symmetric conflict.

    Only a PERSISTENT memory can be promoted (SESSION/CANDIDATE have not earned
    durability yet). Idempotent: promoting an already-promoted memory succeeds.

    Args:
        node_uuid: The UUID of the memory node to promote. Must currently be
            PERSISTENT scope. Get this from recall_memories results.

    Returns:
        Confirmation or failure message.
    """

    return await PromoteMemoryTool().execute(node_uuid=node_uuid)


class PromoteMemoryTool(BaseTextTool):
    name = "promote_memory"
    required_tier = "operator"
    description = "Promote a PERSISTENT memory to PROMOTED (operator-curated, verified ground truth)."

    async def endpoint(self, node_uuid: str) -> str:
        """Promote a memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).

        Args:
            node_uuid: The UUID of the memory node to promote. Must currently be
                PERSISTENT scope. Get this from recall_memories results.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        promoted = await backend.promote_memory(node_uuid)
        if promoted:
            return f"Promoted memory {node_uuid} to PROMOTED (verified ground truth)."
        return f"No PERSISTENT memory found with uuid={node_uuid}."
