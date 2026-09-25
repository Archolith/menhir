"""Artifact read, link, and lifecycle operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeArtifactOpsMixin:
    """Artifact read, link, and lifecycle operations for the in-process backend adapter."""

    async def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.get_artifact, artifact_uuid, namespace=namespace
            )
        )

    async def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_artifacts,
                artifact_type=artifact_type,
                status=status,
                namespace=namespace,
                limit=limit,
            )
        )

    async def list_artifact_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_artifact_questions,
                artifact_uuid=artifact_uuid,
                status=status,
                namespace=namespace,
                limit=limit,
            )
        )

    async def get_artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.get_artifact_relationships, artifact_uuid
            )
        )

    async def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.link_artifacts, source_uuid, target_uuid, relation
            )
        )

    async def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.supersede_artifact, new_uuid, old_uuid
            )
        )

    async def transition_artifact_status(
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.transition_artifact_status,
                artifact_uuid,
                to_status,
                namespace=namespace,
            )
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
        # Both the in-process adapter transport and the HTTP route (which dispatches onto this
        # same method by operation name) funnel through here, so this is the only place the
        # namespace pin can be honoured.
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_artifact_corpus_audit,
                repo_path=repo_path,
                repository=repository,
                from_commit=from_commit,
                conflict_limit=conflict_limit,
                namespace=namespace,
            )
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
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.relocate_artifact_source,
                artifact_uuid=artifact_uuid,
                old_path=old_path,
                new_path=new_path,
                repository=repository,
                medium=medium,
                expected_old_integrity=expected_old_integrity,
                observed_integrity=observed_integrity,
            )
        )
