"""Artifact and candidate operation methods for the HTTP-backed backend adapter.

Extracted from ``backend_client_ops``: artifact CRUD/relationships/status, corpus audit
and source relocation, plus the memory candidate review surface. Reached through
``BackendClientOpsMixin``, which composes this mixin.
"""

from __future__ import annotations

from typing import Any


class BackendClientArtifactsOpsMixin:
    """Artifact and candidate operations for the HTTP-backed backend adapter."""

    async def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        return await self._request(
            "get_artifact", {"artifact_uuid": artifact_uuid, "namespace": namespace}
        )

    async def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_artifacts",
            {
                "artifact_type": artifact_type,
                "status": status,
                "namespace": namespace,
                "limit": limit,
            },
        )

    async def list_artifact_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_artifact_questions",
            {
                "artifact_uuid": artifact_uuid,
                "status": status,
                "namespace": namespace,
                "limit": limit,
            },
        )

    async def get_artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        return await self._request("get_artifact_relationships", {"artifact_uuid": artifact_uuid})

    async def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        return await self._request(
            "link_artifacts",
            {"source_uuid": source_uuid, "target_uuid": target_uuid, "relation": relation},
        )

    async def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        return await self._request(
            "supersede_artifact", {"new_uuid": new_uuid, "old_uuid": old_uuid}
        )

    async def transition_artifact_status(
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "transition_artifact_status",
            {
                "artifact_uuid": artifact_uuid,
                "to_status": to_status,
                "namespace": namespace,
            },
        )

    async def fetch_artifact_corpus_audit(
        self,
        *,
        repo_path: str,
        repository: str,
        from_commit: str | None = None,
        conflict_limit: int = 25,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "fetch_artifact_corpus_audit",
            {
                "repo_path": repo_path,
                "repository": repository,
                "from_commit": from_commit,
                "conflict_limit": conflict_limit,
                "namespace": namespace,
            },
        )

    async def relocate_artifact_source(
        self,
        *,
        artifact_uuid: str,
        old_path: str,
        new_path: str,
        repository: str | None = None,
        medium: str = "markdown",
        expected_old_integrity: str = "",
        observed_integrity: str = "",
    ) -> dict[str, Any]:
        return await self._request(
            "relocate_artifact_source",
            {
                "artifact_uuid": artifact_uuid,
                "old_path": old_path,
                "new_path": new_path,
                "repository": repository,
                "medium": medium,
                "expected_old_integrity": expected_old_integrity,
                "observed_integrity": observed_integrity,
            },
        )

    async def create_candidate(
        self,
        *,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "create_candidate",
            {
                "content": content,
                "source": source,
                "cluster_id": cluster_id,
                "label": label,
                "kind": kind,
                "candidate_type": candidate_type,
                "type": type,
                "evidence_strength": evidence_strength,
                "distinct_sessions": distinct_sessions,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "notes": notes,
                "source_confidence": source_confidence,
                "namespace": namespace,
            },
        )

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_candidates", {"source": source, "limit": limit}
        )

    async def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        return await self._request("fetch_candidate", {"uuid": uuid})

    async def promote_candidate(self, uuid: str) -> bool:
        return bool(await self._request("promote_candidate", {"uuid": uuid}))

    async def reject_candidate(self, uuid: str) -> bool:
        return bool(await self._request("reject_candidate", {"uuid": uuid}))

    async def approve_candidate(self, uuid: str) -> dict[str, Any]:
        return await self._request("approve_candidate", {"uuid": uuid})
