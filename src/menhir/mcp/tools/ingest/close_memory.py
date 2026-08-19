"""MCP tool: close_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def close_memory(uuid: str, namespace: str = "") -> str:
    """Mark a TEMPORAL memory as completed. Stops surfacing it in hook output.

    Once completed, the memory is suppressed from hook reminders and will be
    compressed by lifecycle shortly after the target_date passes.

    Args:
        uuid: UUID of the TEMPORAL memory node to complete.
        namespace: Restrict the operation to a single silo. A pinned client has this forced.

    Returns:
        Confirmation or not-found message.
    """
    return await CloseMemoryTool().execute(uuid=uuid, namespace=namespace)


class CloseMemoryTool(BaseTextTool):
    name = "close_memory"
    required_tier = "operator"
    description = "Mark a TEMPORAL memory as completed."

    async def endpoint(self, uuid: str, namespace: str = "") -> str:
        """Mark a TEMPORAL memory as completed.

        Args:
            uuid: UUID of the TEMPORAL memory node to complete.
            namespace: Restrict the operation to a single silo. A pinned client has this
                forced -- and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.
        """
        backend = self.get_backend()
        # Ownership guard (ET-002). A UUID is an IDENTIFIER, not proof of tenancy: a pinned
        # caller that learns a foreign uuid through any global read path could otherwise mutate
        # another namespace's temporal memory here, because the persistence predicate matches on uuid alone.
        #
        # Two lookups, and the second is the point: refusing whenever the node is not found in
        # this namespace would also refuse when it does not exist at all, turning a plain
        # "not found" into a misleading namespace refusal. Only a node that demonstrably belongs
        # to another silo is refused.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(uuid) is not None:
                return (
                    f"Refused: memory {uuid} exists but is outside namespace {ns}."
                )
        ok = await backend.complete_temporal(uuid)
        if ok:
            return f"Completed memory {uuid}."
        return f"Memory {uuid} not found or already completed."
