"""Todo CRUD and per-caller ownership-guarded todo operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from menhir.core.tenancy import require_own_object

from .backend_shared import _to_jsonable


class RuntimeTodoOpsMixin:
    """Todo CRUD and per-caller ownership-guarded todo operations for the in-process backend adapter."""

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
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.create_todo,
                content=content,
                code_ref=code_ref,
                priority=priority,
                source=source,
                episode_uuid=episode_uuid,
                structure_project=structure_project,
                due_date=due_date,
                namespace=namespace,
            )
        )

    async def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_todos,
                status=status,
                limit=limit,
                namespace=namespace,
            )
        )

    async def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.get_todo, uuid, namespace=namespace
            )
        )

    async def close_todo(self, uuid: str) -> bool:
        return bool(await self._off_loop(self.built.graph_adapter.close_todo, uuid))

    async def _require_own_todo(self, todo_uuid: str) -> None:
        """Refuse a pinned caller that named another silo's todo.

        Here rather than only in the MCP tools for the reason `_require_own_memory`
        states: these ops are reachable from the generic dispatch at
        `/api/internal/backend/{operation}` as well, and that path injects a namespace
        only into methods whose signature declares one. None of these do, so a
        tool-only guard leaves the dispatch surface unguarded -- the exact
        per-caller-fix pattern that cluster has already produced four times.

        The in-query namespace rule is not a substitute: it is RELATIVE (it stops
        linking ACROSS silos) and is equally satisfied by two todos that both belong
        to someone else.
        """
        await require_own_object(
            uuid=todo_uuid,
            lookup=lambda uuid, **kw: self.get_todo(uuid, **kw),
            label="todo",
        )

    async def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        await self._require_own_todo(old_uuid)
        await self._require_own_todo(new_uuid)
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.supersede_todo, old_uuid, new_uuid
            )
        )

    async def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        await self._require_own_todo(todo_uuid)
        await self._require_own_memory(memory_uuid)
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.resolve_todo, todo_uuid, memory_uuid
            )
        )

    async def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        await self._require_own_todo(todo_uuid)
        await self._require_own_memory(memory_uuid)
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.reopen_todo, todo_uuid, memory_uuid
            )
        )

    async def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        await self._require_own_todo(todo_uuid)
        await self._require_own_memory(memory_uuid)
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.link_memory_to_todo,
                memory_uuid,
                todo_uuid,
                relation,
            )
        )

    async def delete_todo(self, uuid: str) -> bool:
        return bool(await self._off_loop(self.built.graph_adapter.delete_todo, uuid))

    async def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.close_stale_todos,
                older_than_days=older_than_days,
                dry_run=dry_run,
                namespace=namespace,
            )
        )
