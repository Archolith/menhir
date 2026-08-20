"""Conflict, scheduler, telemetry, and mutation operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.providers import ProviderConfig

from .backend_shared import _to_jsonable


class RuntimeProviderAdminOpsMixin:
    """Conflict, scheduler, telemetry, and mutation operations for the in-process backend adapter."""

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

    async def scheduler_force_takeover(self, reason: str) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            from menhir.core.runtime import _start_scheduler

            scheduler = await _start_scheduler(self.built)
        return bool(await scheduler.force_takeover(reason=reason))

    async def scheduler_status_snapshot(self) -> dict[str, Any] | None:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return None
        return _to_jsonable(await self._off_loop(scheduler.status_snapshot))

    async def scheduler_pause(self) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return False
        was_running = await self._off_loop(scheduler.is_running)
        await scheduler.stop()
        return was_running

    async def scheduler_resume(self) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return False
        return bool(await scheduler.start())

    async def fetch_operation_stats(
        self, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_operation_stats, since_hours=since_hours
            )
        )

    async def fetch_failure_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_failure_summary, since_hours=since_hours
            )
        )

    async def fetch_enrichment_rate(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_enrichment_rate, since_hours=since_hours
            )
        )

    async def fetch_lifecycle_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_lifecycle_summary, since_hours=since_hours
            )
        )

    async def fetch_episode_task_events(
        self, episode_uuid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_episode_task_events,
                episode_uuid=episode_uuid,
                limit=limit,
            )
        )

    async def fetch_recent_failures(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_recent_failures,
                limit=limit,
                episode_uuid=episode_uuid,
            )
        )

    async def fetch_recent_lifecycle_events(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.telemetry.fetch_recent_lifecycle_events,
                limit=limit,
                episode_uuid=episode_uuid,
            )
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
        await self._off_loop(
            self.telemetry.record_conflict_resolution,
            uuid_a=uuid_a,
            uuid_b=uuid_b,
            status=status,
            group_id=group_id,
            action=action,
            reviewed_by=reviewed_by,
        )

    async def fetch_memory_overview(self) -> dict[str, Any]:
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_memory_overview)
        )

    async def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(self.built.graphiti_client.circuit_breaker_snapshots)
        )

    async def embedding_cache_stats(self) -> dict[str, int]:
        return _to_jsonable(
            await self._off_loop(self.built.graphiti_client.embedding_cache_stats)
        )

    async def get_provider_config(self) -> dict[str, Any]:
        settings = self.built.settings
        chat_provider = ProviderConfig.for_chat(settings)
        graphiti_llm_provider = ProviderConfig.for_graphiti_llm(settings)
        graphiti_embed_provider = ProviderConfig.for_graphiti_embedder(settings)
        graphiti_reranker_provider = ProviderConfig.for_graphiti_reranker(settings)
        return {
            "neo4j_uri": getattr(settings, "neo4j_uri", ""),
            "chat_provider": chat_provider.kind.value,
            "graphiti_provider": graphiti_llm_provider.kind.value,
            "graphiti_embed_provider": graphiti_embed_provider.kind.value,
            "graphiti_reranker_provider": graphiti_reranker_provider.kind.value,
            "neo4j_database": getattr(settings, "neo4j_database", ""),
            "local_llm_base_url": getattr(settings, "local_llm_base_url", ""),
            "local_llm_embed_base_url": getattr(
                settings, "local_llm_embed_base_url", ""
            ),
            "chat_model": chat_provider.chat_model,
            "graphiti_llm_chat_model": graphiti_llm_provider.chat_model,
            "gemini_chat_model": getattr(settings, "gemini_chat_model", ""),
            "embed_model": chat_provider.embed_model,
            "graphiti_embed_model": graphiti_embed_provider.embed_model,
            "backend_url": getattr(settings, "backend_url", ""),
        }

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
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.fetch_artifact_corpus_audit,
                repo_path=repo_path,
                repository=repository,
                from_commit=from_commit,
                conflict_limit=conflict_limit,
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

    async def close_todo(self, uuid: str) -> bool:
        return bool(await self._off_loop(self.built.graph_adapter.close_todo, uuid))

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
        kwargs: dict[str, Any] = {
            "content": content,
            "target_date": target_date,
            "source": source,
            "name": name,
            "flagged": flagged,
            "namespace": namespace,
            "turn_evidence_uuid": turn_evidence_uuid,
        }
        if bootstrap_scope is not None:
            kwargs["bootstrap_scope"] = bootstrap_scope
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.create_temporal, **kwargs)
        )

    async def list_temporal_in_window(
        self, *, window_days: int = 30, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_temporal_in_window,
                window_days=window_days,
                namespace=namespace,
            )
        )

    async def complete_temporal(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.complete_temporal, uuid)
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
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.create_candidate,
                content=content,
                source=source,
                cluster_id=cluster_id,
                label=label,
                kind=kind,
                candidate_type=candidate_type,
                type=type,
                evidence_strength=evidence_strength,
                distinct_sessions=distinct_sessions,
                first_seen=first_seen,
                last_seen=last_seen,
                notes=notes,
                source_confidence=source_confidence,
                namespace=namespace,
            )
        )

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return _to_jsonable(
            await self._off_loop(
                self.built.graph_adapter.list_candidates,
                source=source,
                limit=limit,
            )
        )

    async def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        return _to_jsonable(
            await self._off_loop(self.built.graph_adapter.fetch_candidate, uuid)
        )

    async def promote_candidate(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.promote_candidate, uuid)
        )

    async def reject_candidate(self, uuid: str) -> bool:
        return bool(
            await self._off_loop(self.built.graph_adapter.reject_candidate, uuid)
        )

    async def approve_candidate(self, uuid: str) -> dict[str, Any]:
        # Service-level approval: promote + synchronous contradiction check.
        return _to_jsonable(await self.built.candidate_service.approve(uuid))
