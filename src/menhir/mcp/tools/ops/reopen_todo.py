"""MCP tool: reopen_todo."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def reopen_todo(todo_uuid: str, memory_uuid: str, namespace: str = "") -> str:
    """Reopen a closed TODO and record the memory that reopened it, atomically.

    Clears `closed_at` and returns any linked reminder to open, mirroring what
    closing did. The REOPENS_TODO edge records why it came back.

    Args:
        todo_uuid: The TODO to reopen. Must be closed.
        memory_uuid: The memory that reopened it. Must be a durable memory in the
                     todo's namespace or the shared default bucket.

    Returns:
        Confirmation, or the reason it was refused.
    """
    return await ReopenTodoTool().execute(
        todo_uuid=todo_uuid, memory_uuid=memory_uuid, namespace=namespace
    )


class ReopenTodoTool(BaseTextTool):
    name = "reopen_todo"
    scope = ToolScope.NAMESPACED
    title = "Reopen TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Reopen a closed TODO and record the memory that reopened it."

    async def endpoint(
        self, todo_uuid: str, memory_uuid: str, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        refusal = await foreign_object_refusal(
            uuid=todo_uuid,
            namespace=namespace,
            lookup=lambda uuid, **kw: backend.get_todo(uuid, **kw),
            label="todo",
        )
        if refusal:
            return refusal
        refusal = await foreign_object_refusal(
            uuid=memory_uuid,
            namespace=namespace,
            lookup=lambda uuid, **kw: backend.fetch_memory_by_uuid(uuid, **kw),
            label="memory",
        )
        if refusal:
            return refusal

        result = await backend.reopen_todo(todo_uuid, memory_uuid)
        if result.get("applied"):
            return f"TODO {todo_uuid} reopened, per memory {memory_uuid}"
        return (
            f"Refused: {todo_uuid} was not reopened. The todo must be closed, and the memory "
            "must be a durable non-structural memory in the todo's namespace or the shared "
            "default bucket."
        )
