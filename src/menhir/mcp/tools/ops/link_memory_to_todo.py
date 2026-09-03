"""MCP tool: link_memory_to_todo."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope

_RELATIONS = ("mentions", "addresses")


async def link_memory_to_todo(
    memory_uuid: str, todo_uuid: str, relation: str, namespace: str = ""
) -> str:
    """Point a stored memory at a TODO.

    Direction is always inward: a memory references the todo, never the reverse.
    The todo stays an operational object -- knowledge lives in the memory.

    Args:
        memory_uuid: The memory doing the referencing. Must be a durable,
                     non-structural memory: a file does not "address" a todo.
        todo_uuid: The TODO being referenced.
        relation: `mentions` (the memory talks about this todo) or `addresses`
                  (the memory is work on this todo). Neither moves status --
                  closing is `resolve_todo`, which writes its own edge.

    Returns:
        Confirmation, or the reason it was refused.
    """
    return await LinkMemoryToTodoTool().execute(
        memory_uuid=memory_uuid,
        todo_uuid=todo_uuid,
        relation=relation,
        namespace=namespace,
    )


class LinkMemoryToTodoTool(BaseTextTool):
    name = "link_memory_to_todo"
    scope = ToolScope.NAMESPACED
    title = "Link Memory To TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Declare that a memory mentions or addresses a TODO."

    async def endpoint(
        self, memory_uuid: str, todo_uuid: str, relation: str, namespace: str = ""
    ) -> str:
        if relation not in _RELATIONS:
            return (
                f"Refused: unsupported relation {relation!r}. Use one of "
                f"{', '.join(_RELATIONS)}. Lifecycle relations are not reachable here -- "
                "use resolve_todo or reopen_todo, which move status and edge together."
            )

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

        result = await backend.link_memory_to_todo(memory_uuid, todo_uuid, relation)
        if result.get("linked"):
            return f"Memory {memory_uuid} now {relation} TODO {todo_uuid}"
        return (
            f"Refused: no link written ({result.get('reason', 'unknown')}). The memory must "
            "be a durable non-structural memory, and the todo must be in the memory's "
            "namespace or the shared default bucket."
        )
