"""Prior-version retirement for the scalar lane (#170 decision 1, counting's rule B).

A turn re-read at a higher perceiver version replaces the readings an older version left on it. A
new reading on the SAME head already supersedes in place (strict rank). These queries cover what that
does not: an old reading the new version did not reproduce (it abstained, or read a different span).

Per page, after the new version stored what it read:

* `*_prior_version_rows` lists the CURRENT agent-tier readings on the page turns ranked strictly
  below the version in effect (read-only).
* `*_current_version_spans` reads back what this version actually stored there (read-only).
* `retire_*_prior_version` is ONE statement. It raises the turn's read marker to this rank, then
  retires the CURRENT lower-rank readings on those turns except `keep_ids`.
  - The marker is what `_RECORD_CYPHER` checks, so a slower lower-version writer cannot make a
    reading current on that turn afterwards.
  - Both statements lock the marker before reading, so the two orders serialize.
* `*_owed` lists what an earlier retire at this rank touched, so a crash between retire and
  rebuild is rebuilt when the page is read again.

Only strictly lower ranks are retired, so a downgrade or a non-SemVer version (rank 0) retires
nothing, and readings of the version in effect are never touched. User-tier rows are never retired.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params

#: Marker key prefixes. One marker per (lane, turn); `_RECORD_CYPHER` builds
#: the key from $episode_uuid.
SCALAR_MARKER_PREFIX = "scalar:"

#: `retired_reason` stamped on retired rows, per lane.
SCALAR_RETIRED_REASON = "scalar_reversion"

_SCALAR_ROW = (
    "a.assertion_id AS assertion_id, a.episode_uuid AS episode_uuid, "
    "a.span_start AS span_start, a.span_end AS span_end, a.subject_uuid AS subject_uuid, "
    "coalesce(a.binding_pending, false) AS binding_pending"
)

# Scalar lane rows: claim ordinal 0 (counting rows are ordinal 1 and have their own rule B).
_SCALAR_SCOPE = (
    tenant_scope_cypher("a")
    + " AND coalesce(a.claim_ordinal, 0) = 0 AND a.episode_uuid IN $turn_ids"
)
_RETIRABLE = (
    " AND coalesce(a.evidence_tier, 'agent') = 'agent'"
    " AND coalesce(a.perceiver_rank, 0) < $rank"
)


def _ids(values: list[str] | None) -> list[str]:
    return [str(v) for v in (values or []) if v]


class PerceptionReversionRepository:
    """Rule-B queries for the scalar (`:TypedAssertion`, ordinal 0) lane."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ---- scalar ----------------------------------------------------------------------------

    def scalar_prior_version_rows(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
    ) -> list[dict[str, Any]]:
        ids = _ids(turn_ids)
        if not ids or perceiver_rank <= 0:
            return []
        rows = self._neo4j.execute(
            "MATCH (:TypedAssertionHead)-[:CURRENT]->(a:TypedAssertion) WHERE "
            + _SCALAR_SCOPE + _RETIRABLE
            + " RETURN " + _SCALAR_ROW
            + " ORDER BY a.episode_uuid, a.span_start, a.assertion_id",
            params={"turn_ids": ids, "rank": int(perceiver_rank),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def scalar_current_version_spans(
        self, namespace: str, turn_ids: list[str], *, perceiver_version: str,
        include_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Bound readings of this version on these turns: current ones (any run, including a
        partial earlier one) and `include_ids`. What a re-read actually stored."""
        ids = _ids(turn_ids)
        if not ids:
            return []
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + _SCALAR_SCOPE
            + " AND a.perceiver_version = $pv AND NOT coalesce(a.binding_pending, false)"
            " AND (EXISTS { MATCH (:TypedAssertionHead)-[:CURRENT]->(a) }"
            "      OR a.assertion_id IN $include)"
            " RETURN " + _SCALAR_ROW
            + " ORDER BY a.episode_uuid, a.span_start, a.assertion_id",
            params={"turn_ids": ids, "pv": str(perceiver_version), "include": _ids(include_ids),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def scalar_owed_subjects(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
    ) -> list[str]:
        """Subjects of readings an earlier retire at this rank took off these turns."""
        ids = _ids(turn_ids)
        if not ids or perceiver_rank <= 0:
            return []
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + _SCALAR_SCOPE
            + " AND a.retired_reason = $reason AND a.retired_by_rank = $rank"
            " AND NOT coalesce(a.binding_pending, false)"
            " RETURN collect(DISTINCT a.subject_uuid) AS subjects",
            params={"turn_ids": ids, "rank": int(perceiver_rank),
                    "reason": SCALAR_RETIRED_REASON, **tenant_scope_params(namespace)},
        )
        return sorted({str(s) for s in ((rows[0]["subjects"] if rows else None) or []) if s})

    def retire_scalar_prior_version(
        self, namespace: str, turn_ids: list[str], *, perceiver_rank: int,
        perceiver_version: str, keep_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Raise the read marker of every page turn to this rank, then retire the CURRENT
        lower-rank scalar readings on them except `keep_ids`. Returns {retired, subjects}; subjects
        excludes pending rows, which have no View."""
        ids = _ids(turn_ids)
        if not ids or perceiver_rank <= 0:
            return {"retired": 0, "subjects": []}
        rows = self._neo4j.execute(
            _MARK_TURNS.replace("__PREFIX__", SCALAR_MARKER_PREFIX)
            + "MATCH (h:TypedAssertionHead)-[cr:CURRENT]->(a:TypedAssertion) WHERE "
            + _SCALAR_SCOPE + _RETIRABLE + _RETIRE_TAIL,
            params={"turn_ids": ids, "rank": int(perceiver_rank), "pv": str(perceiver_version),
                    "keep": _ids(keep_ids), "reason": SCALAR_RETIRED_REASON,
                    **tenant_scope_params(namespace)},
        )
        return _retired(rows)

def _retired(rows: list[Any]) -> dict[str, Any]:
    if not rows:
        return {"retired": 0, "subjects": []}
    subjects = sorted({str(s) for s in (rows[0].get("subjects") or []) if s})
    return {"retired": int(rows[0].get("retired") or 0), "subjects": subjects}


# Lock and raise each page turn's read marker first, in a fixed order (two runners lock in the same
# order). A record statement locks the same marker before it decides CURRENT, so a write either
# commits before this statement reads (and is retired below) or waits and then sees this rank.
_MARK_TURNS = """
UNWIND $turn_ids AS tid
WITH tid ORDER BY tid
MERGE (m:TypedReadMarker {marker_key: '__PREFIX__' + tid})
  ON CREATE SET m.rank = -1, m.turn_id = tid
// The write takes the marker's lock; the rank is read only after it (a concurrent raise is seen).
SET m.read_at = datetime()
WITH m, coalesce(m.rank, -1) AS old
SET m.rank = CASE WHEN old < $rank THEN $rank ELSE old END,
    m.perceiver_version = CASE WHEN old <= $rank THEN $pv ELSE m.perceiver_version END
WITH count(m) AS marked
OPTIONAL """

_RETIRE_SET = """
  AND NOT a.assertion_id IN $keep
SET a.superseded = true, a.expired_at = datetime(), a.retired_reason = $reason,
    a.retired_by_rank = $rank
DELETE cr
"""

_RETIRE_TAIL = _RETIRE_SET + """
RETURN count(a) AS retired,
       collect(DISTINCT CASE WHEN coalesce(a.binding_pending, false) THEN null
                             ELSE a.subject_uuid END) AS subjects
"""


__all__ = [
    "SCALAR_MARKER_PREFIX",
    "SCALAR_RETIRED_REASON",
    "PerceptionReversionRepository",
]
