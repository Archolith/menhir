"""MCP tool: resolve_todo."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def resolve_todo(todo_uuid: str, memory_uuid: str, namespace: str = "") -> str:
    """Close a TODO and record the memory that resolved it, atomically.

    Use this instead of `close_todo` when a stored memory is the evidence that
    the work is done: the RESOLVES_TODO edge is what lets a closed todo answer
    "why did this close?" later. `close_todo` moves status and writes nothing.

    Args:
        todo_uuid: The TODO to close. Must be open -- an already-closed todo is
                   refused rather than re-closed, so the recorded resolution
                   stays the one that applied.
        memory_uuid: The memory that resolved it. Must be a durable memory in the
                     todo's namespace or the shared default bucket.

    Returns:
        Confirmation, or the reason it was refused.
    """
    return await ResolveTodoTool().execute(
        todo_uuid=todo_uuid, memory_uuid=memory_uuid, namespace=namespace
    )


class ResolveTodoTool(BaseTextTool):
    name = "resolve_todo"
    scope = ToolScope.NAMESPACED
    title = "Resolve TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Close a TODO and record the memory that resolved it, moving status and edge together."

    async def endpoint(
        self, todo_uuid: str, memory_uuid: str, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        # CF-33 step 4 on both objects, each through its own family's lookup.
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

        result = await backend.resolve_todo(todo_uuid, memory_uuid)
        if result.get("applied"):
            return f"TODO {todo_uuid} closed, resolved by memory {memory_uuid}"
        return (
            f"Refused: {todo_uuid} was not resolved. The todo must be open, and the memory "
            "must be a durable non-structural memory in the todo's namespace or the shared "
            "default bucket."
        )
