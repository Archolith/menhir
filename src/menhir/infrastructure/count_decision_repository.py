"""Related-count matching storage (counting plan unit 4R).

Decisions live as `count_*` properties on the occurrence :TypedAssertion (owner decision 2026-10-17),
so every delete path that removes an assertion removes its decision too. Only the counting runner
writes them, through `record_count_decisions`, which re-checks the counting lease in the same
statement: a run that lost its fence writes nothing.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import normalize_namespace, tenant_scope_cypher, tenant_scope_params

_OCCURRENCE_FIELDS = """
       a.assertion_id AS assertion_id, a.subject_uuid AS subject_uuid,
       a.attribute AS attribute, a.value_kind AS value_kind, a.unit AS unit,
       a.value AS value, toString(a.valid_at) AS valid_at,
       a.episode_uuid AS episode_uuid, a.perceiver_version AS perceiver_version,
       coalesce(a.stated_span, '') AS stated_span,
       coalesce(a.count_object, '') AS count_object, coalesce(a.count_item, '') AS count_item,
       coalesce(a.count_action, '') AS count_action, coalesce(a.count_status, '') AS count_status,
       coalesce(a.count_target_id, '') AS count_target_id,
       coalesce(a.count_family, '') AS count_family,
       coalesce(a.count_attempts, 0) AS count_attempts
"""


def _live(v: str) -> str:
    """A current (any version), bound, counting-lane occurrence."""
    return (f" {v}.operation = 'occurrence' AND coalesce({v}.claim_ordinal, 0) = 1"
            f" AND NOT coalesce({v}.superseded, false)"
            f" AND NOT coalesce({v}.binding_pending, false) ")


def _current(v: str) -> str:
    """... stamped by the counting version in effect ($pv)."""
    return _live(v) + f" AND {v}.perceiver_version = $pv "


_LIVE_OCCURRENCE = _live("a")
_CURRENT_OCCURRENCE = _current("a")


def _counted(v: str) -> str:
    """Counted in the match fold: decided `counted`, or undecided under a pre-match version
    (`status_of`'s rule), so a carried pre-match row is still a target a retelling relates to."""
    return (f" ({v}.count_status = 'counted' OR ({v}.count_status IS NULL"
            f" AND NOT coalesce({v}.perceiver_version, '+m') ENDS WITH '+m')) ")


#: no decision yet (the undecided sweep's set).
_UNDECIDED = " a.count_status IS NULL "
#: last decided at least $cooldown seconds ago (the quiet-namespace re-decide gate).
_COOLED = (" AND (a.count_decided_at IS NULL"
           " OR a.count_decided_at < datetime() - duration({seconds: $cooldown})) ")


def _redecide(target_scope: str, *, cooled: bool = False) -> str:
    """Uncertain, or repeat/same_event whose target is no longer a current counted row of the same
    subject in scope; under $max attempts (the re-decide set). Shared by the per-namespace read and
    the namespace listing so the two cannot drift; only the target's scope predicate differs.
    `cooled` adds the $cooldown gate."""
    return " coalesce(a.count_attempts, 0) < $max " + (_COOLED if cooled else "") + """
      AND (a.count_status = 'uncertain'
           OR (a.count_status IN ['repeat', 'same_event'] AND NOT EXISTS {
                 MATCH (t:TypedAssertion {assertion_id: a.count_target_id})
                 WHERE """ + _counted("t") + """ AND t.subject_uuid = a.subject_uuid
                       AND """ + target_scope + " AND " + _live("t") + """
               })) """


_REDECIDE = _redecide(tenant_scope_cypher("t"))
_REDECIDE_COOLED = _redecide(tenant_scope_cypher("t"), cooled=True)
#: listing: the target must share the row's namespace. A spelling mismatch can only over-list (the
#: scoped per-namespace read then decides), never hide a namespace. Always cooled: it is the
#: quiet-namespace path.
_REDECIDE_ANY_NAMESPACE = _redecide(
    "coalesce(t.namespace, t.group_id, '') = coalesce(a.namespace, a.group_id, '')", cooled=True)

#: the decision SET shared by plain decisions (d) on node a. `projection_pending` is the durable
#: "View is stale" obligation, set in the same statement as the decision; the runner clears it
#: (`mark_projection_complete`) only after a confirmed rebuild, and the scalar repair pass
#: (`repair_pending_bindings`) rebuilds any row left flagged.
_SET_DECISION = """
    a.projection_pending = true,
    a.count_status = d.status, a.count_target_id = d.target_id,
    a.count_family = d.family, a.count_vote = d.vote, a.count_reason = d.reason,
    a.count_truncated = d.truncated, a.count_decided_at = datetime(),
    a.count_attempts = coalesce(a.count_attempts, 0) + 1,
    a.count_run_id = $run_id,
    a.count_key_embedding = coalesce(d.key_embedding, a.count_key_embedding),
    a.count_key_embed_version = CASE WHEN d.key_embedding IS NULL
                                     THEN a.count_key_embed_version ELSE d.embed_version END
"""


def _payload(d: dict[str, Any]) -> dict[str, Any]:
    vec = d.get("key_embedding")
    return {
        "status": str(d["status"]),
        "target_id": str(d.get("target_id") or "") or None,
        "family": str(d.get("family") or "") or None,
        "vote": str(d.get("vote") or "") or None,
        "reason": str(d.get("reason") or "") or None,
        "truncated": bool(d.get("truncated", False)),
        "redecide": bool(d.get("redecide", False)),
        "key_embedding": [float(x) for x in vec] if vec else None,
        "embed_version": str(d.get("embed_version") or "") or None,
    }


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
            + " AND " + _counted("a") + " RETURN " + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit",
            params={"subjects": subjects, "limit": int(limit), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def related_counted_occurrences(
        self, namespace: str, probes: list[dict[str, Any]], *, limit: int,
    ) -> list[dict[str, Any]]:
        """For each probe {id, subject_uuid, value_kind, needles}: the newest `limit` current counted
        occurrences of that subject and value_kind whose family, object or attribute contains any
        needle (a superset of the token relation). One row per distinct occurrence, oldest first,
        with `probe_ids` listing the probes whose capped pull contained it."""
        payload = [
            {"id": str(p["id"]), "subject": str(p.get("subject_uuid") or ""),
             "kind": str(p.get("value_kind") or "").strip().lower(),
             "needles": [str(n).lower() for n in (p.get("needles") or []) if n]}
            for p in probes
        ]
        payload = [p for p in payload if p["subject"] and p["needles"]]
        if not payload:
            return []
        rows = self._neo4j.execute(
            "UNWIND $probes AS p CALL { WITH p MATCH (a:TypedAssertion) WHERE a.subject_uuid = p.subject AND "
            + tenant_scope_cypher("a") + " AND " + _LIVE_OCCURRENCE + " AND " + _counted("a")
            + """ AND toLower(trim(coalesce(a.value_kind, ''))) = p.kind
              AND any(n IN p.needles WHERE toLower(coalesce(a.count_family, '')) CONTAINS n
                      OR toLower(coalesce(a.count_object, '')) CONTAINS n
                      OR toLower(coalesce(a.attribute, '')) CONTAINS n)
              WITH a ORDER BY a.valid_at DESC, a.assertion_id DESC LIMIT $limit
              RETURN a AS a
            }
            WITH a, collect(p.id) AS probe_ids
            RETURN probe_ids, """ + _OCCURRENCE_FIELDS
            + " ORDER BY valid_at ASC, assertion_id ASC",
            params={"probes": payload, "limit": int(limit), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def cosine_related_counted(
        self, namespace: str, probes: list[dict[str, Any]], *, embed_version: str,
        min_cosine: float, per_probe: int,
    ) -> list[dict[str, Any]]:
        """For each probe {id, subject_uuid, value_kind, vector}: the current counted occurrences of
        the same subject and value_kind whose `count_key_embedding` (same embed version) has cosine
        >= `min_cosine`, best first, at most `per_probe`. Brute force over the subject's counted
        rows (no vector index), like `search_assertion_embeddings`. Rows carry `probe_id`, `cosine`."""
        payload = [
            {"id": str(p["id"]), "subject": str(p["subject_uuid"]),
             "kind": str(p.get("value_kind") or ""), "vec": [float(x) for x in p["vector"]]}
            for p in probes if p.get("vector")
        ]
        if not payload:
            return []
        rows = self._neo4j.execute(
            "UNWIND $probes AS p MATCH (a:TypedAssertion) WHERE a.subject_uuid = p.subject AND "
            + tenant_scope_cypher("a") + " AND " + _LIVE_OCCURRENCE + """
              AND a.count_status = 'counted' AND a.value_kind = p.kind
              AND a.assertion_id <> p.id AND a.count_key_embedding IS NOT NULL
              AND a.count_key_embed_version = $ev
            WITH p, a, vector.similarity.cosine(a.count_key_embedding, p.vec) AS cos
            WHERE cos IS NOT NULL AND cos >= $min
            WITH p, a, cos ORDER BY cos DESC, a.assertion_id ASC
            WITH p, collect({a: a, cos: cos})[0..$per] AS hits
            UNWIND hits AS h
            WITH p, h.a AS a, h.cos AS cos
            RETURN p.id AS probe_id, cos AS cosine, """ + _OCCURRENCE_FIELDS,
            params={"probes": payload, "ev": str(embed_version), "min": float(min_cosine),
                    "per": int(per_probe), **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def undecided_occurrences(
        self, namespace: str, *, perceiver_version: str, limit: int,
    ) -> list[dict[str, Any]]:
        """Current, bound occurrences with no decision yet (a crash between persist and decide, or a
        binding repaired later), oldest first."""
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + tenant_scope_cypher("a") + " AND "
            + _CURRENT_OCCURRENCE + " AND " + _UNDECIDED + " RETURN " + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit",
            params={"pv": str(perceiver_version), "limit": int(limit),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def redecide_occurrences(
        self, namespace: str, *, perceiver_version: str, max_attempts: int, limit: int,
        exclude_run_id: str = "", cooldown_s: int | None = None,
    ) -> list[dict[str, Any]]:
        """Current occurrences to decide again, oldest first: `uncertain` rows, and repeat or
        same_event rows whose target is no longer a current counted row of the same subject in this
        namespace (deleted, superseded, swapped). Rows already decided `max_attempts` times are left
        as they are (they fold as uncertain), and rows decided by `exclude_run_id` (this run) wait
        for a later pass, so one pass cannot spend every attempt. With `cooldown_s`, only rows last
        decided at least that long ago (the quiet-namespace sweep)."""
        cooled = cooldown_s is not None
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + tenant_scope_cypher("a") + " AND "
            + _CURRENT_OCCURRENCE + """
              AND coalesce(a.count_run_id, '') <> $exclude_run_id
              AND """ + (_REDECIDE_COOLED if cooled else _REDECIDE) + """
            RETURN """ + _OCCURRENCE_FIELDS
            + " ORDER BY a.valid_at ASC, a.assertion_id ASC LIMIT $limit",
            params={"pv": str(perceiver_version), "max": int(max_attempts), "limit": int(limit),
                    "exclude_run_id": str(exclude_run_id or ""),
                    **({"cooldown": int(cooldown_s)} if cooled else {}),
                    **tenant_scope_params(namespace)},
        )
        return [dict(r) for r in rows]

    def pending_decision_namespaces(
        self, *, perceiver_version: str, max_attempts: int, limit: int, cooldown_s: int,
    ) -> list[str]:
        """Namespaces holding a current occurrence of this counting version that the undecided
        sweep or the cooled re-decide pass would pick up. Unscoped on purpose: it lists
        namespaces."""
        rows = self._neo4j.execute(
            "MATCH (a:TypedAssertion) WHERE " + _CURRENT_OCCURRENCE
            + " AND (" + _UNDECIDED + " OR (" + _REDECIDE_ANY_NAMESPACE + "))"
            + " RETURN DISTINCT a.namespace AS namespace ORDER BY namespace LIMIT $limit",
            params={"pv": str(perceiver_version), "max": int(max_attempts), "limit": int(limit),
                    "cooldown": int(cooldown_s)},
        )
        return list(dict.fromkeys(normalize_namespace(r.get("namespace")) for r in rows))

    def record_count_decisions(
        self, namespace: str, decisions: list[dict[str, Any]], *, perceiver_version: str,
        run_id: str, incarnation: str, epoch: int,
        swaps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Write decisions onto their occurrences, only while this run holds the counting fence
        (checked in the same statement). A decision applies only to a current occurrence of this
        namespace and version, and never replaces an existing decision unless it says `redecide`.

        `swaps` (earlier-dated retelling found later, R3-19): each sets the candidate `counted` AND
        demotes its target to a repeat/same_event of the candidate, both or neither: the target must
        still be a current counted row of the same subject, so the happening never counts twice
        (a failed swap leaves the candidate undecided, which folds as uncertain).
        Returns {"fence_ok", "written", "swapped"}."""
        payload = [{"assertion_id": str(d["assertion_id"]), **_payload(d)} for d in decisions]
        swap_payload = [
            {"candidate_id": str(s["candidate_id"]), "target_id": str(s["target_id"]),
             **_payload({**s, "status": s["status"]})}
            for s in (swaps or [])
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
                SET """ + _SET_DECISION + """
                RETURN count(a) AS written
            }
            CALL {
                WITH fence_ok
                UNWIND CASE WHEN fence_ok THEN $swaps ELSE [] END AS d
                MATCH (a:TypedAssertion {assertion_id: d.candidate_id})
                WHERE """ + tenant_scope_cypher("a") + " AND " + _CURRENT_OCCURRENCE + """
                      AND (a.count_status IS NULL OR d.redecide)
                MATCH (t:TypedAssertion {assertion_id: d.target_id})
                WHERE """ + tenant_scope_cypher("t") + " AND " + _current("t") + """
                      AND t.count_status = 'counted' AND t.subject_uuid = a.subject_uuid
                      AND t.assertion_id <> a.assertion_id
                SET t.count_status = d.status, t.count_target_id = a.assertion_id,
                    t.count_vote = d.vote, t.count_reason = 'swapped_later',
                    t.count_decided_at = datetime(), t.count_run_id = $run_id
                WITH a, d, {status: 'counted', target_id: null, family: d.family, vote: d.vote,
                            reason: d.reason, truncated: false, key_embedding: d.key_embedding,
                            embed_version: d.embed_version} AS d2
                WITH a, d2 AS d
                SET """ + _SET_DECISION + """
                RETURN count(a) AS swapped
            }
            RETURN fence_ok, written, swapped
            """,
            params={"ns": namespace, "run_id": str(run_id), "inc": str(incarnation),
                    "epoch": int(epoch), "pv": str(perceiver_version), "decisions": payload,
                    "swaps": swap_payload, **tenant_scope_params(namespace)},
        )
        if not rows:
            return {"fence_ok": False, "written": 0, "swapped": 0}
        return {"fence_ok": bool(rows[0]["fence_ok"]), "written": int(rows[0]["written"] or 0),
                "swapped": int(rows[0]["swapped"] or 0)}


__all__ = ["CountDecisionRepository"]
