"""MCP tool: add_todo."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def add_todo(
    text: str,
    code_ref: str | None = None,
    priority: str = "normal",
    episode_uuid: str | None = None,
    structure_project: str | None = None,
    due_date: str | None = None,
    namespace: str = "",
) -> str:
    """Create a persistent TODO item that survives across sessions.

    TODOs are stored as :Todo nodes in Neo4j — they never decay, never go
    through enrichment, and appear at the top of every hook bootstrap.

    Graph edges are created automatically:
      - REFERENCES_FILE → structural file entity matching code_ref
      - CREATED_FROM    → episodic memory node if episode_uuid is provided
      - HAS_REMINDER    → TEMPORAL :Entity node (created when due_date is set)

    Args:
        text: The TODO description. Be specific and actionable.
        code_ref: Optional file path and line, e.g. "src/api/routes.py:42".
        priority: "low", "normal", or "high". Default: "normal".
        episode_uuid: UUID of an episodic memory that triggered this TODO.
        due_date: Optional ISO date (YYYY-MM-DD). Creates a linked TEMPORAL
                  reminder that surfaces in hook output ±30 days around this date.
        namespace: Optional silo to scope this TODO to. Empty = the shared
                   "default" silo. A stored TODO always carries a namespace.

    Returns:
        Confirmation with the UUID and any auto-linked graph nodes.
    """
    return await AddTodoTool().execute(
        text=text, code_ref=code_ref, priority=priority, episode_uuid=episode_uuid,
        structure_project=structure_project, due_date=due_date, namespace=namespace,
    )


class AddTodoTool(BaseTextTool):
    name = "add_todo"
    scope = ToolScope.NAMESPACED
    title = "Add TODO"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Create a persistent TODO item that survives across sessions."

    async def endpoint(
        self,
        text: str,
        code_ref: str | None = None,
        priority: str = "normal",
        episode_uuid: str | None = None,
        structure_project: str | None = None,
        due_date: str | None = None,
        namespace: str = "",
    ) -> str:
        backend = self.get_backend()
        todo = await backend.create_todo(
            content=text,
            code_ref=code_ref,
            priority=priority,
            episode_uuid=episode_uuid,
            structure_project=structure_project,
            due_date=due_date,
            namespace=namespace or None,
        )
        uuid = todo.get("uuid", "?")
        ref = f"  {todo['code_ref']}" if todo.get("code_ref") else ""
        tag = (todo.get("priority") or "normal").upper()
        snippet = (text[:80] + "...") if len(text) > 80 else text

        parts = [f"Created TODO uuid={uuid} [{tag}]{ref} — {snippet}"]

        if todo.get("linked_file_path"):
            parts.append(f"  → linked file: {todo['linked_file_path']}")
        if todo.get("episode_uuid"):
            parts.append(f"  → provenance: episode {todo['episode_uuid']}")
        if todo.get("reminder_uuid"):
            parts.append(f"  → reminder: TEMPORAL {todo['reminder_uuid']} due {due_date}")

        return "\n".join(parts)
