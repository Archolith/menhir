"""Graph fetch operations for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: single-node reads, flagged/recent/scope listings,
and the scan fingerprint lookup. Reached through ``RuntimeProviderDataOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeFetchDataOpsMixin:
    """Single-node and listing graph reads for the in-process backend adapter."""

    async def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memory_by_uuid, node_uuid, **kwargs
            )
        )

    async def fetch_node_receipts(self, node_uuid: str) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_node_receipts, node_uuid
            )
        )

    async def fetch_recent_memories(
        self, limit: int = 20, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_recent_memories, **kwargs)
        )

    async def fetch_flagged_memories(
        self,
        limit: int = 50,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if workspace is not None:
            kwargs["workspace"] = workspace
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_flagged_memories, **kwargs)
        )

    async def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {}
        if workspace is not None:
            kwargs["workspace"] = workspace
        if namespace is not None:
            kwargs["namespace"] = namespace
        return str(
            await self._off_loop(
                self.built.graph_adapter.fetch_flagged_memory_bootstrap_version, **kwargs
            )
        )

    async def fetch_memories_by_scope(
        self, scope: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memories_by_scope, scope, **kwargs
            )
        )

    async def fetch_memories_by_type(
        self, memory_type: str, limit: int = 20, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"limit": limit}
        if namespace is not None:
            kwargs["namespace"] = namespace
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_memories_by_type,
                memory_type,
                **kwargs,
            )
        )

    async def get_scan_fingerprint(self, project_name: str) -> str | None:
        return await self._off_loop(
            self.built.graph_adapter.get_scan_fingerprint, project_name
        )
