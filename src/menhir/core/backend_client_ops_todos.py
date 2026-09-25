"""Todo and temporal operation methods for the HTTP-backed backend adapter.

Extracted from ``backend_client_ops``: todo CRUD and lifecycle, memory-to-todo links,
stale-todo cleanup, and temporal reminders. Reached through ``BackendClientOpsMixin``,
which composes this mixin.
"""

from __future__ import annotations

from typing import Any


class BackendClientTodosOpsMixin:
    """Todo and temporal reminder operations for the HTTP-backed backend adapter."""

    async def create_todo(
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
        return await self._request(
            "create_todo",
            {
                "content": content,
                "code_ref": code_ref,
                "priority": priority,
                "source": source,
                "episode_uuid": episode_uuid,
                "structure_project": structure_project,
                "due_date": due_date,
                "namespace": namespace,
            },
        )

    async def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_todos", {"status": status, "limit": limit, "namespace": namespace}
        )

    async def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        return await self._request("get_todo", {"uuid": uuid, "namespace": namespace})

    async def close_todo(self, uuid: str) -> bool:
        return bool(await self._request("close_todo", {"uuid": uuid}))

    async def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        return await self._request(
            "supersede_todo", {"old_uuid": old_uuid, "new_uuid": new_uuid}
        )

    async def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return await self._request(
            "resolve_todo", {"todo_uuid": todo_uuid, "memory_uuid": memory_uuid}
        )

    async def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return await self._request(
            "reopen_todo", {"todo_uuid": todo_uuid, "memory_uuid": memory_uuid}
        )

    async def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        return await self._request(
            "link_memory_to_todo",
            {"memory_uuid": memory_uuid, "todo_uuid": todo_uuid, "relation": relation},
        )

    async def delete_todo(self, uuid: str) -> bool:
        return bool(await self._request("delete_todo", {"uuid": uuid}))

    async def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "close_stale_todos",
            {"older_than_days": older_than_days, "dry_run": dry_run, "namespace": namespace},
        )

    async def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "create_temporal",
            {
                "content": content,
                "target_date": target_date,
                "source": source,
                "name": name,
                "flagged": flagged,
                "bootstrap_scope": bootstrap_scope,
                "namespace": namespace,
                "turn_evidence_uuid": turn_evidence_uuid,
            },
        )

    async def list_temporal_in_window(
        self, *, window_days: int = 30, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_temporal_in_window",
            {"window_days": window_days, "namespace": namespace},
        )

    async def complete_temporal(self, uuid: str) -> bool:
        return bool(await self._request("complete_temporal", {"uuid": uuid}))
