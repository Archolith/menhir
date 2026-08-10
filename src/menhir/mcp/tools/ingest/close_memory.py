"""MCP tool: close_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def close_memory(uuid: str) -> str:
    """Mark a TEMPORAL memory as completed. Stops surfacing it in hook output.

    Once completed, the memory is suppressed from hook reminders and will be
    compressed by lifecycle shortly after the target_date passes.

    Args:
        uuid: UUID of the TEMPORAL memory node to complete.

    Returns:
        Confirmation or not-found message.
    """
    return await CloseMemoryTool().execute(uuid=uuid)


class CloseMemoryTool(BaseTextTool):
    name = "close_memory"
    required_tier = "operator"
    description = "Mark a TEMPORAL memory as completed."

    async def endpoint(self, uuid: str) -> str:
        backend = self.get_backend()
        ok = await backend.complete_temporal(uuid)
        if ok:
            return f"Completed memory {uuid}."
        return f"Memory {uuid} not found or already completed."
