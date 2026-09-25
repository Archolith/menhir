"""Evidence erasure with scalar-assertion cascade for the memory read repository.

Split from ``memory_queries.py``; method bodies are verbatim, composed into
``MemoryQueryRepository`` by the facade module.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class MemoryQueryErasureMixin:
    """Evidence-erasure methods of ``MemoryQueryRepository``."""

    def delete_memory_with_scalar_cascade(
        self, node_uuid: str, *, operation_id: str
    ) -> dict[str, Any]:
        """Delete one memory/observation and its authoritative scalar assertions atomically.

        A surfaced observation is addressed by ``assertion_id``; an Entity/Episodic deletion also
        removes assertions bound to or grounded on that node.  A pending repair receipt is created
        for every affected subject+namespace in the SAME Neo4j transaction as the deletion.  Thus a
        crash can delay View repair, but can never make the work undiscoverable (G15/G20).
        """
        namespace_rows = self.neo4j.execute(
            """
            OPTIONAL MATCH (target)
            WHERE ((target:Entity OR target:Episodic) AND target.uuid = $node_uuid)
               OR (target:TurnEvidence AND target.turn_id = $node_uuid)
               OR (target:TypedAssertion AND (
                   target.assertion_id = $node_uuid
                   OR target.episode_uuid = $node_uuid
                   OR target.subject_uuid = $node_uuid
               ))
            WITH [target IN collect(DISTINCT target) WHERE target IS NOT NULL |
                CASE
                    WHEN trim(toString(coalesce(target.namespace, target.group_id, ''))) = ''
                    THEN 'default'
                    ELSE trim(toString(coalesce(target.namespace, target.group_id)))
                END
            ] AS keys
            UNWIND CASE WHEN size(keys) = 0 THEN [null] ELSE keys END AS namespace_key
            RETURN collect(DISTINCT namespace_key) AS namespace_keys
            """,
            params={"node_uuid": node_uuid},
        )
        namespace_keys = [
            str(value) for value in (
                namespace_rows[0].get("namespace_keys", []) if namespace_rows else []
            ) if value is not None and str(value).strip()
        ]
        if not namespace_keys:
            return self._empty_memory_erasure_result()
        if len(namespace_keys) != 1:
            raise RuntimeError(
                "evidence erasure target resolves to multiple canonical namespaces; refusing "
                "an unfenced cross-namespace mutation"
            )
        return self._delete_memory_with_scalar_cascade_in_namespace(
            node_uuid,
            operation_id=operation_id,
            namespace_key=namespace_keys[0],
        )

    @staticmethod
    def _empty_memory_erasure_result() -> dict[str, Any]:
        return {
            "touched": False,
            "memory_touched": 0,
            "assertions_deleted": 0,
            "heads_deleted": 0,
            "dependent_views_retired": 0,
            "dependent_views_scrubbed": 0,
            "view_repairs_created": 0,
            "watermarks_reset": 0,
            "repairs": [],
        }

    def _delete_memory_with_scalar_cascade_in_namespace(
        self, node_uuid: str, *, operation_id: str, namespace_key: str
    ) -> dict[str, Any]:
        """Apply one erasure after locking and revalidating its preflight namespace."""
        rows = self.neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id, f.locked_at = datetime()
            WITH f
            OPTIONAL MATCH (target)
            WHERE ((target:Entity OR target:Episodic) AND target.uuid = $node_uuid)
               OR (target:TurnEvidence AND target.turn_id = $node_uuid)
               OR (target:TypedAssertion AND (
                   target.assertion_id = $node_uuid
                   OR target.episode_uuid = $node_uuid
                   OR target.subject_uuid = $node_uuid
               ))
            WITH f, [candidate IN collect(DISTINCT target) WHERE candidate IS NOT NULL |
                CASE
                    WHEN trim(toString(coalesce(candidate.namespace, candidate.group_id, ''))) = ''
                    THEN 'default'
                    ELSE trim(toString(coalesce(candidate.namespace, candidate.group_id)))
                END
            ] AS actual_namespace_keys
            WHERE size(actual_namespace_keys) > 0
              AND all(actual_key IN actual_namespace_keys WHERE actual_key = f.namespace_key)
            WITH [f] AS fences
            CALL {
                WITH fences
                OPTIONAL MATCH (v:Entity)
                WHERE (coalesce(v.is_view, false)
                       OR coalesce(v.is_quantstate, false)
                       OR v.view_kind IS NOT NULL)
                  AND (
                      $node_uuid IN coalesce(v.episode_uuids, [])
                      OR v.turn_evidence_uuid = $node_uuid
                      OR EXISTS {
                          MATCH (evidence)-[:MENTIONS]->(v)
                          WHERE (evidence:Episodic AND evidence.uuid = $node_uuid)
                             OR (evidence:TurnEvidence AND evidence.turn_id = $node_uuid)
                      }
                  )
                WITH fences, collect(DISTINCT v) AS dependent_views
                WITH fences, dependent_views,
                     [v IN dependent_views
                      WHERE coalesce(v.view_current, v.qs_current, true)
                        AND NOT coalesce(v.retired, false)] AS current_views
                FOREACH (v IN dependent_views |
                    SET v.episode_uuids = [eid IN coalesce(v.episode_uuids, [])
                                           WHERE eid <> $node_uuid],
                        v.supporting_event_count = size([eid IN coalesce(v.episode_uuids, [])
                                                         WHERE eid <> $node_uuid])
                )
                FOREACH (v IN [candidate IN dependent_views
                               WHERE candidate.turn_evidence_uuid = $node_uuid] |
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
                WITH fences, dependent_views, current_views,
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
                                 WHERE NOT (
                                     source.assertion_id = $node_uuid
                                     OR source.episode_uuid = $node_uuid
                                     OR source.subject_uuid = $node_uuid
                                     OR EXISTS {
                                         MATCH (:Episodic {uuid: $node_uuid})-[:ADMITTED_ON]
                                               ->(:TurnEvidence {turn_id: source.episode_uuid})
                                     }
                                 )
                             }
                             THEN true
                             WHEN v.view_kind = 'timeline' AND EXISTS {
                                 MATCH (v)-[:EVENT_HISTORY_ENTRY]->(source:TypedEventAssertion)
                                 WHERE coalesce(source.episode_uuid, '') <> $node_uuid
                                   AND coalesce(source.turn_evidence_uuid, '') <> $node_uuid
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
                                  rr.operation_kind = 'EVIDENCE_ERASURE',
                                  rr.view_uuid = repair.view.uuid,
                                  rr.view_key = coalesce(repair.view.view_key, repair.view.qs_key),
                                  rr.view_kind = repair.view.view_kind,
                                  rr.namespace = coalesce(
                                      repair.view.namespace, repair.view.group_id, 'default'),
                                  rr.namespace_key = head(fences).namespace_key,
                                  rr.fence_generation = head(fences).generation,
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
                WITH fences, dependent_views, current_views
                CALL {
                    WITH fences, dependent_views
                    WITH [fence IN fences | fence.namespace_key]
                         + [v IN dependent_views |
                            coalesce(v.namespace, v.group_id, 'default')] AS namespaces
                    UNWIND CASE WHEN size(namespaces) = 0
                                THEN [null] ELSE namespaces END AS ns
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
                RETURN size(current_views) AS dependent_views_retired,
                       size(dependent_views) AS dependent_views_scrubbed,
                       size(current_views) AS view_repairs_created,
                       watermarks_reset
            }
            CALL {
                MATCH (a:TypedAssertion)
                WHERE a.assertion_id = $node_uuid
                   OR a.episode_uuid = $node_uuid
                   OR a.subject_uuid = $node_uuid
                   OR EXISTS {
                       MATCH (source_ep:Episodic {uuid: $node_uuid})
                             -[:ADMITTED_ON]->(:TurnEvidence {turn_id: a.episode_uuid})
                   }
                WITH collect(a) AS doomed,
                     collect(DISTINCT {
                         subject_uuid: a.subject_uuid,
                         namespace: a.namespace
                     }) AS repairs,
                     collect(DISTINCT a.source_key) AS source_keys,
                     collect(DISTINCT a.assertion_id) AS assertion_ids
                FOREACH (repair IN repairs |
                    MERGE (rr:ScalarProjectionRepair {
                        repair_key: $operation_id + '\u001fMEMORY_DELETE\u001f'
                                    + coalesce(repair.namespace, '\u0000null') + '\u001f'
                                    + repair.subject_uuid
                    })
                    ON CREATE SET rr.operation_id = $operation_id,
                                  rr.operation_kind = 'MEMORY_DELETE',
                                  rr.namespace = repair.namespace,
                                  rr.subject_uuid = repair.subject_uuid,
                                  rr.status = 'pending', rr.started_at = datetime()
                )
                FOREACH (a IN doomed | DETACH DELETE a)
                WITH repairs, source_keys, assertion_ids, size(doomed) AS assertions_deleted
                OPTIONAL MATCH (rb:AssertionRebind)
                WHERE rb.assertion_id IN assertion_ids
                WITH repairs, source_keys, assertions_deleted, collect(rb) AS stale_rebinds
                FOREACH (rb IN stale_rebinds | DETACH DELETE rb)
                RETURN repairs, source_keys, assertions_deleted
            }
            CALL {
                OPTIONAL MATCH (n)
                WHERE ((n:Entity OR n:Episodic) AND n.uuid = $node_uuid)
                   OR (n:TurnEvidence AND n.turn_id = $node_uuid)
                CALL {
                    WITH n
                    WITH n WHERE n:Entity
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:Episodic
                      AND coalesce(n.processing_state, '') IN ['PENDING', 'ENRICHING']
                    SET n.processing_state = 'FAILED',
                        n.processing_stage = 'failed',
                        n.processing_substage = 'deleted_by_user',
                        n.processing_substage_started_at = datetime(),
                        n.processing_progress = coalesce(n.processing_progress, 100.0),
                        n.processing_completed_at = datetime(),
                        n.processing_owner = null,
                        n.processing_lease_expires_at = null,
                        n.processing_heartbeat_at = datetime(),
                        n.processing_error = 'deleted_by_user',
                        n.processing_llm_active_task = null,
                        n.processing_llm_active_kind = null,
                        n.processing_llm_active_model = null,
                        n.processing_llm_active_endpoint = null
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:Episodic
                      AND NOT coalesce(n.processing_state, '') IN ['PENDING', 'ENRICHING']
                    DETACH DELETE n
                    RETURN 1 AS touched
                    UNION
                    WITH n
                    WITH n WHERE n:TurnEvidence
                    DETACH DELETE n
                    RETURN 1 AS touched
                }
                RETURN count(touched) AS memory_touched
            }
            WITH repairs, source_keys, assertions_deleted, memory_touched,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset
            UNWIND CASE WHEN size(source_keys) = 0 THEN [null] ELSE source_keys END AS source_key
            OPTIONAL MATCH (h:TypedAssertionHead {source_key: source_key})
            WHERE source_key IS NOT NULL AND NOT (h)-[:HAS_VERSION]->(:TypedAssertion)
            WITH repairs, assertions_deleted, memory_touched, collect(h) AS orphan_heads,
                 dependent_views_retired, dependent_views_scrubbed,
                 view_repairs_created, watermarks_reset
            FOREACH (h IN orphan_heads | DETACH DELETE h)
            RETURN assertions_deleted, memory_touched, repairs,
                   size([h IN orphan_heads WHERE h IS NOT NULL]) AS heads_deleted,
                   dependent_views_retired, dependent_views_scrubbed,
                   view_repairs_created, watermarks_reset
            """,
            params={
                "node_uuid": node_uuid,
                "operation_id": operation_id,
                "namespace_key": namespace_key,
            },
        )
        if not rows:
            return self._empty_memory_erasure_result()
        row = dict(rows[0]) if rows else {}
        return {
            "touched": bool(int(row.get("memory_touched", 0) or 0)
                            or int(row.get("assertions_deleted", 0) or 0)),
            "memory_touched": int(row.get("memory_touched", 0) or 0),
            "assertions_deleted": int(row.get("assertions_deleted", 0) or 0),
            "heads_deleted": int(row.get("heads_deleted", 0) or 0),
            "dependent_views_retired": int(row.get("dependent_views_retired", 0) or 0),
            "dependent_views_scrubbed": int(row.get("dependent_views_scrubbed", 0) or 0),
            "view_repairs_created": int(row.get("view_repairs_created", 0) or 0),
            "watermarks_reset": int(row.get("watermarks_reset", 0) or 0),
            "repairs": [dict(r) for r in (row.get("repairs") or [])],
        }

    def delete_memory(self, node_uuid: str) -> bool:
        """Compatibility seam that cannot bypass View invalidation or repair journalling."""
        result = self.delete_memory_with_scalar_cascade(
            node_uuid, operation_id=uuid4().hex
        )
        return bool(result["touched"])
