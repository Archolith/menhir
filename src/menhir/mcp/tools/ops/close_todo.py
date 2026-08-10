"""MCP tool: close_todo."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def close_todo(uuid: str) -> str:
    """Mark a TODO item as closed.

    Args:
        uuid: The UUID of the TODO to close (from add_todo or list_todos output).

    Returns:
        Confirmation message.
    """
    return await CloseTodoTool().execute(uuid=uuid)


class CloseTodoTool(BaseTextTool):
    name = "close_todo"
    description = "Mark a TODO item as closed."

    async def endpoint(self, uuid: str) -> str:
        backend = self.get_backend()
        closed = await backend.close_todo(uuid)
        if closed:
            return f"Closed TODO {uuid}"
        return f"TODO {uuid} not found or already closed"
