"""Memory and recall operation methods for the HTTP-backed backend adapter.

Extracted from ``backend_client_ops``: episode queueing, the flag/promote/delete memory
lifecycle, recall and context building, and the memory fetch/read methods. Reached
through ``BackendClientOpsMixin``, which composes this mixin.
"""

from __future__ import annotations

from typing import Any


class BackendClientMemoryOpsMixin:
    """Memory and recall operations for the HTTP-backed backend adapter."""

    async def queue_episode(
        self,
        text: str,
        *,
        user_id: str,
        session_id: str,
        source: str = "mcp",
        diff: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        occurred_at: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "queue_episode",
            {
                "text": text,
                "user_id": user_id,
                "session_id": session_id,
                "source": source,
                "diff": diff,
                "flagged": flagged,
                "bootstrap_scope": bootstrap_scope,
                "namespace": namespace,
                "occurred_at": occurred_at,
                "turn_evidence_uuid": turn_evidence_uuid,
            },
        )

    async def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        return bool(
            await self._request(
                "flag_memory",
                {"node_uuid": node_uuid, "bootstrap_scope": bootstrap_scope},
            )
        )

    async def unflag_memory(self, node_uuid: str) -> bool:
        return bool(await self._request("unflag_memory", {"node_uuid": node_uuid}))

    async def promote_memory(self, node_uuid: str) -> bool:
        return bool(await self._request("promote_memory", {"node_uuid": node_uuid}))

    async def delete_memory(self, node_uuid: str) -> bool:
        return bool(await self._request("delete_memory", {"node_uuid": node_uuid}))

    async def erase_memory(self, node_uuid: str) -> dict:
        return dict(await self._request("erase_memory", {"node_uuid": node_uuid}) or {})

    async def delete_namespace(
        self,
        namespace: str,
        *,
        max_nodes: int = 200,
        force: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return await self._request(
            "delete_namespace",
            {
                "namespace": namespace,
                "max_nodes": max_nodes,
                "force": force,
                "dry_run": dry_run,
            },
        )

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self._request(
                "enqueue_pending_episode", {"episode_uuid": episode_uuid}
            )
        )

    async def recall(
        self,
        query: str,
        *,
        preset: str = "knowledge",
        limit: int = 10,
        include_session: bool = False,
        include_superseded: bool = False,
        include_invalidated: bool = False,
        trace: bool = False,
        wait_for_pending: bool = False,
        file_context: str | None = None,
        file_context_project: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "recall",
            {
                "query": query,
                "preset": preset,
                "limit": limit,
                "include_session": include_session,
                "include_superseded": include_superseded,
                "include_invalidated": include_invalidated,
                "wait_for_pending": wait_for_pending,
                "file_context": file_context,
                "file_context_project": file_context_project,
                "namespace": namespace,
                "trace": trace,
            },
        )

    async def view_entropy(
        self,
        *,
        namespace: str | None = None,
        kind: str | None = None,
        top_k: int = 20,
        max_views: int = 50,
    ) -> dict[str, Any]:
        return await self._request(
            "view_entropy",
            {
                "namespace": namespace,
                "kind": kind,
                "top_k": top_k,
                "max_views": max_views,
            },
        )

    async def build_context(
        self,
        query: str,
        *,
        max_tokens: int = 4000,
        preset: str = "knowledge",
        session_id: str | None = None,
        include_scores: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "build_context",
            {
                "query": query,
                "max_tokens": max_tokens,
                "preset": preset,
                "session_id": session_id,
                "include_scores": include_scores,
                "namespace": namespace,
            },
        )

    async def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        return await self._request(
            "fetch_memory_by_uuid", {"node_uuid": node_uuid, "namespace": namespace}
        )

    async def fetch_node_receipts(self, node_uuid: str) -> dict[str, Any] | None:
        return await self._request("fetch_node_receipts", {"node_uuid": node_uuid})

    async def fetch_recent_memories(
        self, limit: int = 20, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_recent_memories", {"limit": limit, "namespace": namespace}
        )

    async def fetch_flagged_memories(
        self,
        limit: int = 50,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_flagged_memories",
            {"limit": limit, "workspace": workspace, "namespace": namespace},
        )

    async def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> str:
        return str(
            await self._request(
                "fetch_flagged_memory_bootstrap_version",
                {"workspace": workspace, "namespace": namespace},
            )
        )

    async def fetch_memories_by_scope(
        self, scope: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_memories_by_scope",
            {"scope": scope, "limit": limit, "namespace": namespace},
        )

    async def fetch_memories_by_type(
        self, memory_type: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_memories_by_type",
            {"memory_type": memory_type, "limit": limit, "namespace": namespace},
        )

    async def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        return await self._request("fetch_memory_overview", {"namespace": namespace})
