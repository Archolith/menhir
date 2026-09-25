"""Project and conflict operation methods for the HTTP-backed backend adapter.

Extracted from ``backend_client_ops``: document ingestion, project scan/write, structural
queries, and the conflict review surface. Reached through ``BackendClientOpsMixin``, which
composes this mixin.
"""

from __future__ import annotations

from typing import Any


class BackendClientProjectsOpsMixin:
    """Project scan/ingest/structure and conflict operations for the HTTP-backed backend adapter."""

    async def get_scan_fingerprint(self, project_name: str) -> str | None:
        return await self._request(
            "get_scan_fingerprint", {"project_name": project_name}
        )

    async def ingest_document(
        self,
        path: str,
        *,
        project: str | None,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "ingest_document",
            {
                "path": path,
                "project": project,
                "session_id": session_id,
                "user_id": user_id,
                "document_type": document_type,
                "identity_action": identity_action,
                "adopt_project_id": adopt_project_id,
            },
        )

    async def scan_and_write_project(
        self, path: str, *, name: str | None, force: bool, session_id: str, user_id: str,
        force_identity: bool = False,
        identity_action: str | None = None,
        adopt_project_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "scan_and_write_project",
            {
                "path": path,
                "name": name,
                "force": force,
                "session_id": session_id,
                "user_id": user_id,
                "force_identity": force_identity,
                "identity_action": identity_action,
                "adopt_project_id": adopt_project_id,
            },
        )

    async def write_project_structure(
        self, scan: dict[str, Any], *, session_id: str, user_id: str
    ) -> dict[str, int]:
        return await self._request(
            "write_project_structure",
            {"scan": scan, "session_id": session_id, "user_id": user_id},
        )

    async def query_structure(
        self, project: str, query_type: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any]:
        return await self._request(
            "query_structure",
            {"project": project, "query_type": query_type, "params": params or {}},
        )

    async def list_conflict_groups(
        self, *, status: str | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_conflict_groups",
            {"status": status, "limit": limit, "namespace": namespace},
        )

    async def resolve_conflict_group(
        self,
        group_id: str,
        *,
        action: str,
        resolution_status: str,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        allow_promoted_removal: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "resolve_conflict_group",
            {
                "group_id": group_id,
                "action": action,
                "resolution_status": resolution_status,
                "keep_uuid": keep_uuid,
                "remove_uuid": remove_uuid,
                "allow_promoted_removal": allow_promoted_removal,
                "namespace": namespace,
            },
        )

    async def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "pending", limit: int = 50,
        namespace: str | None = None,
    ) -> int:
        return int(
            await self._request(
                "requeue_conflicts_for_llm_review",
                {"from_status": from_status, "limit": limit, "namespace": namespace},
            )
        )

    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "scan_for_conflicts",
            {"limit": limit, "cursor": cursor, "namespace": namespace},
        )

    async def confirm_pending_conflicts(
        self, *, limit: int = 10, verbose: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "confirm_pending_conflicts",
            {"limit": limit, "verbose": verbose, "namespace": namespace},
        )

    async def record_conflict_resolution(
        self,
        *,
        uuid_a: str,
        uuid_b: str,
        status: str,
        group_id: str,
        action: str,
        reviewed_by: str,
    ) -> None:
        await self._request(
            "record_conflict_resolution",
            {
                "uuid_a": uuid_a,
                "uuid_b": uuid_b,
                "status": status,
                "group_id": group_id,
                "action": action,
                "reviewed_by": reviewed_by,
            },
        )
