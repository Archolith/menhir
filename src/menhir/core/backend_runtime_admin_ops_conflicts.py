"""Conflict-group, episode-processing, and enrichment-recovery operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class RuntimeConflictOpsMixin:
    """Conflict-group and episode-processing operations for the in-process backend adapter."""

    async def list_conflict_groups(
        self, *, status: str | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_conflict_groups,
                status=status,
                limit=limit,
                namespace=namespace,
            )
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
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.resolve_conflict_group,
                group_id,
                action,
                keep_uuid=keep_uuid,
                remove_uuid=remove_uuid,
                resolution_status=resolution_status,
                allow_promoted_removal=allow_promoted_removal,
                namespace=namespace,
            )
        )

    async def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "pending", limit: int = 50,
        namespace: str | None = None,
    ) -> int:
        return int(
            await self._off_loop(
                self.built.graph_adapter.requeue_conflicts_for_llm_review,
                from_status=from_status,
                limit=limit,
                namespace=namespace,
            )
        )

    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self.built.lifecycle_service.scan_for_conflicts(
                limit=limit, cursor=cursor, namespace=namespace
            )
        )

    async def confirm_pending_conflicts(
        self, *, limit: int = 10, verbose: bool = False,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return _to_jsonable(
            await self.built.lifecycle_service.confirm_pending_conflicts(
                limit=limit, verbose=verbose, namespace=namespace
            )
        )

    async def fetch_episode_processing(
        self, episode_uuid: str
    ) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_episode_processing, episode_uuid
            )
        )

    async def list_episode_processing(
        self, *, states: list[str] | None = None, limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_episode_processing,
                processing_states=states,
                limit=limit,
                namespace=namespace,
            )
        )

    async def get_queue_depth(self) -> int:
        return int(await self._off_loop(self.built.ingest_service.get_queue_depth))

    async def get_failed_enrichment_count(self) -> int:
        return int(
            await self._off_loop(self.built.ingest_service.get_failed_enrichment_count)
        )

    async def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self._off_loop(
                self.built.graph_adapter.force_reset_failed_episode, episode_uuid
            )
        )

    async def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int | None = None
    ) -> bool:
        attempts = (
            max_attempts
            if max_attempts is not None
            else await self._off_loop(
                self.built.ingest_service.get_max_enrichment_attempts
            )
        )
        return bool(
            await self._off_loop(
                self.built.graph_adapter.force_release_episode_lease,
                episode_uuid,
                max_attempts=attempts,
            )
        )

    async def fetch_stale_enriching_episodes(
        self, limit: int = 25
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_stale_enriching_episodes, limit=limit
            )
        )

    async def recover_stale_enrichment_leases(self, limit: int = 10) -> dict[str, int]:
        (
            stale_reset,
            requeued,
        ) = await self.built.ingest_service.recover_stale_enrichment_leases(limit=limit)
        return {"stale_reset": int(stale_reset), "requeued_pending": int(requeued)}

    async def get_max_enrichment_attempts(self) -> int:
        return int(self.built.ingest_service.get_max_enrichment_attempts())

    async def recover_orphans(self, *, max_age_hours: float = 24.0) -> dict[str, Any]:
        return _to_jsonable(
            await self.built.lifecycle_service.recover_orphans(
                max_age_hours=max_age_hours
            )
        )

    async def fetch_session_entities(
        self, *, session_id: str | None = None, max_age_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_session_entities,
                session_id=session_id or self._effective_session_id(),
                max_age_hours=max_age_hours,
            )
        )
