"""Episode queue and claim operations: pending listing, transient exhaustion, and claiming.

Mixin of :class:`menhir.infrastructure.episode_lifecycle.EpisodeLifecycleRepository`; moved
verbatim from that module, which still re-exports the composed class.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.infrastructure.cypher import (
    Cypher,
    EPISODE_RETRY_FIELDS,
    LLM_RESET_SET,
)
from menhir.infrastructure.episode_lifecycle_retry import (
    TRANSIENT_RETRY_CAP,
    _RECOVERABLE_CONTEXT_WINDOW_MARKERS,
    is_recoverable_context_window_error,
)

logger = logging.getLogger(__name__)


class _EpisodeClaimMixin:
    """Queue listing, failure triage, artifact reconciliation, and lease acquisition."""

    def list_pending_episode_uuids(self, *, max_attempts: int, limit: int = 100) -> list[str]:
        safe_limit = max(1, min(limit, 500))
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'PENDING'",
                "coalesce(toInteger(n.processing_attempts), 0) < $max_attempts",
                "coalesce(toInteger(n.transient_retries), 0) < $transient_max",
                "coalesce(properties(n)['retry_after'], datetime()) <= datetime()",
            )
            .return_raw("n.uuid AS uuid")
            .order_by("coalesce(n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "limit": safe_limit,
                "max_attempts": max(1, max_attempts),
                "transient_max": TRANSIENT_RETRY_CAP,
            },
        )
        return [str(row["uuid"]) for row in rows if row.get("uuid")]

    def fail_transient_exhausted_pending_episodes(
        self, *, transient_max: int = TRANSIENT_RETRY_CAP
    ) -> int:
        """Park PENDING episodes whose transient requeues hit the cap (#79/#70).

        The genuine-failure sibling (`fail_exhausted_pending_episodes`) can never fire on a
        purely transient history because those attempts are refunded; without this arm a
        permanently dead provider would leave episodes bouncing PENDING forever.
        """
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'PENDING'",
                "coalesce(toInteger(n.transient_retries), 0) >= $transient_max",
            )
            .set(
                (
                    "n.processing_state = 'FAILED'",
                    "n.processing_stage = 'failed'",
                    "n.processing_substage = 'pending_transient_exhausted'",
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
                    " THEN 'pending_transient_exhausted'"
                    " ELSE n.processing_error END",
                )
            )
            .return_raw("count(n) AS failed")
            .build()
        )
        rows = self.neo4j.execute(query, params={"transient_max": max(1, transient_max)})
        return int(rows[0].get("failed", 0)) if rows else 0

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.processing_state = 'FAILED'")
            .return_fields(EPISODE_RETRY_FIELDS)
            .order_by("coalesce(n.processing_completed_at, n.processing_started_at, n.queued_at, n.created_at) ASC")
            .limit()
            .build()
        )
        return self.neo4j.execute(query, params={"limit": safe_limit})

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, Any]]:
        """Group FAILED episodes by their error text, most common first.

        Feeds the queue-health job's stuck-backlog warning. Grouping first keeps the
        classification cost proportional to the number of DISTINCT causes (small) rather
        than to the size of the backlog, which can be hundreds of episodes.

        Classification itself deliberately stays in Python (`classify_enrichment_failure`)
        rather than being reimplemented in Cypher, so the marker lists cannot drift apart.
        """
        safe_limit = max(1, min(limit, 200))
        return self.neo4j.execute(
            """
            MATCH (e:Episodic) WHERE e.processing_state = 'FAILED'
            WITH coalesce(e.processing_error, '(no error recorded)') AS error,
                 count(*) AS count,
                 min(coalesce(e.processing_completed_at, e.created_at)) AS oldest
            RETURN error, count, toString(oldest) AS oldest_at
            ORDER BY count DESC, error ASC
            LIMIT $limit
            """,
            params={"limit": safe_limit},
        )

    def find_completed_episode_artifact(
        self,
        *,
        anchor_uuid: str,
        anchor_name: str,
    ) -> dict[str, Any] | None:
        rows = self.neo4j.execute(
            (
                Cypher()
                .match("(e:Episodic)-[]-(n:Entity)")
                .where("e.uuid <> $anchor_uuid", "e.name = $anchor_name")
                .return_raw("e.uuid AS resolved_episode_uuid, collect(DISTINCT n.uuid) AS entity_uuids")
                .limit("2")
                .build()
            ),
            params={"anchor_uuid": anchor_uuid, "anchor_name": anchor_name},
        )
        if len(rows) > 1:
            logger.warning(
                "Multiple completed episode artifacts found for anchor_name=%s anchor_uuid=%s - skipping reconciliation",
                anchor_name,
                anchor_uuid,
            )
        if len(rows) != 1:
            return None

        resolved_episode_uuid = str(rows[0].get("resolved_episode_uuid") or "")
        entity_uuids = [
            str(entity_uuid)
            for entity_uuid in (rows[0].get("entity_uuids") or [])
            if str(entity_uuid)
        ]
        if not resolved_episode_uuid or not entity_uuids:
            return None

        edge_uuids: list[str] = []
        episode_edge_rows = self.neo4j.execute(
            (
                Cypher()
                .match("(:Episodic {uuid: $episode_uuid})-[r]-()")
                .where("r.uuid IS NOT NULL")
                .return_raw("collect(DISTINCT r.uuid) AS edge_uuids")
                .build()
            ),
            params={"episode_uuid": resolved_episode_uuid},
        )
        if episode_edge_rows:
            edge_uuids.extend(str(uuid) for uuid in (episode_edge_rows[0].get("edge_uuids") or []) if str(uuid))

        entity_edge_rows = self.neo4j.execute(
            (
                Cypher()
                .match("(a:Entity)-[r]-(b:Entity)")
                .where("a.uuid IN $entity_uuids", "b.uuid IN $entity_uuids", "r.uuid IS NOT NULL")
                .return_raw("collect(DISTINCT r.uuid) AS edge_uuids")
                .build()
            ),
            params={"entity_uuids": entity_uuids},
        )
        if entity_edge_rows:
            edge_uuids.extend(str(uuid) for uuid in (entity_edge_rows[0].get("edge_uuids") or []) if str(uuid))

        return {
            "resolved_episode_uuid": resolved_episode_uuid,
            "entity_uuids": entity_uuids,
            "edge_uuids": list(dict.fromkeys(edge_uuids)),
        }

    def claim_pending_episode(
        self,
        episode_uuid: str,
        *,
        max_attempts: int,
        context_retry_attempts: int | None = None,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        precheck_query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $episode_uuid")
            .return_fields(
                (),
                "n.uuid AS uuid",
                "n.processing_state AS processing_state",
                "coalesce(toInteger(n.processing_attempts), 0) AS processing_attempts",
                "n.processing_error AS processing_error",
            )
            .build()
        )
        precheck_rows = self.neo4j.execute(precheck_query, params={"episode_uuid": episode_uuid})
        context_cap = max(max_attempts, int(context_retry_attempts if context_retry_attempts is not None else max_attempts))
        claim_query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "n.processing_state IN ['PENDING', 'FAILED']",
                "coalesce(properties(n)['retry_after'], datetime()) <= datetime()",
                "(coalesce(toInteger(n.processing_attempts), 0) < $max_attempts"
                " OR (n.processing_state = 'FAILED'"
                " AND coalesce(toInteger(n.processing_attempts), 0) < $context_retry_attempts"
                " AND n.processing_error IS NOT NULL"
                " AND ANY(marker IN $context_error_markers"
                " WHERE toLower(n.processing_error) CONTAINS marker)))",
            )
            .set(
                (
                    "n.processing_state = 'ENRICHING'",
                    "n.processing_stage = 'claiming'",
                    "n.processing_substage = 'lease_acquired'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = 5.0",
                    "n.processing_steps_total = coalesce(toInteger(n.processing_steps_total), 5)",
                    "n.processing_steps_completed = 0",
                    "n.processing_llm_tasks_attempt = 0",
                    "n.processing_llm_last_task_at = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    "n.processing_started_at = datetime()",
                    "n.processing_completed_at = null",
                    "n.processing_attempts = coalesce(toInteger(n.processing_attempts), 0) + 1",
                    "n.processing_owner = $worker_id",
                    "n.processing_lease_expires_at = datetime() + duration({seconds: $lease_seconds})",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.retry_after = null",
                )
            )
            .with_clause(
                "n, [(n)-[:ADMITTED_ON]->(t:TurnEvidence) | t] AS subject_turns"
            )
            .return_fields(
                (),
                "n.uuid AS uuid",
                "n.name AS name",
                "n.content AS content",
                "n.source AS source",
                "n.session_id AS session_id",
                "n.user_id AS user_id",
                "n.user_flagged AS user_flagged",
                "n.bootstrap_scope AS bootstrap_scope",
                "n.diff AS diff",
                # Subject-endpoint eligibility is decided only from facts returned by this same
                # lease-acquiring query.  Do not reconstruct it later from source='user': that
                # proves at most episode authorship and loses projection lineage and cardinality.
                "coalesce(n.is_evidence_projection, false) AS is_evidence_projection",
                "n.evidence_projection_of AS evidence_projection_of",
                "size(subject_turns) AS turn_evidence_count",
                "subject_turns[0].role AS turn_evidence_role",
                "subject_turns[0].declarant AS turn_evidence_declarant",
                "subject_turns[0].text AS turn_evidence_text",
                "subject_turns[0].namespace AS turn_evidence_namespace",
                "(coalesce(n.is_evidence_projection, false)"
                " AND size(subject_turns) = 1"
                " AND subject_turns[0].role = 'user'"
                " AND subject_turns[0].declarant = 'user'"
                " AND n.content = subject_turns[0].text"
                " AND n.evidence_projection_of = subject_turns[0].turn_id"
                " AND n.diff IS NULL"
                " AND CASE WHEN trim(coalesce(n.namespace, '')) = '' THEN 'default'"
                " ELSE trim(n.namespace) END"
                " = CASE WHEN trim(coalesce(subject_turns[0].namespace, '')) = ''"
                " THEN 'default' ELSE trim(subject_turns[0].namespace) END)"
                " AS subject_endpoint_eligible",
                # Carry the stored namespace so the enrichment worker writes the
                # extracted entity/edge nodes into the episode's graphiti group_id.
                # Without this the claim returns no namespace, enrichment defaults to
                # the "default" group (group_id ""), and namespace-scoped recall finds
                # nothing (candidates_evaluated=0) -- breaking namespace isolation on
                # the async ingest path.
                "n.namespace AS namespace",
                # Carry the caller-supplied world time (`occurred_at` -> `reference_time`).
                # Without it the claim returns no reference_time, and the enrichment handoff
                # `ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")` silently
                # falls back to the QUEUE time -- so every backdated episode is stamped with its
                # ingestion time in graphiti, and supersession then orders history by when it was
                # imported rather than when it happened. Same failure shape as the namespace bug
                # noted above: stored correctly, projected nowhere.
                # Carry the caller-supplied world time (`occurred_at` -> `reference_time`).
                # Without it the claim returns no reference_time, and the enrichment handoff
                # `ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")` silently
                # falls back to the QUEUE time -- so every backdated episode is stamped with its
                # ingestion time in graphiti, and supersession then orders history by when it was
                # imported rather than when it happened. Same failure shape as the namespace bug
                # noted above: stored correctly, projected nowhere.
                "n.reference_time AS reference_time",
                "n.queued_at AS queued_at",
                # The episode-to-evidence link identifies the current raw transcript turn. A
                # relationless corrective extraction can use it to retrieve bounded adjacent
                # dialogue without putting assistant context into the persisted episode body.
                "subject_turns[0].turn_id AS turn_evidence_uuid",
                "n.processing_attempts AS processing_attempts",
                "n.processing_owner AS processing_owner",
                "n.processing_started_at AS processing_started_at",
                "n.processing_lease_expires_at AS processing_lease_expires_at",
            )
            .build()
        )
        rows = self.neo4j.execute(
            claim_query,
            params={
                "episode_uuid": episode_uuid,
                "max_attempts": max_attempts,
                "context_retry_attempts": context_cap,
                # Retry-budget gate inside the claim query: only operator-clearable errors.
                "context_error_markers": list(_RECOVERABLE_CONTEXT_WINDOW_MARKERS),
                "worker_id": worker_id,
                "lease_seconds": max(1, lease_seconds),
            },
        )
        if rows:
            return rows[0]

        if not precheck_rows:
            logger.debug("claim_pending_episode skipped uuid=%s reason=missing", episode_uuid)
            return None

        current = precheck_rows[0]
        state = str(current.get("processing_state") or "")
        attempts = int(current.get("processing_attempts") or 0)
        error = str(current.get("processing_error") or "")
        # Retry-budget decision: only the operator-clearable variant earns the extended cap.
        context_window_error = is_recoverable_context_window_error(error)
        if state not in {"PENDING", "FAILED"}:
            reason = f"state={state or 'unknown'}"
        elif attempts >= max_attempts and not (
            state == "FAILED" and attempts < context_cap and context_window_error
        ):
            reason = f"attempts_exhausted attempts={attempts} max={max_attempts}"
        else:
            reason = "predicate_miss"
        logger.debug("claim_pending_episode skipped uuid=%s reason=%s", episode_uuid, reason)
        return None
