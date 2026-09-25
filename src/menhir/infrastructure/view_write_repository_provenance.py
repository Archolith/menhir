"""FACT provenance persistence for the shared View writer: MENTIONS union/replace and diagnosis."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.view_models import (
    ViewClass,
    _COUNT_TOKEN,
    _label_for,
    _log_missing_episodes,
)

# `_COUNT_TOKEN` is the placeholder for the supporting-event count inside a kind's
# `summary_template`. Provenance refresh computes the count inside Cypher so the union remains
# atomic, then substitutes the count into the template in the same statement.


class ViewFactProvenanceMixin:
    """Durable, version-local provenance for FACT views (plan D1/D2/D3): MENTIONS linking, the
    unchanged-value union refresh, the authoritative replacement, and its diagnosis."""

    def _link_episodes(
        self, node_uuid: str, episode_uuids: list[str], now: str,
        *, view_class: ViewClass = ViewClass.FACT,
    ) -> dict[str, Any]:
        """Provenance: (evidence)-[:MENTIONS]->(view) for each contributing source.

        Reports which episodes were actually PRESENT and which were MISSING instead of silently
        no-opping (plan D3). The old query inner-MATCHed the episodes, so a fact whose episodes had
        already been reaped got zero MENTIONS and zero signal about it -- the exact way prod facts
        ended up claiming supporting events they could not point at. Both legacy ``:Episodic`` and
        production ``:TurnEvidence`` inputs are evidence anchors.

        Only FACT views carry MENTIONS; METRIC nodes never do (the _write_version guard forbids
        passing episodes for METRIC), so this is a no-op there.
        """
        if not episode_uuids:
            return {"episodes_present": 0, "episodes_missing": 0}
        label = _label_for(view_class)
        rows = self.neo4j.execute(
            f"""
            MATCH (n:{label} {{uuid:$u}})
            UNWIND $eps AS eid
            OPTIONAL MATCH (ep:Episodic {{uuid: eid}})
            OPTIONAL MATCH (te:TurnEvidence {{turn_id: eid}})
            WITH n, coalesce(ep, te) AS e
            FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                MERGE (e)-[:MENTIONS]->(n))
            RETURN collect(coalesce(e.uuid, e.turn_id)) AS present
            """,
            {"u": node_uuid, "eps": episode_uuids},
        )
        present = {str(u) for u in (dict(rows[0]).get("present") or [])} if rows else set()
        missing = [u for u in episode_uuids if u not in present]
        _log_missing_episodes(node_uuid, episode_uuids, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing)}

    def _refresh_fact_provenance(
        self, node_uuid: str, episode_uuids: list[str], now: str, *,
        summary_template: str | None, refresh_props: dict[str, Any] | None,
        namespace: str | None,
    ) -> dict[str, Any]:
        """Unchanged-value provenance refresh for a FACT (plan D2), in ONE Neo4j operation.

        Unions the stored `episode_uuids` with the incoming ones, sorts and deduplicates the union,
        rewrites `supporting_event_count` and the count-bearing summary/content, MERGEs MENTIONS for
        the episodes that exist, and returns the present/missing split -- while leaving `view_value`,
        `view_sig`, `valid_at`, the node UUID, and currentness untouched.

        The union happens INSIDE Cypher, never as a Python read-modify-write: two concurrent
        refreshes of the same fact would otherwise each read the same base list and the second would
        clobber the first's UUIDs. `collect(DISTINCT ...)` after an `ORDER BY` gives the sorted dedup
        server-side; the count is substituted into the summary template in the same statement, so the
        stored count and the quoted count cannot disagree even transiently.

        CONCURRENCY: being one statement is NOT sufficient. Neo4j is read-committed and does not
        protect against lost updates -- the write lock is taken at the `SET`, but the property read
        happens upstream in the `UNWIND`, so two concurrent refreshes could both read the same base
        list, serialize at the SET, and have the second commit a union missing the first's UUIDs.
        The leading `SET n.last_accessed = $now` exists to take the node's exclusive write lock
        BEFORE `episode_uuids` is read (Neo4j's documented explicit-locking pattern). A concurrent
        refresh then blocks there, and once it proceeds its read sees the other's committed list. It
        writes `last_accessed`, which this statement sets anyway, so the lock costs no extra property.
        """
        from menhir.domain.namespace import (
            normalize_namespace,
            tenant_scope_cypher,
            tenant_scope_params,
        )

        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (n:Entity {uuid:$u})
            WHERE coalesce(n.view_current, n.qs_current, true)
              AND NOT coalesce(n.retired, false)
            OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)
            WHERE old_evidence:Episodic OR old_evidence:TurnEvidence
            WITH f, n, collect(DISTINCT coalesce(old_evidence.uuid,
                                                 old_evidence.turn_id)) AS old_mentions
            WHERE size(old_mentions) = size(coalesce(n.episode_uuids, []))
              AND all(eid IN old_mentions WHERE eid IN coalesce(n.episode_uuids, []))
            CALL {
                WITH n
                WITH n, (coalesce(n.episode_uuids, []) + $eps) AS all_eids
                UNWIND CASE WHEN size(all_eids) = 0 THEN [null] ELSE all_eids END AS eid
                WITH DISTINCT n, eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR
                       (e:TurnEvidence AND e.turn_id = eid))
                  AND """ + tenant_scope_cypher("e") + """
                  AND e.evidence_finalized = true
                  AND NOT coalesce(e.evidence_quarantined, false)
                WITH eid, [candidate IN collect(DISTINCT e)
                           WHERE candidate IS NOT NULL] AS candidates
                WITH collect({eid: eid, candidates: candidates}) AS resolved
                RETURN [row IN resolved WHERE size(row.candidates) = 1 |
                        head(row.candidates)] AS evidence,
                       [row IN resolved | row.eid] AS requested,
                       size([row IN resolved WHERE size(row.candidates) = 1]) AS resolved_count
            }
            WITH f, n, evidence, requested, resolved_count
            WHERE resolved_count = size(requested)
              AND all(e IN evidence WHERE
                  coalesce(e.evidence_generation, e.publication_generation) = f.generation)
            SET n.last_accessed = $now, n.view_fence_generation = f.generation
            WITH n, (coalesce(n.episode_uuids, []) + $eps) AS all_eids
            UNWIND CASE WHEN size(all_eids) = 0 THEN [null] ELSE all_eids END AS eid
            WITH n, eid ORDER BY eid
            WITH n, collect(DISTINCT eid) AS uuids
            SET n.episode_uuids = uuids,
                n.supporting_event_count = size(uuids),
                n.summary = CASE WHEN $tpl IS NULL THEN n.summary
                                 ELSE replace($tpl, $token, toString(size(uuids))) END,
                n.content = CASE WHEN $tpl IS NULL THEN n.content
                                 ELSE replace($tpl, $token, toString(size(uuids))) END
            SET n += $refresh
            WITH n, uuids
            CALL {
                WITH n, uuids
                UNWIND CASE WHEN size(uuids) = 0 THEN [null] ELSE uuids END AS eid
                OPTIONAL MATCH (ep:Episodic {uuid: eid})
                OPTIONAL MATCH (te:TurnEvidence {turn_id: eid})
                WITH n, coalesce(ep, te) AS e
                FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                    MERGE (e)-[:MENTIONS]->(n))
                RETURN [uuid IN collect(coalesce(e.uuid, e.turn_id))
                        WHERE uuid IS NOT NULL] AS present
            }
            RETURN uuids AS stored, present
            """,
            {"u": node_uuid, "eps": episode_uuids, "now": now, "token": _COUNT_TOKEN,
             "namespace_key": namespace_key,
             "operation_id": f"refresh:{node_uuid}:{now}",
             "tpl": summary_template, "refresh": dict(refresh_props or {}),
             **tenant_scope_params(namespace_key)},
        )
        if not rows:
            # Zero rows means ONE of four gates above refused, and the write statement cannot
            # say which. Ask read-only, then name it (issue #94; same shape as 68fa2ec8 for the
            # admission path). A refusal that names no gate is a swallow with extra steps.
            detail = self._diagnose_fact_provenance_refusal(node_uuid, episode_uuids, namespace_key)
            raise ValueError(
                "FACT View provenance refresh refused: every stored contributor UUID must resolve "
                f"to live :Episodic or :TurnEvidence evidence; {detail}"
            )
        row = dict(rows[0])
        stored = [str(u) for u in (row.get("stored") or [])]
        present = {str(u) for u in (row.get("present") or [])}
        missing = [u for u in stored if u not in present]
        _log_missing_episodes(node_uuid, stored, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing),
                "supporting_event_count": len(stored)}

    def _diagnose_fact_provenance_refusal(
        self, node_uuid: str, episode_uuids: list[str], namespace_key: str,
    ) -> str:
        """Read-only: report which of the refresh statement's gates refused, and why.

        Mirrors the gates in `_refresh_fact_provenance` one by one -- node/current/retired, the
        old-MENTIONS parity check, and per-contributor resolution (exists, in tenant scope,
        finalized, not quarantined, exactly one candidate, generation matches the fence). Any
        error while diagnosing is reported inside the string rather than raised, so the original
        refusal is never replaced by a diagnostics failure.
        """
        from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params

        try:
            rows = self.neo4j.execute(
                """
                OPTIONAL MATCH (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
                OPTIONAL MATCH (n:Entity {uuid: $u})
                OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)
                WHERE old_evidence:Episodic OR old_evidence:TurnEvidence
                WITH f, n, collect(DISTINCT coalesce(old_evidence.uuid, old_evidence.turn_id)) AS old_mentions
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR (e:TurnEvidence AND e.turn_id = eid))
                WITH f, n, old_mentions, eid, collect(e) AS any_match,
                     [c IN collect(e) WHERE """ + tenant_scope_cypher("c") + """] AS in_scope
                RETURN
                  f IS NOT NULL AS fence_exists,
                  f.generation AS fence_generation,
                  n IS NOT NULL AS node_exists,
                  coalesce(n.view_current, n.qs_current, true) AS node_current,
                  coalesce(n.retired, false) AS node_retired,
                  n.episode_uuids AS stored_eps,
                  old_mentions,
                  collect({
                    eid: eid,
                    any_match: size(any_match),
                    in_scope: size(in_scope),
                    finalized: [c IN in_scope | coalesce(c.evidence_finalized, false)],
                    quarantined: [c IN in_scope | coalesce(c.evidence_quarantined, false)],
                    generation: [c IN in_scope | coalesce(c.evidence_generation, c.publication_generation)],
                    labels: [c IN any_match | labels(c)]
                  }) AS contributors
                """,
                {"u": node_uuid, "eps": list(episode_uuids), "namespace_key": namespace_key,
                 **tenant_scope_params(namespace_key)},
            )
        except Exception as exc:  # noqa: BLE001 - diagnostics must not replace the refusal
            return f"diagnosis unavailable ({type(exc).__name__}: {exc})"
        if not rows:
            return "diagnosis returned no rows"
        r = dict(rows[0])
        stored = list(r.get("stored_eps") or [])
        old = list(r.get("old_mentions") or [])
        parity = len(old) == len(stored) and all(x in stored for x in old)
        parts = [
            f"node_uuid={node_uuid}",
            f"namespace_key={namespace_key}",
            f"fence_exists={r.get('fence_exists')} fence_generation={r.get('fence_generation')}",
            f"node_exists={r.get('node_exists')} current={r.get('node_current')} retired={r.get('node_retired')}",
            f"old_mentions_parity={parity} stored_eps={stored} old_mentions={old}",
            f"requested_eps={list(episode_uuids)}",
        ]
        for c in r.get("contributors") or []:
            parts.append(
                f"contributor eid={c.get('eid')} any_match={c.get('any_match')} "
                f"in_scope={c.get('in_scope')} labels={c.get('labels')} "
                f"finalized={c.get('finalized')} quarantined={c.get('quarantined')} "
                f"generation={c.get('generation')}"
            )
        return " | ".join(parts)

    def _replace_fact_provenance(
        self, node_uuid: str, episode_uuids: list[str], now: str, *,
        summary_template: str | None, refresh_props: dict[str, Any] | None,
        namespace: str | None,
    ) -> dict[str, Any]:
        """Unchanged-signature provenance REPLACEMENT for an authoritative rebuild, in ONE Neo4j
        operation. Unlike `_refresh_fact_provenance` (which UNIONS), this sets `episode_uuids` to
        EXACTLY the incoming contributor set, `supporting_event_count` to that length, prunes
        MENTIONS from episodes no longer in the set, and merges MENTIONS for the new set — because a
        full rebuild replaces the projection, a contributor episode that dropped out must not linger.
        `refresh_props` carries the full re-rendered projection (surface + ss_* + audit). Existing
        fact/counter consumers are untouched: they never pass replace_provenance, so they keep union.

        The leading `SET n.last_accessed` takes the node's write lock before provenance is rewritten
        (same explicit-locking pattern as the union path). Handles an EMPTY set: prune all MENTIONS,
        store []."""
        from menhir.domain.namespace import (
            normalize_namespace,
            tenant_scope_cypher,
            tenant_scope_params,
        )

        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (n:Entity {uuid:$u})
            WHERE coalesce(n.view_current, n.qs_current, true)
              AND NOT coalesce(n.retired, false)
            OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)
            WHERE old_evidence:Episodic OR old_evidence:TurnEvidence
            WITH f, n, collect(DISTINCT coalesce(old_evidence.uuid,
                                                 old_evidence.turn_id)) AS old_mentions
            WHERE size(old_mentions) = size(coalesce(n.episode_uuids, []))
              AND all(eid IN old_mentions WHERE eid IN coalesce(n.episode_uuids, []))
            CALL {
                WITH n
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (e)
                WHERE ((e:Episodic AND e.uuid = eid) OR
                       (e:TurnEvidence AND e.turn_id = eid))
                  AND """ + tenant_scope_cypher("e") + """
                  AND e.evidence_finalized = true
                  AND NOT coalesce(e.evidence_quarantined, false)
                WITH eid, [candidate IN collect(DISTINCT e)
                           WHERE candidate IS NOT NULL] AS candidates
                WITH collect({eid: eid, candidates: candidates}) AS resolved
                RETURN [row IN resolved WHERE size(row.candidates) = 1 |
                        head(row.candidates)] AS evidence,
                       size([row IN resolved WHERE size(row.candidates) = 1]) AS resolved_count
            }
            WITH f, n, evidence, resolved_count
            WHERE resolved_count = size($eps)
              AND all(e IN evidence WHERE
                  coalesce(e.evidence_generation, e.publication_generation) = f.generation)
            SET n.last_accessed = $now,
                n.view_fence_generation = f.generation,
                n.episode_uuids = $eps,
                n.supporting_event_count = size($eps),
                n.summary = CASE WHEN $tpl IS NULL THEN n.summary
                                 ELSE replace($tpl, $token, toString(size($eps))) END,
                n.content = CASE WHEN $tpl IS NULL THEN n.content
                                 ELSE replace($tpl, $token, toString(size($eps))) END
            SET n += $refresh
            WITH n
            CALL {
                WITH n
                OPTIONAL MATCH (old)-[m:MENTIONS]->(n)
                WHERE NOT coalesce(old.uuid, old.turn_id) IN $eps
                  AND (old:Episodic OR old:TurnEvidence)
                DELETE m
                RETURN count(*) AS pruned
            }
            WITH n
            CALL {
                WITH n
                UNWIND CASE WHEN size($eps) = 0 THEN [null] ELSE $eps END AS eid
                OPTIONAL MATCH (ep:Episodic {uuid: eid})
                OPTIONAL MATCH (te:TurnEvidence {turn_id: eid})
                WITH n, coalesce(ep, te) AS e
                FOREACH (_ IN CASE WHEN e IS NULL THEN [] ELSE [1] END |
                    MERGE (e)-[:MENTIONS]->(n))
                RETURN [uuid IN collect(coalesce(e.uuid, e.turn_id))
                        WHERE uuid IS NOT NULL] AS present
            }
            RETURN present
            """,
            {"u": node_uuid, "eps": episode_uuids, "now": now, "token": _COUNT_TOKEN,
             "namespace_key": namespace_key,
             "operation_id": f"replace:{node_uuid}:{now}",
             "tpl": summary_template, "refresh": dict(refresh_props or {}),
             **tenant_scope_params(namespace_key)},
        )
        if not rows:
            raise ValueError(
                "FACT View authoritative rebuild refused: every declared contributor UUID must "
                "resolve to live :Episodic or :TurnEvidence evidence"
            )
        present = {str(u) for u in (dict(rows[0]).get("present") or [])} if rows else set()
        missing = [u for u in episode_uuids if u not in present]
        _log_missing_episodes(node_uuid, episode_uuids, missing)
        return {"episodes_present": len(present), "episodes_missing": len(missing),
                "supporting_event_count": len(episode_uuids)}
