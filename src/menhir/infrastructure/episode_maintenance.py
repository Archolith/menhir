"""Episode maintenance, reset, and cleanup helpers."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.cypher import (
    Cypher,
    EPISODE_PROCESSING_FIELDS,
    LLM_RESET_SET,
    build_reset_or_fail_query,
)

#: Rows per terminal-failure sweep. Each row becomes a full-content read plus one MERGE on
#: ``:Entity`` by ``raw_capture_for``, so this bounds both the fetch and the write loop above it.
_EXHAUSTED_EPISODE_FETCH_LIMIT = 200


class EpisodeMaintenanceRepository:
    """Scheduled reset, recovery, and cleanup operations."""

    neo4j: Any

    def reset_stale_enriching_episodes(self, *, max_attempts: int) -> int:
        query = build_reset_or_fail_query(
            where=[
                "n.processing_state = 'ENRICHING'",
                "(n.processing_lease_expires_at < datetime() OR n.processing_lease_expires_at IS NULL)",
            ],
            exhausted_substage=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_attempts_exhausted'"
                " ELSE 'lease_expired_attempts_exhausted' END"
            ),
            reset_substage=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_reset'"
                " ELSE 'lease_expired_reset' END"
            ),
            exhausted_error=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_reset_attempts_exhausted'"
                " ELSE 'lease_expired_reset_attempts_exhausted' END"
            ),
            reset_error=(
                "CASE WHEN n.processing_lease_expires_at IS NULL"
                " THEN 'lease_missing_reset'"
                " ELSE 'lease_expired_reset' END"
            ),
        )
        rows = self.neo4j.execute(query, params={"max_attempts": max(1, max_attempts)})
        return int(rows[0].get("reset", 0)) if rows else 0

    def reset_orphaned_enriching_episodes(self, *, max_attempts: int) -> int:
        query = build_reset_or_fail_query(
            where=[
                "n.processing_state = 'ENRICHING'",
                "(n.processing_owner IS NULL OR trim(toString(n.processing_owner)) = '')",
            ],
            exhausted_substage="'orphaned_reset_attempts_exhausted'",
            reset_substage="'orphaned_reset'",
            exhausted_error="'orphaned_enriching_reset_attempts_exhausted'",
            reset_error="'orphaned_enriching_reset'",
        )
        rows = self.neo4j.execute(query, params={"max_attempts": max(1, max_attempts)})
        return int(rows[0].get("reset", 0)) if rows else 0

    def fail_exhausted_pending_episodes(self, *, max_attempts: int) -> int:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'PENDING'",
                "coalesce(toInteger(n.processing_attempts), 0) >= $max_attempts",
            )
            .set(
                (
                    "n.processing_state = 'FAILED'",
                    "n.processing_stage = 'failed'",
                    "n.processing_substage = 'pending_attempts_exhausted'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = 100.0",
                    "n.processing_steps_completed = coalesce("
                    " toInteger(n.processing_steps_total),"
                    " coalesce(toInteger(n.processing_steps_completed), 5))",
                    *LLM_RESET_SET,
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_started_at = null",
                    "n.processing_completed_at = datetime()",
                    "n.processing_error = CASE"
                    " WHEN n.processing_error IS NULL OR trim(toString(n.processing_error)) = ''"
                    " THEN 'pending_attempts_exhausted'"
                    " ELSE n.processing_error END",
                )
            )
            .return_raw("count(n) AS failed")
            .build()
        )
        rows = self.neo4j.execute(query, params={"max_attempts": max(1, max_attempts)})
        return int(rows[0].get("failed", 0)) if rows else 0

    def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        query = (
            Cypher()
            .match("(n:Episodic {uuid: $uuid})")
            .where("n.processing_state IN ['FAILED', 'PENDING']")
            .set(
                (
                    "n.processing_state = 'PENDING'",
                    "n.processing_stage = 'queued'",
                    "n.processing_progress = 0.0",
                    "n.processing_steps_completed = 0",
                    "n.processing_llm_tasks_attempt = 0",
                    "n.processing_attempts = 0",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_started_at = null",
                    "n.processing_completed_at = null",
                    "n.processing_error = null",
                )
            )
            .return_raw("count(n) AS reset")
            .build()
        )
        rows = self.neo4j.execute(query, {"uuid": episode_uuid})
        return bool(rows and int(rows[0].get("reset", 0)) > 0)

    def force_release_episode_lease(self, episode_uuid: str, *, max_attempts: int) -> bool:
        query = build_reset_or_fail_query(
            match="(n:Episodic {uuid: $uuid})",
            where=["n.processing_state = 'ENRICHING'"],
            exhausted_substage="'lease_released_attempts_exhausted'",
            reset_substage="'lease_released'",
            exhausted_error="'manual_lease_release_attempts_exhausted'",
            reset_error="'manual_lease_release'",
            return_alias="released",
        )
        rows = self.neo4j.execute(query, {"uuid": episode_uuid, "max_attempts": max(1, max_attempts)})
        return bool(rows and int(rows[0].get("released", 0)) > 0)

    def release_worker_episode_leases(self, worker_id: str, *, max_attempts: int) -> int:
        query = build_reset_or_fail_query(
            where=[
                "n.processing_state = 'ENRICHING'",
                "n.processing_owner = $worker_id",
            ],
            exhausted_substage="'worker_shutdown_release_attempts_exhausted'",
            reset_substage="'worker_shutdown_release'",
            exhausted_error="'worker_shutdown_release_attempts_exhausted'",
            reset_error="'worker_shutdown_release'",
            return_alias="released",
        )
        rows = self.neo4j.execute(query, {"worker_id": worker_id, "max_attempts": max(1, max_attempts)})
        return int(rows[0].get("released", 0)) if rows else 0

    def list_episode_processing(
        self,
        *,
        processing_states: list[str] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 200))
        states = [str(state).strip().upper() for state in (processing_states or []) if str(state).strip()]
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where_if(bool(states), "n.processing_state IN $states")
            .return_fields(
                EPISODE_PROCESSING_FIELDS,
                "n.resolved_episode_uuid AS resolved_episode_uuid",
                "n.session_id AS session_id",
                "n.source AS source",
                "n.created_at AS created_at",
            )
            .order_by("coalesce(n.processing_started_at, n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        return self.neo4j.execute(query, params={"states": states, "limit": safe_limit})

    def fetch_stale_enriching_episodes(
        self,
        *,
        include_missing_lease: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'ENRICHING'",
                "(n.processing_lease_expires_at < datetime()"
                " OR ($include_missing_lease = true AND n.processing_lease_expires_at IS NULL))",
            )
            .return_fields(
                EPISODE_PROCESSING_FIELDS,
                "n.source AS source",
                "n.session_id AS session_id",
            )
            .order_by("coalesce(n.processing_started_at, n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        return self.neo4j.execute(query, params={"include_missing_lease": include_missing_lease, "limit": safe_limit})

    def reset_zero_extraction_episodes(self) -> int:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'READY'",
                "NOT EXISTS { MATCH (n)-[]-(e:Entity) }",
                "coalesce(toInteger(n.enriched_nodes_touched), 0) <= 1",
                "coalesce(toInteger(n.enriched_edges_touched), 0) = 0",
            )
            .set(
                (
                    "n.processing_state = 'PENDING'",
                    "n.processing_stage = 'queued'",
                    "n.processing_progress = 0.0",
                    "n.processing_steps_completed = 0",
                    "n.processing_llm_tasks_attempt = 0",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = 'zero_extraction_reset'",
                    "n.processing_attempts = 0",
                )
            )
            .return_raw("count(n) AS reset")
            .build()
        )
        rows = self.neo4j.execute(query)
        return int(rows[0].get("reset", 0)) if rows else 0

    def count_pending_episodes(self, session_id: str | None = None) -> int:
        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.scope = 'SESSION'", "n.processing_state IN ['PENDING', 'ENRICHING']")
            .where_if(session_id is not None, "n.session_id = $session_id")
            .return_raw("count(n) AS pending")
            .build()
        )
        rows = self.neo4j.execute(query, params=params)
        return int(rows[0].get("pending", 0)) if rows else 0

    def cleanup_orphan_episodes(self, session_id: str | None = None) -> int:
        params: dict[str, Any] = {}
        if session_id is not None:
            params["session_id"] = session_id
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.scope = 'SESSION'",
                "n.processing_state = 'READY'",
                "coalesce(n.user_flagged, false) = false",
                "NOT EXISTS { MATCH (n)-[]-(e:Entity) }",
            )
            .where_if(session_id is not None, "n.session_id = $session_id")
            .detach_delete("n")
            .return_raw("count(n) AS deleted")
            .build()
        )
        rows = self.neo4j.execute(query, params=params)
        return int(rows[0].get("deleted", 0)) if rows else 0

    def create_raw_capture_entity(
        self,
        episode_uuid: str,
        name: str,
        content: str,
        namespace: str,
        session_id: str,
        user_id: str,
        source: str,
    ) -> str | None:
        """Create a degraded raw-capture Entity for a failed episode with memorable content.

        This is called when an episode fails terminally (preflight oversize rejection,
        exhausted-retry failure) but has content worth preserving for recall.

        Args:
            episode_uuid: The failed episode's UUID (for provenance link).
            name: Short identifier for the raw capture (e.g., first 60 chars of content).
            content: Full episode text to store.
            namespace: The episode's namespace.
            session_id: Session ID for stamping.
            user_id: User ID for stamping.
            source: Source for stamping.

        Returns:
            UUID of the created raw-capture Entity, or None if creation failed.
            (Idempotent on re-failure: MERGE on raw_capture_for = episode_uuid.)
        """
        from uuid import uuid4

        entity_uuid = str(uuid4())
        query = (
            Cypher()
            .merge(
                "(n:Entity { raw_capture_for: $episode_uuid })"
            )
            .on_create_set(
                (
                    "n.uuid = $entity_uuid",
                    "n.name = $name",
                    "n.content = $content",
                    "n.type = 'ENTITY'",
                    "n.scope = 'SESSION'",
                    "n.raw_capture = true",
                    "n.namespace = $namespace",
                    "n.session_id = $session_id",
                    "n.user_id = $user_id",
                    "n.source = $source",
                    "n.created_at = datetime()",
                    "n.freshness = 'ACTIVE'",
                )
            )
            .return_raw("n.uuid AS entity_uuid")
            .build()
        )
        try:
            rows = self.neo4j.execute(
                query,
                params={
                    "episode_uuid": episode_uuid,
                    "entity_uuid": entity_uuid,
                    "name": name,
                    "content": content,
                    "namespace": namespace,
                    "session_id": session_id,
                    "user_id": user_id,
                    "source": source,
                },
            )
            if rows:
                captured_uuid = rows[0].get("entity_uuid")
                # Link episode to raw-capture entity for provenance
                self.neo4j.execute(
                    """
                    MATCH (ep:Episodic {uuid: $episode_uuid})
                    MATCH (entity:Entity {uuid: $entity_uuid})
                    MERGE (ep)-[:MENTIONS]->(entity)
                    """,
                    params={"episode_uuid": episode_uuid, "entity_uuid": captured_uuid},
                )
                return str(captured_uuid) if captured_uuid else None
        except (OSError, RuntimeError) as e:
            # Capture failure must not break the failure handling itself
            import logging

            logging.getLogger(__name__).warning(
                "Failed to create raw-capture entity for episode %s: %s",
                episode_uuid,
                e,
            )
        return None

    def mark_raw_capture_superseded(self, episode_uuid: str) -> bool:
        """Mark raw-capture entities for this episode as GONE when repair succeeds.

        Called after a previously-failed episode is requeued and succeeds, so the
        degraded copy never shadows the enriched result.
        """
        query = (
            Cypher()
            .match("(entity:Entity { raw_capture_for: $episode_uuid })")
            .set("entity.freshness = 'GONE'")
            .return_raw("count(entity) AS updated")
            .build()
        )
        try:
            rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
            return bool(rows and int(rows[0].get("updated", 0)) > 0)
        except (OSError, RuntimeError) as e:
            import logging

            logging.getLogger(__name__).warning(
                "Failed to mark raw-capture superseded for episode %s: %s",
                episode_uuid,
                e,
            )
        return False

    def fetch_exhausted_pending_episodes(
        self, *, max_attempts: int, limit: int = _EXHAUSTED_EPISODE_FETCH_LIMIT
    ) -> list[dict[str, Any]]:
        """Fetch episodes that will be marked as exhausted.

        Bounded: the caller creates one raw-capture entity per row, so an unbounded fetch of
        full episode content feeds an unbounded per-row MERGE. Ordered by ``queued_at`` so
        successive sweeps drain the backlog oldest-first instead of re-reading the same page.

        Returns:
            List of dicts with uuid, content, session_id, user_id, source, namespace.
        """
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'PENDING'",
                "coalesce(toInteger(n.processing_attempts), 0) >= $max_attempts",
            )
            .return_raw(
                "n.uuid AS episode_uuid, n.content AS content, "
                "n.session_id AS session_id, n.user_id AS user_id, "
                "n.source AS source, n.namespace AS namespace"
            )
            .order_by("coalesce(n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        rows = self.neo4j.execute(
            query, params={"max_attempts": max(1, max_attempts), "limit": limit}
        )
        return list(rows) if rows else []
