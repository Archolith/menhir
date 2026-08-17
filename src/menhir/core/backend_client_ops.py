"""Operation methods for the HTTP-backed backend adapter."""

from __future__ import annotations

from typing import Any


class BackendClientOpsMixin:
    """Behavior mixin holding the large HTTP operation surface."""

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
        self, limit: int = 50, workspace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_flagged_memories", {"limit": limit, "workspace": workspace}
        )

    async def fetch_flagged_memory_bootstrap_version(
        self, workspace: str | None = None
    ) -> str:
        return str(
            await self._request(
                "fetch_flagged_memory_bootstrap_version", {"workspace": workspace}
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
    ) -> dict[str, Any]:
        return await self._request(
            "ingest_document",
            {
                "path": path,
                "project": project,
                "session_id": session_id,
                "user_id": user_id,
                "document_type": document_type,
            },
        )

    async def scan_and_write_project(
        self, path: str, *, name: str | None, force: bool, session_id: str, user_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "scan_and_write_project",
            {
                "path": path,
                "name": name,
                "force": force,
                "session_id": session_id,
                "user_id": user_id,
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
        self, *, status: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_conflict_groups", {"status": status, "limit": limit}
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
        return await self._request(
            "resolve_conflict_group",
            {
                "group_id": group_id,
                "action": action,
                "resolution_status": resolution_status,
                "keep_uuid": keep_uuid,
                "remove_uuid": remove_uuid,
                "allow_promoted_removal": allow_promoted_removal,
            },
        )

    async def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "pending", limit: int = 50
    ) -> int:
        return int(
            await self._request(
                "requeue_conflicts_for_llm_review",
                {"from_status": from_status, "limit": limit},
            )
        )

    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "scan_for_conflicts", {"limit": limit, "cursor": cursor}
        )

    async def confirm_pending_conflicts(
        self, *, limit: int = 10, verbose: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "confirm_pending_conflicts", {"limit": limit, "verbose": verbose}
        )

    async def fetch_episode_processing(
        self, episode_uuid: str
    ) -> dict[str, Any] | None:
        return await self._request(
            "fetch_episode_processing", {"episode_uuid": episode_uuid}
        )

    async def list_episode_processing(
        self, *, states: list[str] | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_episode_processing", {"states": states, "limit": limit}
        )

    async def get_queue_depth(self) -> int:
        return int(await self._request("get_queue_depth"))

    async def get_failed_enrichment_count(self) -> int:
        return int(await self._request("get_failed_enrichment_count"))

    async def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        return bool(
            await self._request(
                "force_reset_failed_episode", {"episode_uuid": episode_uuid}
            )
        )

    async def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int | None = None
    ) -> bool:
        return bool(
            await self._request(
                "force_release_episode_lease",
                {"episode_uuid": episode_uuid, "max_attempts": max_attempts},
            )
        )

    async def fetch_stale_enriching_episodes(
        self, limit: int = 25
    ) -> list[dict[str, Any]]:
        return await self._request("fetch_stale_enriching_episodes", {"limit": limit})

    async def recover_stale_enrichment_leases(self, limit: int = 10) -> dict[str, int]:
        return await self._request("recover_stale_enrichment_leases", {"limit": limit})

    async def get_max_enrichment_attempts(self) -> int:
        return int(await self._request("get_max_enrichment_attempts"))

    async def recover_orphans(self, *, max_age_hours: float = 24.0) -> dict[str, Any]:
        return await self._request("recover_orphans", {"max_age_hours": max_age_hours})

    async def fetch_session_entities(
        self, *, session_id: str | None = None, max_age_hours: float = 24.0
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_session_entities",
            {"session_id": session_id, "max_age_hours": max_age_hours},
        )

    async def scheduler_force_takeover(self, reason: str) -> bool:
        return bool(await self._request("scheduler_force_takeover", {"reason": reason}))

    async def scheduler_status_snapshot(self) -> dict[str, Any] | None:
        return await self._request("scheduler_status_snapshot")

    async def scheduler_pause(self) -> bool:
        return bool(await self._request("scheduler_pause"))

    async def scheduler_resume(self) -> bool:
        return bool(await self._request("scheduler_resume"))

    async def fetch_operation_stats(
        self, since_hours: int = 24
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_operation_stats", {"since_hours": since_hours}
        )

    async def fetch_failure_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_failure_summary", {"since_hours": since_hours}
        )

    async def fetch_enrichment_rate(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_enrichment_rate", {"since_hours": since_hours}
        )

    async def fetch_lifecycle_summary(self, since_hours: int = 24) -> dict[str, Any]:
        return await self._request(
            "fetch_lifecycle_summary", {"since_hours": since_hours}
        )

    async def fetch_episode_task_events(
        self, episode_uuid: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_episode_task_events", {"episode_uuid": episode_uuid, "limit": limit}
        )

    async def fetch_recent_failures(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_recent_failures", {"limit": limit, "episode_uuid": episode_uuid}
        )

    async def fetch_recent_lifecycle_events(
        self, *, limit: int = 20, episode_uuid: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "fetch_recent_lifecycle_events",
            {"limit": limit, "episode_uuid": episode_uuid},
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

    async def fetch_memory_overview(self) -> dict[str, Any]:
        return await self._request("fetch_memory_overview")

    async def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return await self._request("circuit_breaker_snapshots")

    async def embedding_cache_stats(self) -> dict[str, int]:
        return await self._request("embedding_cache_stats")

    async def get_provider_config(self) -> dict[str, Any]:
        return await self._request("get_provider_config")

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
        return await self._request(
            "create_todo",
            {
                "content": content,
                "code_ref": code_ref,
                "priority": priority,
                "source": source,
                "episode_uuid": episode_uuid,
                "structure_project": structure_project,
                "due_date": due_date,
                "namespace": namespace,
            },
        )

    async def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_todos", {"status": status, "limit": limit, "namespace": namespace}
        )

    async def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        return await self._request("get_todo", {"uuid": uuid, "namespace": namespace})

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
        self, artifact_uuid: str, to_status: str
    ) -> dict[str, Any]:
        return await self._request(
            "transition_artifact_status",
            {"artifact_uuid": artifact_uuid, "to_status": to_status},
        )

    async def fetch_artifact_corpus_audit(
        self,
        *,
        repo_path: str,
        repository: str,
        from_commit: str | None = None,
        conflict_limit: int = 25,
    ) -> dict[str, Any]:
        return await self._request(
            "fetch_artifact_corpus_audit",
            {
                "repo_path": repo_path,
                "repository": repository,
                "from_commit": from_commit,
                "conflict_limit": conflict_limit,
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

    async def close_todo(self, uuid: str) -> bool:
        return bool(await self._request("close_todo", {"uuid": uuid}))

    async def delete_todo(self, uuid: str) -> bool:
        return bool(await self._request("delete_todo", {"uuid": uuid}))

    async def close_stale_todos(self, *, older_than_days: int = 60, dry_run: bool = True) -> dict[str, Any]:
        return await self._request(
            "close_stale_todos",
            {"older_than_days": older_than_days, "dry_run": dry_run},
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
        return await self._request(
            "create_temporal",
            {
                "content": content,
                "target_date": target_date,
                "source": source,
                "name": name,
                "flagged": flagged,
                "bootstrap_scope": bootstrap_scope,
                "namespace": namespace,
                "turn_evidence_uuid": turn_evidence_uuid,
            },
        )

    async def list_temporal_in_window(
        self, *, window_days: int = 30
    ) -> list[dict[str, Any]]:
        return await self._request(
            "list_temporal_in_window", {"window_days": window_days}
        )

    async def complete_temporal(self, uuid: str) -> bool:
        return bool(await self._request("complete_temporal", {"uuid": uuid}))

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
