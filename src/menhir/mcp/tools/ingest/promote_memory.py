"""MCP tool: promote_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def promote_memory(node_uuid: str, namespace: str = "") -> str:
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

    return await PromoteMemoryTool().execute(node_uuid=node_uuid, namespace=namespace)


class PromoteMemoryTool(BaseTextTool):
    name = "promote_memory"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Promote Memory"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Promote a PERSISTENT memory to PROMOTED (operator-curated, verified ground truth)."

    async def endpoint(self, node_uuid: str, namespace: str = "") -> str:
        """Promote a memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).

        Args:
            node_uuid: The UUID of the memory node to promote. Must currently be
                PERSISTENT scope. Get this from recall_memories results.
            namespace: Restrict the operation to a single silo. A pinned client has this
                forced -- and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        # Ownership guard (ET-002). A UUID is an IDENTIFIER, not proof of tenancy: a pinned
        # caller that learns a foreign uuid through any global read path could otherwise pin
        # another namespace's node at confidence 1.0 and make it merge-immune, because the
        # persistence predicate matches on uuid alone.
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
        promoted = await backend.promote_memory(node_uuid)
        if promoted:
            return f"Promoted memory {node_uuid} to PROMOTED (verified ground truth)."
        return f"No PERSISTENT memory found with uuid={node_uuid}."
