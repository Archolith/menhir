"""Episode processing-state transitions: ready, failed, and pending requeue.

Mixin of :class:`menhir.infrastructure.episode_lifecycle.EpisodeLifecycleRepository`; moved
verbatim from that module, which still re-exports the composed class.
"""

from __future__ import annotations

from menhir.infrastructure.cypher import Cypher


class _EpisodeTransitionsMixin:
    """Lease-fenced state transitions for a claimed episode."""

    def mark_episode_ready(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        required_state: str | None = None,
        resolved_episode_uuid: str,
        nodes_touched: int,
        edges_touched: int,
    ) -> bool:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($required_state IS NULL OR n.processing_state = $required_state)",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING'"
                " AND n.processing_owner = $worker_id))",
            )
            .set(
                (
                    "n.processing_state = 'READY'",
                    "n.processing_stage = 'ready'",
                    "n.processing_substage = 'complete'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = 100.0",
                    "n.processing_steps_completed = coalesce(toInteger(n.processing_steps_total), 5)",
                    "n.processing_completed_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    "n.resolved_episode_uuid = $resolved_episode_uuid",
                    "n.enriched_nodes_touched = $nodes_touched",
                    "n.enriched_edges_touched = $edges_touched",
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "worker_id": worker_id,
                "required_state": required_state,
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": nodes_touched,
                "edges_touched": edges_touched,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def mark_episode_failed(
        self,
        episode_uuid: str,
        error: str,
        *,
        worker_id: str | None = None,
        transient_requeue: bool = False,
        claim_started_at: object | None = None,
    ) -> bool:
        # Neo4j temporal equality includes timezone identity: a stored `Z` value round-trips
        # through the Python driver as `[UTC]` and compares unequal despite naming one instant.
        # Epoch seconds + nanoseconds preserve full instant precision for the claim fence.
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "(($worker_id IS NULL AND NOT $transient_requeue)"
                " OR (n.processing_state = 'ENRICHING' AND n.processing_owner = $worker_id"
                " AND (NOT $transient_requeue OR ($claim_started_at IS NOT NULL"
                " AND n.processing_started_at IS NOT NULL"
                " AND n.processing_started_at.epochSeconds"
                " = datetime($claim_started_at).epochSeconds"
                " AND n.processing_started_at.nanosecond"
                " = datetime($claim_started_at).nanosecond))))",
            )
            .set(
                (
                    "n.transient_retries = CASE WHEN $transient_requeue"
                    " THEN coalesce(toInteger(n.transient_retries), 0) + 1"
                    " ELSE n.transient_retries END",
                    "n.processing_attempts = CASE WHEN NOT $transient_requeue"
                    " THEN n.processing_attempts"
                    " WHEN coalesce(toInteger(n.processing_attempts), 0) > 0"
                    " THEN toInteger(n.processing_attempts) - 1 ELSE 0 END",
                    "n.processing_state = 'FAILED'",
                    "n.processing_stage = 'failed'",
                    "n.processing_substage = 'failed'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = CASE"
                    " WHEN n.processing_progress IS NULL THEN 100.0"
                    " ELSE n.processing_progress END",
                    "n.processing_completed_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = $error",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "error": error,
                "worker_id": worker_id,
                "transient_requeue": transient_requeue,
                "claim_started_at": claim_started_at,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def mark_episode_pending(
        self,
        episode_uuid: str,
        *,
        retry_after_s: float = 0.0,
        worker_id: str | None = None,
        transient_requeue: bool = False,
        claim_started_at: object | None = None,
    ) -> bool:
        retry_clause = (
            f"n.retry_after = datetime() + duration({{seconds: {int(max(0, retry_after_s))}}})"
            if retry_after_s > 0
            else "n.retry_after = null"
        )
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "(($worker_id IS NULL AND NOT $transient_requeue)"
                " OR (n.processing_state = 'ENRICHING' AND n.processing_owner = $worker_id"
                " AND (NOT $transient_requeue OR ($claim_started_at IS NOT NULL"
                " AND n.processing_started_at IS NOT NULL"
                " AND n.processing_started_at.epochSeconds"
                " = datetime($claim_started_at).epochSeconds"
                " AND n.processing_started_at.nanosecond"
                " = datetime($claim_started_at).nanosecond))))",
            )
            .set(
                (
                    "n.transient_retries = CASE WHEN $transient_requeue"
                    " THEN coalesce(toInteger(n.transient_retries), 0) + 1"
                    " ELSE n.transient_retries END",
                    "n.processing_attempts = CASE WHEN NOT $transient_requeue"
                    " THEN n.processing_attempts"
                    " WHEN coalesce(toInteger(n.processing_attempts), 0) > 0"
                    " THEN toInteger(n.processing_attempts) - 1 ELSE 0 END",
                    "n.processing_state = 'PENDING'",
                    "n.processing_stage = 'queued'",
                    "n.processing_substage = 'circuit_breaker_requeue'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    retry_clause,
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "worker_id": worker_id,
                "transient_requeue": transient_requeue,
                "claim_started_at": claim_started_at,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)
