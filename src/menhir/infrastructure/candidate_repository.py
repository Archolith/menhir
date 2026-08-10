"""CandidateRepository - direct Neo4j reads/writes for CANDIDATE :Entity nodes.

CANDIDATE nodes are :Entity nodes with scope='CANDIDATE'. They are pre-structured
signals (e.g. recurring-friction clusters emitted by cth.painscan) written directly
via Cypher, bypassing the Graphiti enrichment pipeline - exactly like TEMPORAL nodes.

A CANDIDATE is the low-trust "review tier": it is NOT recalled and NOT scored into
context until a human approves it, at which point it is promoted to PERSISTENT scope
(via the normal contradiction-checked promotion path). Rejection deletes it and records
a suppression so the same signal does not silently reappear.

Writes are idempotent on (source, cluster_id): re-emitting the same cluster refreshes
its provenance (evidence strength, distinct-session count, last_seen, notes) instead of
creating a duplicate node. user_flagged is always False - a flagged node would hit the
lifecycle auto-promote shortcut, which skips the contradiction check we require here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

# A candidate's stored notes are capped to keep the node compact and the review
# surface readable; painscan clusters can accrue dozens of evidence rows.
_MAX_NOTES = 12
_MAX_NOTE_LEN = 280


def _cap_notes(notes: list[str] | None) -> list[str]:
    out: list[str] = []
    for note in (notes or [])[:_MAX_NOTES]:
        text = str(note).strip()
        if text:
            out.append(text[:_MAX_NOTE_LEN])
    return out


class CandidateRepository:
    """Direct Neo4j CRUD for CANDIDATE :Entity nodes (the review tier)."""

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_candidate(
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
        """Create or refresh a CANDIDATE :Entity node (idempotent on source+cluster_id).

        Args:
            content:           Human-readable memory/friction text shown on recall after approval.
            source:            Origin identifier (e.g. 'painscan-friction').
            cluster_id:        Stable cluster id from the emitter; the idempotency key with source.
            label:             Short display label for the review surface.
            kind:              'friction' or 'memory' - what the candidate represents.
            candidate_type:    Emitter-side cluster type tag (free-form, e.g. 'tooling').
            type:              MemoryType used after promotion (default SEMANTIC).
            evidence_strength: Ladder value (ANECDOTAL/REPEATED/COMMON).
            distinct_sessions: Number of distinct sessions backing the cluster.
            first_seen/last_seen: ISO timestamps; default to now.
            notes:             Evidence note strings (capped/truncated for storage).
            source_confidence: Prior confidence; candidates stay low-trust until approved.

        Returns:
            Dict with uuid, scope, cluster_id, evidence_strength, distinct_sessions,
            created_at, last_seen, and created (True if a new node was written).
        """
        node_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        node_name = (label or content)[:120]
        capped_notes = _cap_notes(notes)

        rows = self.neo4j.execute(
            """
            MERGE (n:Entity {source: $source, candidate_cluster_id: $cluster_id})
            ON CREATE SET
                n.uuid = $uuid,
                n.name = $name,
                n.summary = '',
                n.content = $content,
                n.group_id = '',
                n.type = $type,
                n.scope = 'CANDIDATE',
                n.candidate_kind = $kind,
                n.candidate_type = $candidate_type,
                n.candidate_label = $label,
                n.candidate_evidence_strength = $evidence_strength,
                n.candidate_distinct_sessions = $distinct_sessions,
                n.candidate_first_seen = $first_seen,
                n.candidate_last_seen = $last_seen,
                n.candidate_notes = $notes,
                n.source_confidence = $source_confidence,
                n.user_flagged = false,
                n.created_at = $now,
                n.last_accessed = $now,
                n.freshness = 'ACTIVE',
                n.edge_count = 0,
                n.sharpness = 0.0
            ON MATCH SET
                n.candidate_evidence_strength = $evidence_strength,
                n.candidate_distinct_sessions = $distinct_sessions,
                n.candidate_last_seen = $last_seen,
                n.candidate_notes = $notes,
                n.last_accessed = $now
            RETURN
                n.uuid AS uuid,
                n.scope AS scope,
                n.candidate_cluster_id AS cluster_id,
                n.candidate_evidence_strength AS evidence_strength,
                n.candidate_distinct_sessions AS distinct_sessions,
                n.created_at AS created_at,
                n.candidate_last_seen AS last_seen
            """,
            {
                "source": source,
                "cluster_id": cluster_id,
                "uuid": node_uuid,
                "name": node_name,
                "content": content,
                "type": type,
                "kind": kind,
                "candidate_type": candidate_type,
                "label": label,
                "evidence_strength": evidence_strength,
                "distinct_sessions": int(distinct_sessions),
                "first_seen": first_seen or now,
                "last_seen": last_seen or now,
                "notes": capped_notes,
                "source_confidence": float(source_confidence),
                "now": now,
            },
        )
        row = dict(rows[0]) if rows else {"uuid": node_uuid, "scope": "CANDIDATE"}
        row["created"] = bool(row.get("created_at") == now)
        return row

    # ------------------------------------------------------------------
    # Approve / reject (review-tier transitions)
    # ------------------------------------------------------------------

    def promote_candidate(self, uuid: str) -> bool:
        """Approve: flip a CANDIDATE node to PERSISTENT scope (now recallable).

        Guarded on scope='CANDIDATE' so this never touches SESSION/PERSISTENT nodes.
        Candidate provenance (source, cluster_id, evidence_strength, ...) is retained
        for traceability. The caller is responsible for running the contradiction
        check first - this method only performs the scope transition.
        """
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            WHERE n.scope = 'CANDIDATE'
            SET n.scope = 'PERSISTENT',
                n.freshness = 'ACTIVE',
                n.promoted_at = datetime(),
                n.last_accessed = $now
            RETURN count(n) AS promoted
            """,
            {"uuid": uuid, "now": now},
        )
        return bool(rows and int(rows[0].get("promoted", 0)) > 0)

    def reject_candidate(self, uuid: str) -> bool:
        """Reject: DETACH DELETE a CANDIDATE node. Returns True if one was removed.

        Guarded on scope='CANDIDATE' so only review-tier nodes can be deleted here.
        Suppression bookkeeping (so a rejected signal does not silently reappear) is
        the caller's responsibility.
        """
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            WHERE n.scope = 'CANDIDATE'
            WITH n, n.uuid AS removed
            DETACH DELETE n
            RETURN count(removed) AS rejected
            """,
            {"uuid": uuid},
        )
        return bool(rows and int(rows[0].get("rejected", 0)) > 0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_candidates(self, *, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Return CANDIDATE nodes for the review surface, newest activity first."""
        safe_limit = max(1, min(int(limit), 500))
        return self.neo4j.execute(
            """
            MATCH (n:Entity)
            WHERE n.scope = 'CANDIDATE'
              AND ($source IS NULL OR n.source = $source)
            RETURN
                n.uuid AS uuid,
                n.name AS name,
                n.content AS content,
                n.source AS source,
                n.candidate_cluster_id AS cluster_id,
                n.candidate_kind AS kind,
                n.candidate_type AS type,
                n.candidate_label AS label,
                n.candidate_evidence_strength AS evidence_strength,
                n.candidate_distinct_sessions AS distinct_sessions,
                n.candidate_first_seen AS first_seen,
                n.candidate_last_seen AS last_seen,
                n.candidate_notes AS notes,
                n.created_at AS created_at
            ORDER BY coalesce(n.candidate_last_seen, n.created_at) DESC
            LIMIT $limit
            """,
            {"source": source, "limit": safe_limit},
        )

    def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        """Return a single CANDIDATE node by uuid, or None if not a candidate."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {uuid: $uuid})
            WHERE n.scope = 'CANDIDATE'
            RETURN
                n.uuid AS uuid,
                n.name AS name,
                n.content AS content,
                n.source AS source,
                n.candidate_cluster_id AS cluster_id,
                n.candidate_kind AS kind,
                n.candidate_type AS type,
                n.candidate_label AS label,
                n.candidate_evidence_strength AS evidence_strength,
                n.candidate_distinct_sessions AS distinct_sessions,
                n.candidate_first_seen AS first_seen,
                n.candidate_last_seen AS last_seen,
                n.candidate_notes AS notes,
                n.created_at AS created_at
            """,
            {"uuid": uuid},
        )
        return dict(rows[0]) if rows else None
