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

from menhir.domain.secret_scrub import ensure_scrubbed
from menhir.domain.namespace import (
    namespace_to_group_id,
    stamped_namespace,
    tenant_scope_cypher,
    tenant_scope_params,
)
from typing import Any
from uuid import uuid4

# A candidate's stored notes are capped to keep the node compact and the review
# surface readable; painscan clusters can accrue dozens of evidence rows.
_MAX_NOTES = 12
_MAX_NOTE_LEN = 280

# The MATCH above it reads without a lock, and Neo4j does not re-check a WHERE after a later
# write waits for one. Without this, a reject queued behind an approve deletes the memory the
# approve just made PERSISTENT. Writing a throwaway property takes the node's write lock; the
# scope is then read again under it. tests/test_candidate_review_431.py races both orders.
_REVIEW_LOCK = """SET n._candidate_review_lock = true
            REMOVE n._candidate_review_lock
            WITH n
            WHERE n.scope = 'CANDIDATE'"""


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
        namespace: str | None = None,
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
            namespace:         Silo for the candidate. Opt-in: omitted keeps the historical
                               unscoped behavior exactly, including the '' group_id.

        The namespace joins the MERGE KEY, not just the properties, and that is the point.
        The key was (source, cluster_id) alone, so two silos emitting the same cluster id from
        the same source landed on ONE node -- and `ON MATCH SET` then let each rewrite the
        other's `candidate_evidence_strength`, `candidate_distinct_sessions` and
        `candidate_notes`. Those are the fields a promotion decision reads, so the collision
        was not merely untidy: one silo's activity could push another silo's candidate up the
        evidence ladder toward approval.

        It is added to the key only when supplied, so existing nodes (which carry no
        `namespace` property) still match their own key and are not duplicated. There is no
        migration here and none is needed.

        Returns:
            Dict with uuid, scope, cluster_id, evidence_strength, distinct_sessions,
            created_at, last_seen, and created (True if a new node was written).
        """
        content = ensure_scrubbed(content)
        notes = [ensure_scrubbed(str(n)) for n in notes] if notes else notes
        node_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        node_name = (label or content)[:120]
        capped_notes = _cap_notes(notes)

        ns = str(namespace).strip() if namespace is not None else ""
        merge_key = "{source: $source, candidate_cluster_id: $cluster_id}"
        if ns:
            merge_key = (
                "{source: $source, candidate_cluster_id: $cluster_id, namespace: $namespace}"
            )
        rows = self.neo4j.execute(
            f"""
            MERGE (n:Entity {merge_key})
            ON CREATE SET
                n.uuid = $uuid,
                n.name = $name,
                n.summary = '',
                n.content = $content,
                n.group_id = $group_id,
                n.namespace = $stamped_namespace,
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
                n.created_at = datetime($now),
                n.last_accessed = datetime($now),
                n.freshness = 'ACTIVE',
                n.edge_count = 0,
                n.sharpness = 0.0
            ON MATCH SET
                n.candidate_evidence_strength = $evidence_strength,
                n.candidate_distinct_sessions = $distinct_sessions,
                n.candidate_last_seen = $last_seen,
                n.candidate_notes = $notes,
                n.last_accessed = datetime($now)
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
                "namespace": ns,
                # `namespace_to_group_id` maps both unspecified and 'default' onto graphiti's
                # default partition (''), which is exactly the hardcoded value this replaces --
                # so an unscoped write lands where it always did.
                "group_id": namespace_to_group_id(ns or None),
                "stamped_namespace": stamped_namespace(ns or None),
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
        row["created"] = row.get("uuid") == node_uuid
        return row

    # ------------------------------------------------------------------
    # Approve / reject (review-tier transitions)
    # ------------------------------------------------------------------

    def promote_candidate(
        self,
        uuid: str,
        *,
        namespace: str | None = None,
        name_embedding: list[float] | None = None,
    ) -> bool:
        """Approve: flip a CANDIDATE node to PERSISTENT scope (now recallable).

        Guarded on scope='CANDIDATE' so this never touches SESSION/PERSISTENT nodes.
        Candidate provenance (source, cluster_id, evidence_strength, ...) is retained
        for traceability. The caller is responsible for running the contradiction
        check first - this method only performs the scope transition.

        ``namespace`` (opt-in) is checked in the same statement as the write, so a scoped caller
        cannot promote another silo's candidate. ``name_embedding`` is written with the scope flip:
        candidates are created without one, the property vector recall reads (#431). An empty
        ``summary`` takes the candidate's content (first 1000 chars): Graphiti's full-text index
        covers ``name`` and ``summary`` only, and recall shows the summary.
        """
        now = datetime.now(timezone.utc).isoformat()
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $uuid}})
            WHERE n.scope = 'CANDIDATE'
              AND {tenant_scope_cypher("n")}
            {_REVIEW_LOCK}
            SET n.scope = 'PERSISTENT',
                n.freshness = 'ACTIVE',
                n.promoted_at = datetime(),
                n.last_accessed = datetime($now),
                n.summary = CASE WHEN trim(coalesce(n.summary, '')) = ''
                                 THEN left(coalesce(n.content, ''), 1000) ELSE n.summary END
            FOREACH (_ IN CASE WHEN $emb IS NULL THEN [] ELSE [1] END |
                SET n.name_embedding = $emb)
            RETURN count(n) AS promoted
            """,
            {"uuid": uuid, "now": now, "emb": name_embedding, **tenant_scope_params(ns or None)},
        )
        return bool(rows and int(rows[0].get("promoted", 0)) > 0)

    def reject_candidate(self, uuid: str, *, namespace: str | None = None) -> bool:
        """Reject: DETACH DELETE a CANDIDATE node. Returns True if one was removed.

        Guarded on scope='CANDIDATE' so only review-tier nodes can be deleted here, and on the
        caller's silo when ``namespace`` is supplied. Suppression bookkeeping (so a rejected
        signal does not silently reappear) is the caller's responsibility.
        """
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $uuid}})
            WHERE n.scope = 'CANDIDATE'
              AND {tenant_scope_cypher("n")}
            {_REVIEW_LOCK}
            WITH n, n.uuid AS removed
            DETACH DELETE n
            RETURN count(removed) AS rejected
            """,
            {"uuid": uuid, **tenant_scope_params(ns or None)},
        )
        return bool(rows and int(rows[0].get("rejected", 0)) > 0)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_candidates(
        self, *, source: str | None = None, limit: int = 100, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return CANDIDATE nodes for the review surface, newest activity first.

        ``namespace`` is opt-in per the namespace contract: ``None``/empty/whitespace does not
        filter, so every existing caller reads every silo exactly as before. Supplied, it narrows
        to that silo through the shared ``tenant_scope_cypher`` predicate. Each row reports the
        silo it belongs to, so an unscoped reviewer can tell them apart.
        """
        safe_limit = max(1, min(int(limit), 500))
        ns = str(namespace).strip() if namespace is not None else ""
        return self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.scope = 'CANDIDATE'
              AND ($source IS NULL OR n.source = $source)
              AND {tenant_scope_cypher("n")}
            RETURN
                n.uuid AS uuid,
                n.name AS name,
                n.content AS content,
                n.source AS source,
                coalesce(n.namespace, n.group_id) AS namespace,
                n.candidate_cluster_id AS cluster_id,
                n.candidate_kind AS kind,
                n.candidate_type AS type,
                n.type AS memory_type,
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
            {"source": source, "limit": safe_limit, **tenant_scope_params(ns or None)},
        )

    def fetch_candidate(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        """Return a single CANDIDATE node by uuid, or None if not a candidate.

        ``namespace`` is opt-in: supplied, a candidate in another silo reads as None.
        """
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{uuid: $uuid}})
            WHERE n.scope = 'CANDIDATE'
              AND {tenant_scope_cypher("n")}
            RETURN
                n.uuid AS uuid,
                n.name AS name,
                n.content AS content,
                n.source AS source,
                coalesce(n.namespace, n.group_id) AS namespace,
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
            {"uuid": uuid, **tenant_scope_params(ns or None)},
        )
        return dict(rows[0]) if rows else None
