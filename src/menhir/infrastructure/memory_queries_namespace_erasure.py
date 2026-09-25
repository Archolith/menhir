"""Namespace-wide erasure with scalar and event-log cascade.

Split from ``memory_queries.py``; the method body is verbatim, composed into
``MemoryQueryRepository`` by the facade module.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.namespace import normalize_namespace


class MemoryQueryNamespaceErasureMixin:
    """Namespace-erasure method of ``MemoryQueryRepository``."""

    def delete_namespace_with_scalar_cascade(
        self, group_id: str, namespace: str, *, operation_id: str
    ) -> dict[str, Any]:
        """Atomically delete a graph partition plus its namespace-keyed scalar and event logs (G15/G20).

        In addition to the ``group_id`` partition (which already covers the namespace-keyed
        :EventConsolidationWatermark cursor) and the scalar/episode namespace rows, deletes the
        durable event log: every :TypedEventAssertion in the namespace and every
        :TypedEventAssertionHead that HAS_VERSION to an event assertion in the namespace AND to no
        event assertion outside it. A shared head (still HAS_VERSION to a surviving assertion in
        another namespace) is PRESERVED; its deleted CURRENT is repaired by a later idempotent
        write, and event recall reads durable assertions, so a shared head may temporarily carry no
        CURRENT without data loss. Scalar repair receipts and return shape are unchanged.

        :TurnEvidence is deleted HERE, inside this query, rather than by a follow-up call. It holds
        raw user prompts plus ``cwd`` and ``transcript_path``, and it used to be omitted from this
        clause entirely -- so two of the three deletion paths left it behind. The one path that did
        purge it did so as a separate, unjournaled step AFTER this saga had already committed, which
        meant a crash in that window left raw prompts behind with no unresolved erasure row capable
        of resuming them. Folding the label into this MATCH makes its removal atomic with the rest
        of the partition, which is the only way that durability argument holds."""
        namespace_key = normalize_namespace(namespace)
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id,
                f.locked_at = datetime(),
                f.generation = coalesce(f.generation, 0) + 1,
                f.last_reset_operation_id = $operation_id,
                f.last_reset_at = datetime()
            WITH f
            OPTIONAL MATCH (a:TypedAssertion {namespace: $namespace})
            WITH f, collect(DISTINCT CASE WHEN a IS NULL THEN null ELSE {
                     subject_uuid: a.subject_uuid,
                     namespace: a.namespace
                 } END) AS repairs
            FOREACH (repair IN repairs |
                MERGE (rr:ScalarProjectionRepair {
                    repair_key: $operation_id + '\u001fNAMESPACE_DELETE\u001f'
                                + coalesce(repair.namespace, '\u0000null') + '\u001f'
                                + repair.subject_uuid
                })
                ON CREATE SET rr.operation_id = $operation_id,
                              rr.operation_kind = 'NAMESPACE_DELETE',
                              rr.namespace = repair.namespace,
                              rr.subject_uuid = repair.subject_uuid,
                              rr.status = 'pending', rr.started_at = datetime()
            )
            WITH f, repairs
            OPTIONAL MATCH (n)
            WHERE n.group_id = $group_id
               OR (n:Episodic AND n.namespace = $namespace)
               OR (n:TypedAssertion AND n.namespace = $namespace)
               OR (n:TypedAssertionHead AND n.namespace = $namespace)
               OR (n:ScalarConsolidationWatermark AND n.namespace = $namespace)
               OR (n:TurnEvidence AND n.namespace = $namespace)
               OR (n:EventConsolidationWatermark AND n.group_id = $namespace)
               OR (n:TypedEventAssertion AND n.namespace = $namespace)
               OR (n:TypedEventAssertionHead
                   AND EXISTS { MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion)
                                WHERE ev.namespace = $namespace }
                   AND NOT EXISTS { MATCH (n)-[:HAS_VERSION]->(ev2:TypedEventAssertion)
                                    WHERE ev2.namespace <> $namespace })
            WITH f, repairs, collect(DISTINCT n) AS doomed
            WITH f, repairs, doomed,
                 [n IN doomed WHERE coalesce(n.uuid, n.turn_id) IS NOT NULL |
                    coalesce(n.uuid, n.turn_id)] AS doomed_uuids
            OPTIONAL MATCH (v:Entity)
            WHERE (coalesce(v.is_view, false)
                   OR coalesce(v.is_quantstate, false)
                   OR v.view_kind IS NOT NULL)
              AND NOT v IN doomed
              AND (
                  any(eid IN coalesce(v.episode_uuids, []) WHERE eid IN doomed_uuids)
                  OR v.turn_evidence_uuid IN doomed_uuids
                  OR EXISTS {
                      MATCH (evidence)-[:MENTIONS]->(v)
                      WHERE evidence IN doomed
                  }
              )
            WITH f, repairs, doomed, doomed_uuids,
                 collect(DISTINCT v) AS dependent_views
            WITH f, repairs, doomed, doomed_uuids, dependent_views,
                 [v IN dependent_views
                  WHERE coalesce(v.view_current, v.qs_current, true)
                    AND NOT coalesce(v.retired, false)] AS current_views,
                 [v IN dependent_views
                  | coalesce(v.namespace, v.group_id, 'default')] AS dependent_namespaces
            FOREACH (v IN dependent_views |
                SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                       WHERE NOT eid IN doomed_uuids],
                    v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                     WHERE NOT eid IN doomed_uuids])
            )
            FOREACH (v IN [candidate IN dependent_views
                           WHERE candidate.turn_evidence_uuid IN doomed_uuids] |
                REMOVE v.turn_evidence_uuid
            )
            FOREACH (v IN current_views |
                SET v.view_current = false,
                    v.qs_current = false,
                    v.retired = true,
                    v.retired_reason = 'contributing_namespace_erased',
                    v.expired_at = datetime(),
                    v.last_accessed = datetime()
                REMOVE v.ss_view_key_current
            )
            WITH f, repairs, doomed, doomed_uuids, dependent_views, current_views,
                 dependent_namespaces,
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
                             WHERE NOT source IN doomed
                         }
                         THEN true
                         WHEN v.view_kind = 'timeline' AND EXISTS {
                             MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                             WHERE NOT source IN doomed
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
                              rr.operation_kind = 'NAMESPACE_ERASURE',
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
            WITH f, repairs, doomed, dependent_views, current_views,
                 dependent_namespaces
            CALL {
                WITH f, dependent_namespaces
                WITH [f.namespace_key] + dependent_namespaces AS namespaces
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
            FOREACH (n IN doomed | DETACH DELETE n)
            RETURN size(doomed) AS deleted, repairs,
                   size(current_views) AS dependent_views_retired,
                   size(dependent_views) AS dependent_views_scrubbed,
                   size(current_views) AS view_repairs_created,
                   watermarks_reset
            """,
            params={
                "group_id": group_id,
                "namespace": namespace,
                "namespace_key": namespace_key,
                "operation_id": operation_id,
            },
        )
        row = dict(rows[0]) if rows else {}
        return {
            "deleted": int(row.get("deleted", 0) or 0),
            "dependent_views_retired": int(row.get("dependent_views_retired", 0) or 0),
            "dependent_views_scrubbed": int(row.get("dependent_views_scrubbed", 0) or 0),
            "view_repairs_created": int(row.get("view_repairs_created", 0) or 0),
            "watermarks_reset": int(row.get("watermarks_reset", 0) or 0),
            "repairs": [dict(r) for r in (row.get("repairs") or [])],
        }
