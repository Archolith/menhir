"""MCP tool: delete_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def delete_memory(node_uuid: str, namespace: str = "") -> str:
    """Delete a specific memory node and all its relationships.

    Args:
        node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.
        namespace: Restrict the operation to a single silo. A pinned client has this forced.

    Returns:
        Confirmation or failure message.
    """

    return await DeleteMemoryTool().execute(node_uuid=node_uuid, namespace=namespace)


class DeleteMemoryTool(BaseTextTool):
    name = "delete_memory"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    description = "Delete a specific memory node and all its relationships."

    async def endpoint(self, node_uuid: str, namespace: str = "") -> str:
        """Delete a specific memory node and all its relationships.

        Args:
            node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.
            namespace: Restrict the operation to a single silo. A pinned client has this forced.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        # Ownership guard. Without the `namespace` parameter above, the pin cannot reach
        # this tool at all: _apply_pinned_namespace injects only into endpoints whose
        # signature names it, so a pinned client could erase any uuid it had learned.
        #
        # Two lookups, not one, and the second is the point. Refusing whenever the node is
        # not found IN this namespace would also refuse when it is not in the graph AT ALL
        # -- and that is a supported erasure path, not an error: `graph_already_absent` is
        # how a merge leaves the node it absorbed, whose stored content must still be
        # erasable. So absent-from-graph proceeds, and only a node that demonstrably
        # belongs to another silo is refused.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                return (
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}."
                )
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
