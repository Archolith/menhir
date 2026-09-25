"""TODO delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphTodosMixin:
    """TODO delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # TODO delegates → TodoRepository
    # -------------------------------------------------------------------------

    def create_todo(
        self,
        *,
        content: str,
        code_ref: str | None = None,
        priority: str = "normal",
        source: str = "claude-code",
        episode_uuid: str | None = None,
        structure_project: str | None = None,
        due_date: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return self._todos.create_todo(
            content=content,
            code_ref=code_ref,
            priority=priority,
            source=source,
            episode_uuid=episode_uuid,
            structure_project=structure_project,
            due_date=due_date,
            namespace=namespace,
        )

    def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return self._todos.list_todos(status=status, limit=limit, namespace=namespace)

    def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        return self._todos.get_todo(uuid, namespace=namespace)

    def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        return self._todos.supersede_todo(old_uuid, new_uuid)

    def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return self._todos.resolve_todo(todo_uuid, memory_uuid)

    def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return self._todos.reopen_todo(todo_uuid, memory_uuid)

    def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        return self._todos.link_memory_to_todo(memory_uuid, todo_uuid, relation)

    def close_todo(self, uuid: str) -> bool:
        return self._todos.close_todo(uuid)

    def delete_todo(self, uuid: str) -> bool:
        return self._todos.delete_todo(uuid)

    def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        return self._todos.close_stale_todos(
            older_than_days=older_than_days, dry_run=dry_run, namespace=namespace
        )

    def list_todos_matching_query(
        self, query: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        return self._todos.search_by_query(query, limit=limit)
