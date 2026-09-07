"""MCP tool: supersede_todo."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def supersede_todo(old_uuid: str, new_uuid: str, namespace: str = "") -> str:
    """Close one TODO and record the TODO that replaced it, atomically.

    Menhir has no update path: editing a todo means closing it and adding a
    replacement. Use this instead of `close_todo` whenever the new todo IS the
    edited version of the old one, so the refile lineage survives as an edge
    rather than as prose in whatever memory happened to describe it.

    Writes SUPERSEDED_BY and closes the old todo in a single statement: an edge
    pointing at a still-open todo, or a closed todo with no record of what
    replaced it, are both states the graph must never hold.

    Args:
        old_uuid: The TODO being replaced. Must be open and not already superseded.
        new_uuid: The replacement TODO. Must be open, and in the old todo's
                  namespace or the shared default bucket.

    Returns:
        Confirmation, or the reason it was refused.
    """
    return await SupersedeTodoTool().execute(
        old_uuid=old_uuid, new_uuid=new_uuid, namespace=namespace
    )


class SupersedeTodoTool(BaseTextTool):
    name = "supersede_todo"
    # NAMESPACED per CF-33 step 4, and the guard runs on BOTH uuids: the namespace rule in
    # the query is RELATIVE (it stops a cross-silo link) and is equally satisfied by two
    # todos that both live in someone else's silo. This is the absolute check.
    scope = ToolScope.NAMESPACED
    title = "Supersede TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Close a TODO and record the TODO that replaced it, moving status and edge together."

    async def endpoint(
        self, old_uuid: str, new_uuid: str, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        for uuid in (old_uuid, new_uuid):
            refusal = await foreign_object_refusal(
                uuid=uuid,
                namespace=namespace,
                # Resolved lazily so an UNPINNED call touches nothing it did not touch
                # before: `backend.get_todo` is not looked up unless a namespace is set.
                lookup=lambda todo_uuid, **kw: backend.get_todo(todo_uuid, **kw),
                label="todo",
            )
            if refusal:
                return refusal

        result = await backend.supersede_todo(old_uuid, new_uuid)
        if result.get("applied"):
            return f"TODO {old_uuid} is now closed, superseded by {new_uuid}"
        return (
            f"Refused: {old_uuid} was not superseded ({result.get('reason', 'unknown')}). "
            "The old todo must be open and have no successor already, the new todo must "
            "exist and be open, they must not be the same todo, and the new todo must be "
            "in the old one's namespace or the shared default bucket."
        )
