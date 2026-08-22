"""TemporalRepository — direct Neo4j reads/writes for TEMPORAL :Entity nodes.

TEMPORAL nodes are :Entity nodes with type='TEMPORAL' and a target_date property.
They bypass the Graphiti enrichment pipeline and are written directly via Cypher.

They surface in hook output within a ±30-day window around target_date, and
auto-decay via lifecycle once target_date has passed (compress after 7 days idle,
delete after 30 days idle). Closing a TEMPORAL node sets status='completed',
suppressing it from hook output and triggering fast compress.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


class TemporalRepository:
    """Direct Neo4j CRUD for TEMPORAL :Entity nodes."""

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        user_flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
        turn_evidence_repo: Any | None = None,
        audit_recorder: Any | None = None,
    ) -> dict[str, Any]:
        """Create an open TEMPORAL :Entity node.

        Args:
            content:     The reminder/fact text.
            target_date: ISO date string (YYYY-MM-DD), e.g. "2027-02-16".
            source:      Origin identifier (default: claude-code).
            name:        Short display name; defaults to first 60 chars of content.
            user_flagged: If True, mark as permanently retained (exempt from decay).
            bootstrap_scope: Optional startup pin scope for a flagged memory.
            namespace:   Optional silo to scope this node to. None/"default" resolve
                         to the shared default group, matching the behavior of every
                         other ingestion path (see domain.namespace).
            turn_evidence_uuid: Optional UUID of a :TurnEvidence node for user-tier grounding.
            turn_evidence_repo: Optional TurnEvidenceRepository for fetching evidence (testing).
            audit_recorder: Optional callable to record admission audit (from memory_graph_adapter).

        Returns:
            Dict with uuid, content, target_date, status, created_at.
        """
        import logging
        from menhir.domain.namespace import namespace_to_group_id, stamped_namespace
        from menhir.domain.truth.admission_gate import evaluate_user_tier_claim

        logger_temp = logging.getLogger(__name__)

        # Gate user-tier claims: fetch turn evidence and evaluate.
        # Rewrite source BEFORE persistence so it's never stored at ungrounded high tier.
        # Wrap in try/except so any gate evaluation errors fail closed (downgrade to agent_inference).
        effective_source = source
        verdict = None
        if source.strip().lower() in ("user", "manual"):
            try:
                turn_evidence = None
                if turn_evidence_uuid and turn_evidence_repo:
                    turn_evidence = turn_evidence_repo.fetch_by_uuid(turn_evidence_uuid)
                verdict = evaluate_user_tier_claim(
                    requested_source=source,
                    turn_evidence=turn_evidence,
                    claimed_text=content,
                    session_id=None,  # TEMPORAL has no session_id; rely on namespace only
                    namespace=namespace,
                )
                effective_source = verdict.effective_source
            except Exception as e:
                # Fail closed: any gate evaluation error downgrades to agent_inference
                logger_temp.warning(
                    "Admission gate evaluation failed for TEMPORAL (namespace=%s): %s; downgrading to agent_inference",
                    namespace, e, exc_info=True,
                )
                effective_source = "agent_inference"
                verdict = evaluate_user_tier_claim(
                    requested_source=source,
                    turn_evidence=None,
                    claimed_text=content,
                    session_id=None,
                    namespace=namespace,
                )

        node_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        node_name = (name or content)[:60]
        group_id = namespace_to_group_id(namespace)
        node_namespace = stamped_namespace(namespace)

        self.neo4j.execute(
            """
            CREATE (n:Entity {
                uuid:        $uuid,
                name:        $name,
                summary:     '',
                content:     $content,
                group_id:    $group_id,
                namespace:   $namespace,
                type:        'TEMPORAL',
                target_date: $target_date,
                status:      'open',
                source:      $source,
                scope:       'PERSISTENT',
                user_flagged: $user_flagged,
                bootstrap_scope: $bootstrap_scope,
                created_at:  $now,
                last_accessed: $now,
                freshness:   'ACTIVE',
                edge_count:  0,
                sharpness:   1.0
            })
            """,
            {
                "uuid": node_uuid,
                "name": node_name,
                "content": content,
                "group_id": group_id,
                "namespace": node_namespace,
                "target_date": target_date,
                "source": effective_source,
                "user_flagged": user_flagged,
                "bootstrap_scope": bootstrap_scope,
                "now": now,
            },
        )

        # Audit trail (best-effort): record admission verdict. Never block on audit write.
        if verdict is not None and audit_recorder is not None:
            try:
                audit_recorder(
                    subject="temporal",
                    namespace=namespace,
                    requested_source=source,
                    effective_source=verdict.effective_source,
                    granted=verdict.granted,
                    turn_evidence_uuid=verdict.turn_evidence_uuid,
                    reason=verdict.reason,
                )
            except Exception:
                logger_temp.debug(
                    "Failed to record admission audit for TEMPORAL (namespace=%s); audit trail incomplete (non-fatal)",
                    namespace, exc_info=True,
                )

        return {
            "uuid": node_uuid,
            "name": node_name,
            "content": content,
            "target_date": target_date,
            "status": "open",
            "created_at": now,
        }

    def complete_temporal(self, uuid: str) -> bool:
        """Mark a TEMPORAL node as completed. Returns True if it was open."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid, type: 'TEMPORAL'})
            WHERE n.status = 'open'
            SET n.status = 'completed', n.last_accessed = $now
            RETURN count(n) AS updated
            """,
            {"uuid": uuid, "now": now},
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_in_window(
        self, *, window_days: int = 30, namespace: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return open TEMPORAL nodes whose target_date is within ±window_days of today.

        ``namespace`` is opt-in, matching the sibling ``TodoRepository.list_todos``: omitting it
        lists every silo. Reminders written before they were stamped carry no namespace property,
        so a missing one reads as ``default`` rather than being stranded -- the same coalesce
        idiom the memory queries use.
        """
        safe_window = max(1, min(window_days, 365))
        # CF-44: unbounded before. Every returned row becomes a line in the hook block, so the
        # bound belongs in the query -- trimming in the formatter still pays for transporting
        # and parsing rows nobody will read. `None` keeps the unbounded form for callers that
        # genuinely want the whole window (the reminder tooling), so this narrows the hook, not
        # everything that lists reminders.
        safe_limit = None if limit is None else max(1, min(int(limit), 500))
        bound = "" if safe_limit is None else "\n            LIMIT $limit"
        scope = (
            ""
            if namespace is None
            else "              AND coalesce(n.namespace, 'default') = $namespace\n"
        )
        return self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.type = 'TEMPORAL'
              AND n.status = 'open'
              AND n.target_date IS NOT NULL
              AND date(n.target_date) >= date() - duration({{days: $window}})
              AND date(n.target_date) <= date() + duration({{days: $window}})
{scope}            RETURN
                n.uuid        AS uuid,
                n.name        AS name,
                n.content     AS content,
                n.target_date AS target_date,
                n.status      AS status
            ORDER BY n.target_date ASC{bound}
            """,
            {"window": safe_window, "namespace": namespace, "limit": safe_limit},
        )
