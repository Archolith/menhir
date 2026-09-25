"""Structure graph queries for the in-process backend adapter.

Extracted from ``backend_runtime_data_ops``: the dispatch side of `query_structure`, answered in
the backend before the adapter's own fallthrough. Reached through ``RuntimeProviderDataOpsMixin``.
"""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeStructureDataOpsMixin:
    """Structure graph queries for the in-process backend adapter."""

    async def query_structure(
        self, project: str, query_type: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        if query_type == "projects":
            return _to_jsonable(
                await self._off_loop(self.built.graph_adapter.list_structure_projects)
            )
        if query_type == "orphan_structure_projects":
            return _to_jsonable(
                await self._off_loop(self.built.graph_adapter.list_orphan_structure_projects)
            )
        if query_type == "documents":
            # params: optional path_filter (via "path"), document_type (via "doc_type")
            path_filter = params.get("path", "") if params else ""
            doc_type = params.get("doc_type") if params else None
            return _to_jsonable(
                await self._off_loop(
                    self.built.graph_adapter.query_documents,
                    project,
                    path_filter=path_filter,
                    document_type=doc_type,
                )
            )
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.query_structure,
                project,
                query_type,
                **(params or {}),
            )
        )
