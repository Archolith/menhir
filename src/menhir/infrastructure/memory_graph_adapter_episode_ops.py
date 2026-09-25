"""Episode reset/recovery and edge-weight delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from typing import Any


class MemoryGraphEpisodeOpsMixin:
    """Episode reset/recovery and edge-weight delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # Episode reset / recovery delegates → EpisodeRepository
    # -------------------------------------------------------------------------

    def reset_stale_enriching_episodes(self, *, max_attempts: int) -> int:
        return self._episodes.reset_stale_enriching_episodes(max_attempts=max_attempts)

    def reset_orphaned_enriching_episodes(self, *, max_attempts: int) -> int:
        return self._episodes.reset_orphaned_enriching_episodes(
            max_attempts=max_attempts
        )

    def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        return self._episodes.force_reset_failed_episode(episode_uuid)

    def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int
    ) -> bool:
        return self._episodes.force_release_episode_lease(
            episode_uuid, max_attempts=max_attempts
        )

    def release_worker_episode_leases(
        self, worker_id: str, *, max_attempts: int
    ) -> int:
        return self._episodes.release_worker_episode_leases(
            worker_id, max_attempts=max_attempts
        )

    def list_episode_processing(
        self,
        *,
        processing_states: list[str] | None = None,
        limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._episodes.list_episode_processing(
            processing_states=processing_states, limit=limit, namespace=namespace
        )

    def fetch_stale_enriching_episodes(
        self,
        *,
        include_missing_lease: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._episodes.fetch_stale_enriching_episodes(
            include_missing_lease=include_missing_lease, limit=limit
        )

    def update_episode_processing(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        stage: str | None = None,
        substage: str | None = None,
        progress: float | None = None,
        steps_completed: int | None = None,
        steps_total: int | None = None,
        llm_active_task: str | None = None,
        llm_active_kind: str | None = None,
        llm_active_model: str | None = None,
        llm_active_endpoint: str | None = None,
        clear_llm_active: bool = False,
        heartbeat: bool = True,
    ) -> bool:
        return self._episodes.update_episode_processing(
            episode_uuid,
            worker_id=worker_id,
            stage=stage,
            substage=substage,
            progress=progress,
            steps_completed=steps_completed,
            steps_total=steps_total,
            llm_active_task=llm_active_task,
            llm_active_kind=llm_active_kind,
            llm_active_model=llm_active_model,
            llm_active_endpoint=llm_active_endpoint,
            clear_llm_active=clear_llm_active,
            heartbeat=heartbeat,
        )

    def touch_episode_processing_heartbeat(
        self, episode_uuid: str, *, worker_id: str | None = None
    ) -> bool:
        return self._episodes.touch_episode_processing_heartbeat(
            episode_uuid, worker_id=worker_id
        )

    def increment_episode_llm_usage(
        self,
        episode_uuid: str,
        *,
        task_delta: int = 1,
        phase: str = "started",
        kind: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        task: str | None = None,
        error: str | None = None,
    ) -> bool:
        return self._episodes.increment_episode_llm_usage(
            episode_uuid,
            task_delta=task_delta,
            phase=phase,
            kind=kind,
            model=model,
            endpoint=endpoint,
            task=task,
            error=error,
        )

    def reset_zero_extraction_episodes(self) -> int:
        return self._episodes.reset_zero_extraction_episodes()

    def count_pending_episodes(self, session_id: str | None = None) -> int:
        return self._episodes.count_pending_episodes(session_id)

    def cleanup_orphan_episodes(self, session_id: str | None = None) -> int:
        return self._episodes.cleanup_orphan_episodes(session_id)

    def increment_edge_weight(self, edge_uuid: str) -> bool:
        return self._consolidation.increment_edge_weight(edge_uuid)

    def increment_edge_weights(self, edge_uuids: list[str]) -> int:
        return self._consolidation.increment_edge_weights(edge_uuids)

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        return self._consolidation.update_edge_facts(updates)
