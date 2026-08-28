"""MCP tool: close_todo."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def close_todo(uuid: str, namespace: str = "") -> str:
    """Mark a TODO item as closed.

    Args:
        uuid: The UUID of the TODO to close (from add_todo or list_todos output).

    Returns:
        Confirmation message.
    """
    return await CloseTodoTool().execute(uuid=uuid, namespace=namespace)


class CloseTodoTool(BaseTextTool):
    name = "close_todo"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): a uuid is not proof of
    # ownership, so the object the caller names is checked against the pin at load.
    scope = ToolScope.NAMESPACED
    title = "Close TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Mark a TODO item as closed."

    async def endpoint(self, uuid: str, namespace: str = "") -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load. `close_stale_todos` is already namespace-scoped
        # (CF-217); closing ONE todo by uuid was not, so the bulk path was bounded and the
        # single path was not.
        refusal = await foreign_object_refusal(
            uuid=uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch before.
            lookup=lambda todo_uuid, **kw: backend.get_todo(todo_uuid, **kw),
            label="todo",
        )
        if refusal:
            return refusal
        closed = await backend.close_todo(uuid)
        if closed:
            return f"Closed TODO {uuid}"
        return f"TODO {uuid} not found or already closed"
