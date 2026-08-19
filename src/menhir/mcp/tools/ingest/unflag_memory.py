"""MCP tool: unflag_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def unflag_memory(node_uuid: str, namespace: str = "") -> str:
    """Remove the permanent-retention flag from a memory node. The node will
    be subject to normal lifecycle decay unless re-flagged.

    Args:
        node_uuid: The UUID of the memory node to unflag. Get this from recall_memories results.
        namespace: Restrict the operation to a single silo. A pinned client has this forced.

    Returns:
        Confirmation or failure message.
    """

    return await UnflagMemoryTool().execute(node_uuid=node_uuid, namespace=namespace)


class UnflagMemoryTool(BaseTextTool):
    name = "unflag_memory"
    description = "Remove the permanent-retention flag from a memory node."

    async def endpoint(self, node_uuid: str, namespace: str = "") -> str:
        """Remove the permanent-retention flag from a memory node.

        Args:
            node_uuid: UUID of the memory node to unflag.
            namespace: Restrict the operation to a single silo. A pinned client has this
                forced -- and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.
        """
        backend = self.get_backend()
        # Ownership guard (ET-002). A UUID is an IDENTIFIER, not proof of tenancy: a pinned
        # caller that learns a foreign uuid through any global read path could otherwise mutate
        # another namespace's retention state here, because the persistence predicate matches on uuid alone.
        #
        # Two lookups, and the second is the point: refusing whenever the node is not found in
        # this namespace would also refuse when it does not exist at all, turning a plain
        # "not found" into a misleading namespace refusal. Only a node that demonstrably belongs
        # to another silo is refused.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                return (
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}."
                )
        try:
            unflagged = await backend.unflag_memory(node_uuid)
        except ValueError as exc:
            return f"Cannot unflag: {exc}"
        if unflagged:
            return f"Removed retention flag from memory {node_uuid}."
        return f"No memory found with uuid={node_uuid}."
