"""Related-count matching storage (counting plan unit 4R).

Decisions live as `count_*` properties on the occurrence :TypedAssertion (owner decision 2026-10-17),
so every delete path that removes an assertion removes its decision too. Only the counting runner
writes them, through `record_count_decisions`, which re-checks the counting lease in the same
statement: a run that lost its fence writes nothing.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params

_OCCURRENCE_FIELDS = """
       a.assertion_id AS assertion_id, a.subject_uuid AS subject_uuid,
       a.attribute AS attribute, a.value_kind AS value_kind, a.unit AS unit,
       a.value AS value, toString(a.valid_at) AS valid_at,
       a.episode_uuid AS episode_uuid, a.perceiver_version AS perceiver_version,
       coalesce(a.count_object, '') AS count_object, coalesce(a.count_item, '') AS count_item,
       coalesce(a.count_action, '') AS count_action, coalesce(a.count_status, '') AS count_status,
       coalesce(a.count_target_id, '') AS count_target_id,
       coalesce(a.count_family, '') AS count_family,
       coalesce(a.count_attempts, 0) AS count_attempts
"""

#: a current (any version), bound, counting-lane occurrence.
_LIVE_OCCURRENCE = """
      a.operation = 'occurrence' AND coalesce(a.claim_ordinal, 0) = 1
      AND NOT coalesce(a.superseded, false) AND NOT coalesce(a.binding_pending, false)
"""

#: ... stamped by the counting version in effect.
_CURRENT_OCCURRENCE = _LIVE_OCCURRENCE + " AND a.perceiver_version = $pv\n"


class CountDecisionRepository:
    """Reads and fenced writes for occurrence count decisions."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    def occurrences_by_ids(
        self, namespace: str, assertion_ids: list[str], *, perceiver_version: str,
    ) -> list[dict[str, Any]]:
        """The current, bound occurrences among `assertion_ids` (the page's new candidates)."""
        ids = [str(i) for i in assertion_ids if i]
        if not ids:
            return []
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE a.assertion_id IN $ids AND "
            + tenant_scope_cypher("a") + " AND " + _CURRENT_OCCURRENCE
            + " RETURN " + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC",
            params={"ids": ids, "pv": str(perceiver_version), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def counted_occurrences_for_subjects(
        self, namespace: str, subject_uuids: list[str], *, limit: int,
    ) -> list[dict[str, Any]]:
        """Current counted occurrences of these subjects (the related-pull pool), oldest first.
        Any counting version: during a re-version, a counted row on a turn not yet re-read is still
        in the fold, so a new row must relate to it or the happening could count twice."""
        subjects = [str(s) for s in subject_uuids if s]
        if not subjects:
            return []
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE a.subject_uuid IN $subjects AND "
            + tenant_scope_cypher("a") + " AND " + _LIVE_OCCURRENCE
            + " AND a.count_status = 'counted' RETURN " + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit",
            params={"subjects": subjects, "limit": int(limit), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def undecided_occurrences(
        self, namespace: str, *, perceiver_version: str, limit: int,
    ) -> list[dict[str, Any]]:
        """Current, bound occurrences with no decision yet (a crash between persist and decide, or a
        binding repaired later), oldest first."""
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + tenant_scope_cypher("a") + " AND "
            + _CURRENT_OCCURRENCE + " AND a.count_status IS NULL RETURN " + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit",
            params={"pv": str(perceiver_version), "limit": int(limit),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def record_count_decisions(
        self, namespace: str, decisions: list[dict[str, Any]], *, perceiver_version: str,
        run_id: str, incarnation: str, epoch: int,
    ) -> dict[str, Any]:
        """Write decisions onto their occurrences, only while this run holds the counting fence
        (checked in the same statement). A decision applies only to a current occurrence of this
        namespace and version, and never replaces an existing decision unless it says `redecide`.
        Returns {"fence_ok", "written"}."""
        payload = [
            {
                "assertion_id": str(d["assertion_id"]),
                "status": str(d["status"]),
                "target_id": str(d.get("target_id") or "") or None,
                "family": str(d.get("family") or "") or None,
                "vote": str(d.get("vote") or "") or None,
                "reason": str(d.get("reason") or "") or None,
                "truncated": bool(d.get("truncated", False)),
                "redecide": bool(d.get("redecide", False)),
            }
            for d in decisions
        ]
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (w:CountingConsolidationWatermark {group_id: $ns})
            WHERE w.owner = $run_id AND w.incarnation = $inc AND w.epoch = $epoch
            WITH w IS NOT NULL AS fence_ok
            CALL {
                WITH fence_ok
                UNWIND CASE WHEN fence_ok THEN $decisions ELSE [] END AS d
                MATCH (a:TypedAssertion {assertion_id: d.assertion_id})
                WHERE """ + tenant_scope_cypher("a") + " AND " + _CURRENT_OCCURRENCE + """
                      AND (a.count_status IS NULL OR d.redecide)
                SET a.count_status = d.status, a.count_target_id = d.target_id,
                    a.count_family = d.family, a.count_vote = d.vote, a.count_reason = d.reason,
                    a.count_truncated = d.truncated, a.count_decided_at = datetime(),
                    a.count_attempts = coalesce(a.count_attempts, 0) + 1,
                    a.count_run_id = $run_id
                RETURN count(a) AS written
            }
            RETURN fence_ok, written
            """,
            params={"ns": namespace, "run_id": str(run_id), "inc": str(incarnation),
                    "epoch": int(epoch), "pv": str(perceiver_version), "decisions": payload,
                    **tenant_scope_params(namespace)},
        )
        if not rows:
            return {"fence_ok": False, "written": 0}
        return {"fence_ok": bool(rows[0]["fence_ok"]), "written": int(rows[0]["written"] or 0)}


__all__ = ["CountDecisionRepository"]
