"""Episode intake: PENDING-episode creation and evidence-projection idempotency lookup.

Mixin of :class:`menhir.infrastructure.episode_lifecycle.EpisodeLifecycleRepository`; moved
verbatim from that module, which still re-exports the composed class.
"""

from __future__ import annotations

from datetime import datetime

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params


class _EpisodeIntakeMixin:
    """Creation of pending episodes and lookup of existing evidence projections."""

    def create_pending_episode(
        self,
        *,
        episode_uuid: str,
        name: str,
        content: str,
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        diff: str | None = None,
        user_flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str = "default",
        reference_time: datetime | None = None,
    ) -> str:
        self.neo4j.execute(
            """
            // CF-158 criterion 1, sibling label. `:Episodic.uuid` has no uniqueness constraint
            // either -- the duplicate census in that finding counted BOTH labels -- so this
            // carries the identical hazard as the TEMPORAL `:Entity` writes. Fixing one and
            // leaving this one is trap T26 exactly: the instance gets fixed, the class does not.
            MERGE (n:Episodic {uuid: $episode_uuid})
            ON CREATE SET
                n.name = $name,
                n.type = 'EPISODIC',
                n.scope = 'SESSION',
                n.content = $content,
                n.diff = $diff,
                n.source = $source,
                n.source_confidence = $source_confidence,
                n.user_flagged = $user_flagged,
                n.bootstrap_scope = $bootstrap_scope,
                n.session_id = $session_id,
                n.user_id = $user_id,
                n.namespace = $namespace,
                n.created_at = datetime(),
                n.last_accessed = datetime(),
                n.sharpness = 0.0,
                n.edge_count = 0,
                n.processing_state = 'PENDING',
                n.processing_stage = 'queued',
                n.processing_progress = 0.0,
                n.processing_steps_total = 5,
                n.processing_steps_completed = 0,
                n.processing_llm_tasks_attempt = 0,
                n.processing_llm_tasks_total = 0,
                n.processing_llm_last_task_at = null,
                n.processing_attempts = 0,
                n.queued_at = datetime(),
                n.reference_time = $reference_time,
                n.processing_owner = null,
                n.processing_lease_expires_at = null,
                n.processing_heartbeat_at = datetime(),
                n.processing_error = null,
                n.resolved_episode_uuid = null,
                n.enrichment_priority = 'P1',
                n.enriched_nodes_touched = 0,
                n.enriched_edges_touched = 0
            """,
            params={
                "episode_uuid": episode_uuid,
                "name": name,
                "content": content,
                "diff": diff,
                "session_id": session_id,
                "user_id": user_id,
                "source": source,
                "source_confidence": source_confidence,
                "user_flagged": user_flagged,
                "bootstrap_scope": bootstrap_scope,
                "namespace": namespace,
                "reference_time": reference_time,
            },
        )
        return episode_uuid

    def find_pending_evidence_projection_uuid(
        self, *, turn_evidence_uuid: str, namespace: str | None = None
    ) -> str | None:
        """Return the existing pending projection for an idempotent admission retry."""

        rows = self.neo4j.execute(
            """
            MATCH (p:Episodic {evidence_projection_of: $turn_evidence_uuid})
            WHERE coalesce(p.is_evidence_projection, false)
              AND p.processing_state = 'PENDING'
              AND """ + tenant_scope_cypher("p") + """
            RETURN p.uuid AS uuid
            ORDER BY coalesce(p.queued_at, p.created_at) ASC, p.uuid ASC
            LIMIT 1
            """,
            params={
                "turn_evidence_uuid": turn_evidence_uuid,
                **tenant_scope_params(namespace),
            },
        )
        if not rows or not str(rows[0].get("uuid") or "").strip():
            return None
        return str(rows[0]["uuid"])
