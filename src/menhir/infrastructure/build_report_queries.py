"""Per-namespace build-report queries (counts, integrity checks, failed projections, scope promotion).

Every statement takes the namespace as a query parameter (``$ns`` / ``$nl``), never interpolated
into the text. Reads raise on an empty or failed result rather than returning a sentinel, so a
caller can never mistake "the query failed" for "the count is zero".
"""

from __future__ import annotations

from typing import Any

from menhir.domain.ingest import EXTRACTION_MODE_STORE_ONLY
from menhir.domain.namespace import namespace_to_group_id

BUILD_COUNT_SECTIONS = ("state", "scalar", "integrity")

_STATE_QUERY = (
    "MATCH (e:Episodic {namespace:$ns}) RETURN "
    "sum(CASE WHEN e.processing_state='PENDING' THEN 1 ELSE 0 END) AS pending, "
    "sum(CASE WHEN e.processing_state='READY' THEN 1 ELSE 0 END) AS ready, "
    "sum(CASE WHEN e.processing_state='ENRICHING' THEN 1 ELSE 0 END) AS enriching, "
    "sum(CASE WHEN e.processing_state='FAILED' THEN 1 ELSE 0 END) AS failed, "
    "sum(coalesce(toInteger(e.processing_llm_tasks_total), 0)) AS llm_tasks, "
    "sum(coalesce(toInteger(e.processing_attempts), 0)) AS processing_attempts, "
    "count(*) AS total"
)

_SCALAR_QUERIES: dict[str, str] = {
    "turn_evidence": "MATCH (t:TurnEvidence {namespace:$ns}) RETURN count(t) AS c",
    "typed_assertions": "MATCH (a:TypedAssertion {namespace:$ns}) RETURN count(a) AS c",
    "scalar_views": (
        "MATCH (v:Entity {group_id:$ns, view_kind:'scalar_state'}) "
        "WHERE coalesce(v.view_current, true) RETURN count(v) AS c"
    ),
    "scalar_history_views": (
        "MATCH (v:Entity {group_id:$ns, view_kind:'scalar_history'}) "
        "WHERE coalesce(v.view_current, true) RETURN count(v) AS c"
    ),
    "user_founded_scalar_views": (
        "MATCH (t:TurnEvidence {namespace:$ns, declarant:'user'})-[:FOUNDS]->"
        "(a:TypedAssertion {namespace:$ns})<-[:CURRENT_ANCHOR]-"
        "(v:Entity {group_id:$gid, view_kind:'scalar_state'}) "
        "WHERE coalesce(v.view_current, true) RETURN count(DISTINCT v) AS c"
    ),
}

# Keyed on group_id alone; their ``$ns`` is bound to the translated group_id (default -> '').
_GROUP_KEYED_SCALARS = frozenset({"scalar_views", "scalar_history_views"})

_ADMITTED_QUERY = (
    "MATCH (t:TurnEvidence {namespace:$ns}) "
    "WITH t, "
    "EXISTS { MATCH (q:Episodic)-[:ADMITTED_ON]->(t) WHERE q.valid_at IS NULL "
    "AND q.source = 'user' AND NOT coalesce(q.is_evidence_projection, false) } AS admitted, "
    "EXISTS { MATCH (q:Episodic)-[:ADMITTED_ON]->(t) WHERE q.valid_at IS NULL "
    "AND q.source = 'user' AND NOT coalesce(q.is_evidence_projection, false) "
    "AND coalesce(q.extraction_mode, '') <> $store_only } AS extracted, "
    "EXISTS { MATCH (p:Episodic)-[:ADMITTED_ON]->(t) "
    "WHERE coalesce(p.is_evidence_projection, false) } AS projected "
    "RETURN sum(CASE WHEN admitted THEN 1 ELSE 0 END) AS admitted, "
    "sum(CASE WHEN extracted AND NOT projected THEN 1 ELSE 0 END) AS unprojected"
)

_UNFOUNDED_QUERY = (
    "MATCH (a {namespace:$ns}) WHERE (a:TypedAssertion OR a:TypedEventAssertion) "
    "AND NOT EXISTS { MATCH (:TurnEvidence {namespace:$ns})-[:FOUNDS]->(a) } "
    "AND NOT EXISTS { MATCH (:TurnEvidence {namespace:$ns})<-[:ADMITTED_ON]-(:Episodic)"
    "-[:GROUNDS]->(a) } RETURN count(a) AS c"
)

_VALID_AT_UNSUBMITTED_QUERY = (
    "MATCH (g:Episodic {group_id:$ns}) RETURN sum(CASE WHEN g.valid_at IS NULL THEN 1 "
    "WHEN datetime(toString(g.valid_at)).epochMillis IN $epochs THEN 0 ELSE 1 END) AS c"
)

_TIME_UNPAIRED_QUERY = (
    "MATCH (q:Episodic {namespace:$ns}) WHERE q.valid_at IS NULL "
    "AND q.processing_state = 'READY' AND coalesce(q.extraction_mode, '') <> $store_only "
    "OPTIONAL MATCH (g:Episodic {group_id:$gid}) "
    "WHERE g.uuid = q.resolved_episode_uuid AND g.valid_at IS NOT NULL "
    "WITH q, collect(g) AS gs, "
    "q.processing_substage = 'verbatim_skipped' AND q.extraction_skipped_for IS NOT NULL AS skipped "
    "OPTIONAL MATCH (p:Episodic {namespace:$ns}) WHERE skipped "
    "AND p.uuid = q.extraction_skipped_for AND coalesce(p.is_evidence_projection, false) "
    "AND p.processing_state = 'READY' "
    "WITH q, gs, skipped, collect(p) AS ps "
    "RETURN sum(CASE WHEN skipped THEN CASE WHEN size(ps) = 1 AND q.reference_time IS NOT NULL "
    "AND ps[0].reference_time IS NOT NULL "
    "AND datetime(toString(ps[0].reference_time)).epochMillis "
    "= datetime(toString(q.reference_time)).epochMillis THEN 0 ELSE 1 END "
    "WHEN size(gs) <> 1 OR q.reference_time IS NULL THEN 1 "
    "WHEN datetime(toString(gs[0].valid_at)).epochMillis "
    "<> datetime(toString(q.reference_time)).epochMillis THEN 1 ELSE 0 END) AS c"
)

_CROSS_NAMESPACE_QUERY = (
    "MATCH (a) WHERE toLower(a.namespace) = $nl OR toLower(a.group_id) = $gl "
    "WITH a, CASE WHEN a.namespace IS NOT NULL AND a.group_id IS NOT NULL "
    "AND [toLower(a.namespace), toLower(a.group_id)] <> [$nl, $gl] THEN 1 ELSE 0 END AS split "
    "OPTIONAL MATCH (a)-[r]-(b) WHERE toLower(coalesce(b.namespace, $nl)) <> $nl "
    "OR toLower(coalesce(b.group_id, $gl)) <> $gl "
    "OR toLower(coalesce(r.namespace, $nl)) <> $nl "
    "OR toLower(coalesce(r.group_id, $gl)) <> $gl "
    "WITH a, split, count(r) AS crossing RETURN sum(split) + sum(crossing) AS c"
)

_FAILED_PROJECTIONS_QUERY = (
    "MATCH (e:Episodic {namespace:$ns}) WHERE e.processing_state='FAILED' "
    "AND coalesce(e.is_evidence_projection, false) "
    "RETURN e.uuid AS uuid, e.processing_error AS processing_error ORDER BY uuid"
)

_PROMOTE_ENTITIES_QUERY = (
    "MATCH (n:Entity {group_id:$ns}) WHERE coalesce(n.scope,'SESSION')='SESSION' "
    "SET n.scope='PERSISTENT' RETURN count(*) AS c"
)
_PROMOTE_EDGES_QUERY = (
    "MATCH ()-[r:RELATES_TO {group_id:$ns}]->() "
    "WHERE coalesce(r.scope,'SESSION')='SESSION' SET r.scope='PERSISTENT' RETURN count(*) AS c"
)


class BuildReportQueries:
    """Runs the build-report statements through a `Neo4jRepository`-shaped ``execute``."""

    def __init__(self, neo4j: Any) -> None:
        self.neo4j = neo4j

    def _rows(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.neo4j.execute(query, params=params)
        return list(rows or [])

    def _one(self, query: str, params: dict[str, Any], what: str) -> dict[str, Any]:
        rows = self._rows(query, params)
        if not rows:
            raise RuntimeError(f"build report read returned no row: {what}")
        return rows[0]

    def _count(self, query: str, params: dict[str, Any], what: str) -> int:
        return int(self._one(query, params, what).get("c") or 0)

    def namespace_build_counts(
        self,
        namespace: str,
        submitted_epoch_millis: list[int] | None = None,
        sections: list[str] | None = None,
    ) -> dict[str, Any]:
        wanted = set(BUILD_COUNT_SECTIONS if sections is None else sections)
        unknown = wanted - set(BUILD_COUNT_SECTIONS)
        if unknown:
            raise ValueError(f"unknown build count sections: {sorted(unknown)}")
        ns = namespace
        params = {"ns": ns, "gid": namespace_to_group_id(ns)}
        out: dict[str, Any] = {"namespace": ns}
        if "state" in wanted:
            row = self._one(_STATE_QUERY, params, "state")
            out["state"] = {
                key: int(row.get(key) or 0)
                for key in ("pending", "ready", "enriching", "failed", "llm_tasks",
                            "processing_attempts", "total")
            }
        if "scalar" in wanted:
            out["scalar"] = {
                key: self._count(
                    query,
                    {"ns": params["gid"]} if key in _GROUP_KEYED_SCALARS else params,
                    f"scalar.{key}",
                )
                for key, query in _SCALAR_QUERIES.items()
            }
        if "integrity" in wanted:
            out["integrity"] = self._integrity(ns, submitted_epoch_millis)
        return out

    def _integrity(self, ns: str, submitted_epoch_millis: list[int] | None) -> dict[str, Any]:
        gid = namespace_to_group_id(ns)
        params = {"ns": ns}
        store_params = {"ns": ns, "gid": gid, "store_only": EXTRACTION_MODE_STORE_ONLY}
        admitted = self._one(_ADMITTED_QUERY, store_params, "integrity.admitted")
        unsubmitted: int | None = None
        if submitted_epoch_millis is not None:
            unsubmitted = self._count(
                _VALID_AT_UNSUBMITTED_QUERY,
                {"ns": gid, "epochs": [int(e) for e in submitted_epoch_millis]},
                "integrity.episodes_valid_at_unsubmitted",
            )
        return {
            "user_turns_admitted": int(admitted.get("admitted") or 0),
            "admitted_turns_unprojected": int(admitted.get("unprojected") or 0),
            "assertions_unfounded": self._count(
                _UNFOUNDED_QUERY, params, "integrity.assertions_unfounded"),
            "episodes_valid_at_unsubmitted": unsubmitted,
            "ready_episodes_time_unpaired": self._count(
                _TIME_UNPAIRED_QUERY, store_params, "integrity.ready_episodes_time_unpaired"),
            "cross_namespace_links": self._count(
                _CROSS_NAMESPACE_QUERY, {"nl": ns.lower(), "gl": gid.lower()}, "integrity.cross_namespace_links"),
        }

    def failed_evidence_projections(self, namespace: str) -> list[dict[str, Any]]:
        rows = self._rows(_FAILED_PROJECTIONS_QUERY, {"ns": namespace})
        return [
            {"uuid": r.get("uuid"), "processing_error": r.get("processing_error")}
            for r in rows
        ]

    def promote_namespace_scope(self, namespace: str) -> dict[str, Any]:
        # Two sequential single-statement writes; each SET is absolute, so re-running is harmless.
        entities = self._count(_PROMOTE_ENTITIES_QUERY, {"ns": namespace_to_group_id(namespace)}, "promote.entities")
        edges = self._count(_PROMOTE_EDGES_QUERY, {"ns": namespace_to_group_id(namespace)}, "promote.edges")
        return {"namespace": namespace, "entities_promoted": entities, "edges_promoted": edges}
