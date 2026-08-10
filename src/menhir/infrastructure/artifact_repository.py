"""ArtifactRepository — direct Neo4j reads/writes for L4 institutional artifacts.

The menhir-side store for the L4 slice (bench spec: archolith_bench/l4; plan:
.agent/plans/l4-artifact-loop-v0.md). An artifact is an :Entity node carrying
artifact_* fields, written directly via Cypher exactly like the CANDIDATE review tier
and TEMPORAL nodes — bypassing the Graphiti enrichment pipeline. It reuses the existing
scope machinery rather than inventing a parallel one:

    artifact_status=trusted    -> scope PERSISTENT   (recallable as fact)
    artifact_status=candidate  -> scope CANDIDATE    (review tier)
    artifact_status=historical -> scope PERSISTENT, superseded (kept, never deleted)

The one genuinely new structure is FIRST-CLASS Evidence: each artifact is linked to
:Evidence nodes by a SUPPORTED_BY edge, so evidence is a node you can query and audit,
not a loose note string. Supersession adds a (new)-[:SUPERSEDES]->(old) edge and flips
the old artifact to historical; it never deletes (invariant 7).

This repository is the single writer. It applies the already-decided status from the
R9-lite policy (domain.artifacts.decide_status) — it does not itself decide trust — but
it does enforce the promotion guard in Cypher as defense-in-depth: an evidence-less
artifact cannot be flipped to trusted at the write boundary (invariant 3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from menhir.domain.artifacts import (
    ArtifactSource,
    ArtifactStatus,
    decide_status,
    has_promotable_evidence,
)

_MAX_SUMMARY_LEN = 280


def _policy_clamped_status(*, requested: str, source: str, evidence_rows: list[dict[str, Any]]) -> str:
    """Boundary guard: never persist a 'trusted' status the create-time policy wouldn't grant.

    The repository is the single graph writer, so it enforces the trust policy itself as
    defense-in-depth — not just ArtifactService. Raw flexibility (a caller passing
    status='trusted' directly) cannot mint trust: an LLM-sourced artifact is never trusted
    on create (inv. 4) and a human one is trusted only with >=1 PROMOTABLE evidence anchor
    (inv. 5; agent_inference alone never suffices). This only ever DOWNGRADES trusted ->
    candidate; an explicit 'candidate' request is honored as-is.
    """
    if requested != ArtifactStatus.TRUSTED.value:
        return requested
    promotable = has_promotable_evidence(str(ev.get("kind", "")) for ev in evidence_rows)
    try:
        policy = decide_status(source=ArtifactSource(source), has_promotable_evidence=promotable)
    except ValueError:
        policy = ArtifactStatus.CANDIDATE  # unknown source -> safe default
    return requested if policy is ArtifactStatus.TRUSTED else ArtifactStatus.CANDIDATE.value


def _evidence_rows(evidence: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalize evidence dicts for the Cypher UNWIND/FOREACH (kind+ref are the key)."""
    out: list[dict[str, Any]] = []
    for ev in evidence or []:
        kind = str(ev.get("kind", "")).strip()
        ref = str(ev.get("ref", "")).strip()
        if not kind or not ref:
            continue
        out.append({
            "uuid": str(uuid4()),
            "kind": kind,
            "ref": ref,
            "directness": float(ev.get("directness", 1.0)),
            "note": ev.get("note"),
            "is_structural": kind in ("git", "test"),
        })
    return out


class ArtifactRepository:
    """Direct Neo4j CRUD for L4 artifact :Entity nodes + first-class :Evidence."""

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        summary: str,
        source: str,
        status: str,
        body: str = "",
        evidence: list[dict[str, Any]] | None = None,
        anchors: list[str] | None = None,
        source_confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Create or refresh an artifact :Entity (idempotent on artifact_id) and link its
        first-class :Evidence nodes via SUPPORTED_BY.

        `status` is the policy decision (domain.artifacts.decide_status) — one of
        'candidate' or 'trusted'; the node scope is derived from it. Evidence is matched
        on (artifact_id, kind, ref) so re-emitting the same artifact does not duplicate
        evidence. FOREACH is used so an evidence-less artifact still writes cleanly.

        ON MATCH deliberately does NOT touch artifact_status or scope: a re-capture refreshes
        provenance only, so it can neither resurrect a HISTORICAL artifact nor silently
        re-trust one (invariant 7). Status transitions go through promote/supersede alone.
        """
        node_uuid = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        ev_rows = _evidence_rows(evidence)
        # Enforce the create-time trust policy at the write boundary (inv. 4 + 5), so raw
        # repository/adapter access cannot forge trust by passing status='trusted'.
        status = _policy_clamped_status(requested=status, source=source, evidence_rows=ev_rows)
        scope = "CANDIDATE" if status == "candidate" else "PERSISTENT"

        rows = self.neo4j.execute(
            """
            MERGE (a:Entity {artifact_id: $artifact_id})
            ON CREATE SET
                a.uuid = $uuid,
                a.name = $name,
                a.summary = $summary,
                a.content = $body,
                a.group_id = '',
                a.type = 'SEMANTIC',
                a.scope = $scope,
                a.is_artifact = true,
                a.artifact_type = $artifact_type,
                a.artifact_status = $status,
                a.artifact_source = $source,
                a.artifact_anchors = $anchors,
                a.source_confidence = $source_confidence,
                a.user_flagged = false,
                a.created_at = $now,
                a.last_accessed = $now,
                a.freshness = 'ACTIVE',
                a.edge_count = 0,
                a.sharpness = 0.0
            ON MATCH SET
                a.summary = $summary,
                a.artifact_anchors = $anchors,
                a.last_accessed = $now
            FOREACH (ev IN $evidence |
                MERGE (e:Evidence {artifact_id: $artifact_id, kind: ev.kind, ref: ev.ref})
                ON CREATE SET
                    e.uuid = ev.uuid,
                    e.directness = ev.directness,
                    e.note = ev.note,
                    e.is_structural = ev.is_structural,
                    e.created_at = $now
                MERGE (a)-[:SUPPORTED_BY]->(e)
            )
            WITH a
            OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(e:Evidence)
            RETURN
                a.uuid AS uuid,
                a.scope AS scope,
                a.artifact_status AS status,
                a.created_at AS created_at,
                count(e) AS evidence_count
            """,
            {
                "artifact_id": artifact_id,
                "uuid": node_uuid,
                "name": (summary or artifact_id)[:120],
                "summary": (summary or "")[:_MAX_SUMMARY_LEN],
                "body": body or "",
                "scope": scope,
                "artifact_type": artifact_type,
                "status": status,
                "source": source,
                "anchors": list(anchors or []),
                "source_confidence": float(source_confidence),
                "evidence": ev_rows,
                "now": now,
            },
        )
        row = dict(rows[0]) if rows else {"uuid": node_uuid, "scope": scope, "status": status}
        row["created"] = bool(row.get("created_at") == now)
        return row

    def promote_artifact(self, artifact_id: str, *, trusted_confidence: float = 0.9) -> bool:
        """Flip a CANDIDATE artifact to TRUSTED (scope PERSISTENT). Fail-closed: refuses
        unless the artifact has at least one PROMOTABLE SUPPORTED_BY :Evidence — an
        agent_inference-only artifact (LLM self-evidence) can NOT be trusted (invariant 3
        + 4). The caller (ArtifactService) checks this too; the Cypher guard is defense-in-
        depth. Promotion also lifts source_confidence off the neutral candidate default."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (a:Entity {artifact_id: $artifact_id})
            WHERE a.scope = 'CANDIDATE'
              AND a.artifact_status = 'candidate'
              AND EXISTS { MATCH (a)-[:SUPPORTED_BY]->(e:Evidence) WHERE e.kind <> 'agent_inference' }
            SET a.scope = 'PERSISTENT',
                a.artifact_status = 'trusted',
                a.freshness = 'ACTIVE',
                a.source_confidence = $trusted_confidence,
                a.promoted_at = datetime(),
                a.last_accessed = $now
            RETURN count(a) AS promoted
            """,
            {"artifact_id": artifact_id, "now": now, "trusted_confidence": float(trusted_confidence)},
        )
        return bool(rows and int(rows[0].get("promoted", 0)) > 0)

    def supersede_artifact(self, old_id: str, new_id: str) -> bool:
        """Mark `old_id` historical, link (new)-[:SUPERSEDES]->(old). Never deletes
        (invariant 7). Returns True if the old artifact was found and superseded."""
        now = datetime.now(timezone.utc).isoformat()
        rows = self.neo4j.execute(
            """
            MATCH (old:Entity {artifact_id: $old_id}), (new:Entity {artifact_id: $new_id})
            WHERE old.artifact_id <> new.artifact_id
            SET old.artifact_status = 'historical',
                old.superseded_by = $new_id,
                old.last_accessed = $now,
                new.supersedes = $old_id
            MERGE (new)-[:SUPERSEDES]->(old)
            RETURN count(old) AS superseded
            """,
            {"old_id": old_id, "new_id": new_id, "now": now},
        )
        return bool(rows and int(rows[0].get("superseded", 0)) > 0)

    # ------------------------------------------------------------------
    # Read (the MemoryOracle's data source)
    # ------------------------------------------------------------------

    def find_artifacts(
        self, *, tokens: list[str] | None = None, anchors: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return artifacts matching any structural anchor or any topic token, with their
        evidence collected. Returns historical/candidate too (status intact) — bucketing
        them into fact/hypothesis/stale is the ColdStartBrief's job, not the store's."""
        safe_limit = max(1, min(int(limit), 200))
        rows = self.neo4j.execute(
            """
            MATCH (a:Entity)
            WHERE a.is_artifact = true
              AND (
                any(x IN $anchors WHERE x IN a.artifact_anchors)
                OR any(tok IN $tokens WHERE toLower(a.summary) CONTAINS tok)
              )
            OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(e:Evidence)
            WITH a, collect(CASE WHEN e IS NULL THEN NULL
                                 ELSE {kind: e.kind, ref: e.ref, directness: e.directness,
                                       is_structural: e.is_structural} END) AS evidence
            RETURN
                a.artifact_id AS artifact_id,
                a.artifact_type AS type,
                a.summary AS summary,
                a.content AS body,
                a.artifact_status AS status,
                a.artifact_source AS source,
                a.artifact_anchors AS anchors,
                a.superseded_by AS superseded_by,
                [ev IN evidence WHERE ev IS NOT NULL] AS evidence
            ORDER BY a.artifact_id
            LIMIT $limit
            """,
            {"tokens": list(tokens or []), "anchors": list(anchors or []), "limit": safe_limit},
        )
        return [dict(r) for r in rows]

    def fetch_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        """Return one artifact by id (with evidence), or None if it is not an artifact."""
        rows = self.neo4j.execute(
            """
            MATCH (a:Entity {artifact_id: $artifact_id})
            WHERE a.is_artifact = true
            OPTIONAL MATCH (a)-[:SUPPORTED_BY]->(e:Evidence)
            WITH a, collect(CASE WHEN e IS NULL THEN NULL
                                 ELSE {kind: e.kind, ref: e.ref} END) AS evidence
            RETURN
                a.artifact_id AS artifact_id,
                a.artifact_type AS type,
                a.summary AS summary,
                a.artifact_status AS status,
                a.artifact_source AS source,
                a.artifact_anchors AS anchors,
                a.superseded_by AS superseded_by,
                [ev IN evidence WHERE ev IS NOT NULL] AS evidence
            """,
            {"artifact_id": artifact_id},
        )
        return dict(rows[0]) if rows else None
