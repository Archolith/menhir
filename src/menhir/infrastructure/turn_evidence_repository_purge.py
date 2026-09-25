"""Namespace purge and counting for `:TurnEvidence`.

Split out of ``turn_evidence_repository.py`` (file-size refactor). ``TurnEvidenceRepository``
composes ``TurnEvidencePurgeMixin``; methods run against the facade's ``self._neo4j``.
"""

from __future__ import annotations

from uuid import uuid4

from menhir.domain.namespace import (
    normalize_namespace,
    tenant_scope_cypher,
    tenant_scope_params,
)

#: Raw `purge_namespace` Cypher. The `__*_SCOPE__` placeholders are substituted per call with the
#: tenant-scope builder, exactly as before the extraction.
_PURGE_NAMESPACE_QUERY = """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id,
                f.locked_at = datetime(),
                f.generation = coalesce(f.generation, 0) + 1,
                f.last_reset_operation_id = $operation_id,
                f.last_reset_at = datetime()
            WITH f
            OPTIONAL MATCH (t:TurnEvidence)
            WHERE __TURN_SCOPE__
            WITH f, collect(DISTINCT t) AS doomed
            WITH f, doomed, [t IN doomed WHERE t.turn_id IS NOT NULL | t.turn_id] AS doomed_ids
            OPTIONAL MATCH (a:TypedAssertion)
            WHERE __ASSERTION_SCOPE__
               OR a.episode_uuid IN doomed_ids
               OR EXISTS { MATCH (source:TurnEvidence)-[:FOUNDS]->(a) WHERE source IN doomed }
            WITH f, doomed, doomed_ids, collect(DISTINCT a) AS scalar_assertions
            WITH f, doomed, doomed_ids, scalar_assertions,
                 [a IN scalar_assertions | a.assertion_id] AS scalar_assertion_ids
            OPTIONAL MATCH (event:TypedEventAssertion)
            WHERE __EVENT_SCOPE__
               OR event.episode_uuid IN doomed_ids
               OR event.turn_evidence_uuid IN doomed_ids
               OR EXISTS { MATCH (source:TurnEvidence)-[:FOUNDS]->(event) WHERE source IN doomed }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 collect(DISTINCT event) AS event_assertions
            OPTIONAL MATCH (scalar_head:TypedAssertionHead)-[:HAS_VERSION]->(head_assertion:TypedAssertion)
            WHERE head_assertion IN scalar_assertions
              AND NOT EXISTS {
                  MATCH (scalar_head)-[:HAS_VERSION]->(outside:TypedAssertion)
                  WHERE NOT outside IN scalar_assertions
              }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 event_assertions, collect(DISTINCT scalar_head) AS scalar_heads
            OPTIONAL MATCH (event_head:TypedEventAssertionHead)
                  -[:HAS_VERSION]->(head_event:TypedEventAssertion)
            WHERE head_event IN event_assertions
              AND NOT EXISTS {
                  MATCH (event_head)-[:HAS_VERSION]->(outside:TypedEventAssertion)
                  WHERE NOT outside IN event_assertions
              }
            WITH f, doomed, doomed_ids, scalar_assertions, scalar_assertion_ids,
                 event_assertions, scalar_heads,
                 collect(DISTINCT event_head) AS event_heads
            OPTIONAL MATCH (stale_repair)
            WHERE ((stale_repair:ScalarProjectionRepair
                    OR stale_repair:ViewProjectionRepair)
                   AND __REPAIR_SCOPE__)
               OR (stale_repair:AssertionRebind
                   AND stale_repair.assertion_id IN scalar_assertion_ids)
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, collect(DISTINCT stale_repair) AS stale_repairs
            OPTIONAL MATCH (v:Entity)
            WHERE (coalesce(v.is_view, false)
                   OR coalesce(v.is_quantstate, false)
                   OR v.view_kind IS NOT NULL)
              AND (
                  any(eid IN coalesce(v.episode_uuids, []) WHERE eid IN doomed_ids)
                  OR v.turn_evidence_uuid IN doomed_ids
                  OR EXISTS {
                      MATCH (source:TurnEvidence)-[:MENTIONS]->(v)
                      WHERE source IN doomed
                  }
              )
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, stale_repairs,
                 collect(DISTINCT v) AS dependent_views
            WITH f, doomed, doomed_ids, scalar_assertions, event_assertions,
                 scalar_heads, event_heads, stale_repairs, dependent_views,
                 [v IN dependent_views
                  WHERE coalesce(v.view_current, v.qs_current, true)
                    AND NOT coalesce(v.retired, false)] AS current_views,
                 [f.namespace_key] + [v IN dependent_views |
                     coalesce(v.namespace, v.group_id, 'default')] AS namespaces
            FOREACH (v IN dependent_views |
                SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                       WHERE NOT eid IN doomed_ids],
                    v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                     WHERE NOT eid IN doomed_ids])
            )
            FOREACH (v IN [candidate IN dependent_views
                           WHERE candidate.turn_evidence_uuid IN doomed_ids] |
                REMOVE v.turn_evidence_uuid
            )
            FOREACH (v IN current_views |
                SET v.view_current = false,
                    v.qs_current = false,
                    v.retired = true,
                    v.retired_reason = 'contributing_evidence_erased',
                    v.expired_at = datetime(),
                    v.last_accessed = datetime()
                REMOVE v.ss_view_key_current
            )
            FOREACH (repair IN stale_repairs | DETACH DELETE repair)
            WITH f, doomed, scalar_assertions, event_assertions, scalar_heads, event_heads,
                 stale_repairs, dependent_views, current_views, namespaces,
                 [v IN current_views | {
                     view: v,
                     source_family: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history']
                         THEN 'typed_scalar_assertions'
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(:TypedEventAssertion)
                         }
                         THEN 'typed_event_assertions'
                         ELSE 'none'
                     END,
                     reconstructible: CASE
                         WHEN v.view_kind IN ['scalar_state', 'scalar_history'] AND EXISTS {
                             MATCH (v)-[:CURRENT_ANCHOR|CONTRIBUTED_TO|HISTORY_ENTRY]
                                   ->(source:TypedAssertion)
                             WHERE NOT source IN scalar_assertions
                         }
                         THEN true
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                             WHERE NOT source IN event_assertions
                         }
                         THEN true
                         ELSE false
                     END
                 }] AS view_repairs
            FOREACH (repair IN view_repairs |
                MERGE (rr:ViewProjectionRepair {
                    repair_key: $operation_id + '\u001f' + repair.view.uuid
                })
                ON CREATE SET rr.operation_id = $operation_id,
                              rr.operation_kind = 'TURN_EVIDENCE_NAMESPACE_PURGE',
                              rr.view_uuid = repair.view.uuid,
                              rr.view_key = coalesce(repair.view.view_key, repair.view.qs_key),
                              rr.view_kind = repair.view.view_kind,
                              rr.namespace = coalesce(
                                  repair.view.namespace, repair.view.group_id, 'default'),
                              rr.namespace_key = f.namespace_key,
                              rr.fence_generation = f.generation,
                              rr.view_subtype = repair.view.view_subtype,
                              rr.subject_uuid = repair.view.view_subject_uuid,
                              rr.predicate = repair.view.view_predicate,
                              rr.domain = coalesce(repair.view.view_domain, ''),
                              rr.source_family = repair.source_family,
                              rr.reconstructible = repair.reconstructible,
                              rr.remaining_evidence_count = size(
                                  coalesce(repair.view.episode_uuids, [])),
                              rr.status = CASE
                                  WHEN repair.reconstructible THEN 'pending'
                                  ELSE 'terminal_not_rebuildable'
                              END,
                              rr.terminal_reason = CASE
                                  WHEN NOT repair.reconstructible
                                  THEN 'not_rebuildable'
                                  ELSE null
                              END,
                              rr.started_at = datetime()
            )
            WITH f, doomed, scalar_assertions, event_assertions, scalar_heads, event_heads,
                 stale_repairs, dependent_views, current_views, namespaces, view_repairs
            CALL {
                WITH namespaces
                UNWIND namespaces AS ns
                OPTIONAL MATCH (w)
                WHERE (w:ConsolidationWatermark OR w:ScalarConsolidationWatermark
                       OR w:EventConsolidationWatermark)
                  AND (coalesce(w.group_id, w.namespace, '') = ns
                       OR (ns = 'default'
                           AND coalesce(w.group_id, w.namespace, '') = ''))
                WITH collect(DISTINCT w) AS watermarks
                FOREACH (w IN watermarks | DETACH DELETE w)
                RETURN size([w IN watermarks WHERE w IS NOT NULL]) AS watermarks_reset
            }
            FOREACH (a IN scalar_assertions | DETACH DELETE a)
            FOREACH (event IN event_assertions | DETACH DELETE event)
            FOREACH (head IN scalar_heads | DETACH DELETE head)
            FOREACH (head IN event_heads | DETACH DELETE head)
            FOREACH (t IN doomed | DETACH DELETE t)
            RETURN size(doomed) AS c, size(current_views) AS dependent_views_retired,
                   size(dependent_views) AS dependent_views_scrubbed,
                   size(current_views) AS view_repairs_created,
                   size(scalar_assertions) AS typed_assertions_deleted,
                   size(event_assertions) AS typed_event_assertions_deleted,
                   size(stale_repairs) AS stale_repairs_deleted,
                   watermarks_reset
            """


class TurnEvidencePurgeMixin:
    """Throwaway-eval observability and atomic namespace teardown for `:TurnEvidence`."""

    def count_namespace(self, namespace: str) -> int:
        """Count `:TurnEvidence` nodes captured for a namespace (throwaway-eval observability)."""
        rows = self._neo4j.execute(
            "MATCH (t:TurnEvidence {namespace: $ns}) RETURN count(t) AS c",
            params={"ns": namespace},
        )
        return int(rows[0]["c"]) if rows else 0

    def purge_namespace(self, namespace: str) -> int:
        """Delete a namespace's evidence and atomically invalidate dependent Views.

        `:TurnEvidence` is keyed by `t.namespace` (not `group_id`), so the graph-partition
        teardown (`delete_namespace`, which matches `group_id`) does NOT remove it. Throwaway
        Phase 3 eval resets must call this to leave zero residue and stay re-runnable. Contributor
        UUIDs on retained historical Views are scrubbed, current dependent Views are retired, and
        fold cursors are reset in the same transaction so no caller can leave a recallable ghost.
        """
        namespace_key = normalize_namespace(namespace)
        operation_id = uuid4().hex
        purge_query = _PURGE_NAMESPACE_QUERY
        purge_query = (
            purge_query
            .replace("__TURN_SCOPE__", tenant_scope_cypher("t"))
            .replace("__ASSERTION_SCOPE__", tenant_scope_cypher("a"))
            .replace("__EVENT_SCOPE__", tenant_scope_cypher("event"))
            .replace("__REPAIR_SCOPE__", tenant_scope_cypher("stale_repair"))
        )
        rows = self._neo4j.execute(
            purge_query,
            params={
                "namespace_key": namespace_key,
                "operation_id": operation_id,
                **tenant_scope_params(namespace),
            },
        )
        return int(rows[0]["c"]) if rows else 0
