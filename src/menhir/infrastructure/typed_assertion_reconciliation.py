"""Typed-assertion rebinding, reconciliation receipts, and activation operations."""

from __future__ import annotations

import json
from typing import Any, Callable

from menhir.domain.typed_assertion import IDENTITY_VERSION, TypedAssertion, normalize_scalar
from menhir.infrastructure.schema import get_scalar_state_activation_queries

from menhir.infrastructure.typed_assertion_models import (
    ScalarStateActivationError,
    _RECORD_CYPHER,
)

class TypedAssertionReconciliationMixin:
    def rebind_assertions(
        self, *, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Rebind the absorbed entity's :TypedAssertion nodes onto the survivor after an
        ENTITY_MERGE, journaling one :AssertionRebind per moved assertion (idempotent per op via the
        rebind_key unique constraint). The merge's DETACH DELETE removed the absorbed Entity + its
        HAS_ASSERTION edges, but the assertion NODES survive carrying the dead subject_uuid; move them
        by that surviving property. `namespace` (C.4.4) scopes the move to ONE silo so a tenant-A
        repair never drags a tenant-B assertion that happens to share the absorbed subject_uuid; None =
        all (unchanged). The rebind_key stays op+assertion_id (per assertion, so no namespace collision)
        and the record persists the moved assertion's namespace for a scoped restore. Head subject_uuid
        moves ONLY for the moved assertions' source_keys. Returns {rebound, source_keys}."""
        if not absorbed_uuid.strip() or not survivor_uuid.strip():
            raise ValueError("rebind_assertions requires non-blank absorbed and survivor uuids")
        if not merge_op_id.strip():
            raise ValueError("rebind_assertions requires a merge_op_id (lineage)")
        if absorbed_uuid.strip() == survivor_uuid.strip():
            return {"rebound": 0, "source_keys": []}
        ns_pred = " AND a.namespace = $namespace" if namespace is not None else ""
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion {{subject_uuid: $absorbed}})
            WHERE true{ns_pred}
            // journal the per-op lineage entry (DB-unique on rebind_key = op + assertion_id); persist
            // the assertion's namespace so an unmerge can restore this op's rebinds scoped by silo.
            MERGE (r:AssertionRebind {{rebind_key: $op + '::' + a.assertion_id}})
              ON CREATE SET r.merge_op_id = $op, r.assertion_id = a.assertion_id,
                            r.from_uuid = $absorbed, r.to_uuid = $survivor,
                            r.source_key = a.source_key, r.namespace = a.namespace,
                            r.rebound_at = datetime()
            SET a.subject_uuid = $survivor, a.rebound_at = datetime()
            WITH collect(a) AS moved, collect(DISTINCT a.source_key) AS source_keys
            OPTIONAL MATCH (s:Entity {{uuid: $survivor}})
            FOREACH (a IN CASE WHEN s IS NULL THEN [] ELSE moved END |
                MERGE (s)-[:HAS_ASSERTION]->(a))
            // scope head moves to EXACTLY the moved assertions' source_keys (the head identity)
            WITH moved, source_keys
            UNWIND (CASE WHEN size(source_keys)=0 THEN [null] ELSE source_keys END) AS sk
            OPTIONAL MATCH (h:TypedAssertionHead {{source_key: sk}})
            FOREACH (_ IN CASE WHEN h IS NULL THEN [] ELSE [1] END | SET h.subject_uuid = $survivor)
            RETURN size(moved) AS rebound, source_keys
            """,
            params={"absorbed": absorbed_uuid.strip(), "survivor": survivor_uuid.strip(),
                    "op": merge_op_id.strip(), "namespace": namespace},
        )
        if not rows:
            return {"rebound": 0, "source_keys": []}
        row = rows[0]
        return {"rebound": int(row.get("rebound", 0) or 0),
                "source_keys": [str(c) for c in (row.get("source_keys") or [])]}

    def restore_rebound_assertions(
        self, *, merge_op_id: str, namespace: str | None = None
    ) -> dict[str, Any]:
        """Inverse of `rebind_assertions` for an unmerge, scoped to ONE merge op. Move exactly the
        assertions this op moved back to their recorded from_uuid, re-point HAS_ASSERTION, restrict
        head moves to those source_keys, and delete the op's :AssertionRebind records. `namespace`
        (C.4.4) further restricts to the rebind records of ONE silo (the record persists its
        namespace); None = all of the op's records (unchanged). Idempotent (a replay finds no records).
        Returns {restored, source_keys, from_uuids}."""
        if not merge_op_id.strip():
            raise ValueError("restore_rebound_assertions requires a merge_op_id")
        ns_pred = " AND r.namespace = $namespace" if namespace is not None else ""
        rows = self._neo4j.execute(
            f"""
            MATCH (r:AssertionRebind {{merge_op_id: $op}})
            WHERE true{ns_pred}
            MATCH (a:TypedAssertion {{assertion_id: r.assertion_id}})
            WITH a, r, r.from_uuid AS from_uuid, r.to_uuid AS to_uuid, r.source_key AS sk
            // unlink the current (to) owner's HAS_ASSERTION, move back to from_uuid, re-link it
            OPTIONAL MATCH (:Entity {{uuid: to_uuid}})-[rel:HAS_ASSERTION]->(a)
            DELETE rel
            SET a.subject_uuid = from_uuid
            WITH a, r, from_uuid, sk
            OPTIONAL MATCH (b:Entity {{uuid: from_uuid}})
            FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [1] END |
                MERGE (b)-[:HAS_ASSERTION]->(a))
            // move the head for this exact source_key back to from_uuid
            WITH a, r, from_uuid, sk
            OPTIONAL MATCH (h:TypedAssertionHead {{source_key: sk}})
            FOREACH (_ IN CASE WHEN h IS NULL THEN [] ELSE [1] END | SET h.subject_uuid = from_uuid)
            WITH collect(DISTINCT a) AS moved, collect(DISTINCT sk) AS source_keys,
                 collect(DISTINCT from_uuid) AS from_uuids, collect(r) AS records
            FOREACH (r IN records | DELETE r)
            RETURN size(moved) AS restored, source_keys, from_uuids
            """,
            params={"op": merge_op_id.strip(), "namespace": namespace},
        )
        if not rows:
            return {"restored": 0, "source_keys": [], "from_uuids": []}
        row = rows[0]
        return {"restored": int(row.get("restored", 0) or 0),
                "source_keys": [str(c) for c in (row.get("source_keys") or [])],
                "from_uuids": [str(u) for u in (row.get("from_uuids") or [])]}

    #: reconciliation is receipted per-DIRECTION so a forward merge (marked REVERSED after an
    #: unmerge) and its inverse each carry their OWN completion proof. The receipt is keyed by the
    #: operation's OWN id (the merge op_id for a merge; the unmerge op_id for an unmerge).
    RECONCILE_KINDS = ("ENTITY_MERGE", "ENTITY_UNMERGE")

    def record_reconcile_receipt(
        self, *, operation_id: str, operation_kind: str, merge_op_id: str | None = None,
        namespace: str | None = None,
    ) -> None:
        """Write a :ScalarReconcile receipt marking scalar reconciliation COMPLETE for a lifecycle op
        IN ONE NAMESPACE. Written ONLY after ALL stages succeed, so a committed op WITHOUT a matching
        receipt is exactly a partial/failed reconciliation the repair pass must redo — even when rebind
        succeeded and only rebuild failed (no dead subject_uuid for the orphan scan), and even for a
        committed UNMERGE (whose forward merge is marked REVERSED, so a merge-only scan would miss it).

        NAMESPACE-KEYED (C.4.4): identity is (operation_id, operation_kind, namespace). One merge op can
        span two assertion silos and is repaired INDEPENDENTLY per silo (orphan repair replays it with
        each row's namespace), so a global receipt would let a tenant-A success certify a tenant-B
        failure and permanently mask tenant-B's missing View. `namespace=None` keys the receipt to the
        null silo, which is exactly the scope such a repair covered. Idempotent."""
        if not operation_id.strip() or operation_kind not in self.RECONCILE_KINDS:
            raise ValueError(f"record_reconcile_receipt needs operation_id + kind in {self.RECONCILE_KINDS}")
        self._neo4j.execute(
            """
            MERGE (rc:ScalarReconcile {receipt_key: $receipt_key})
            ON CREATE SET rc.operation_id = $op, rc.operation_kind = $kind,
                          rc.namespace = $namespace, rc.merge_op_id = $merge_op,
                          rc.completed_at = datetime(), rc.status = 'complete'
            ON MATCH SET rc.operation_id = $op, rc.operation_kind = $kind,
                         rc.namespace = $namespace, rc.merge_op_id = $merge_op,
                         rc.status = 'complete', rc.completed_at = datetime()
            """,
            params={"receipt_key": self._receipt_key(operation_id, operation_kind, namespace),
                    "op": operation_id.strip(), "kind": operation_kind, "namespace": namespace,
                    "merge_op": (merge_op_id or "").strip() or None},
        )

    def record_reconcile_intent(
        self, *, operation_id: str, operation_kind: str, merge_op_id: str | None = None,
        namespace: str | None = None,
    ) -> None:
        """Write a PENDING :ScalarReconcile marker BEFORE a reconciliation consumes its own namespace
        evidence, so that evidence outlives a mid-operation crash.

        The unmerge path is the reason this exists. `restore_rebound_assertions` DELETES the forward
        op's :AssertionRebind records — the only durable carrier of the unmerge's namespace. If restore
        succeeds and the subsequent rebuild raises, no completion receipt is written, yet later
        namespace discovery finds no rebind records and would silently skip the receiptless unmerge
        FOREVER. The invariant: namespace evidence must not be consumable before the namespace-keyed
        receipt is complete.

        Shares receipt identity (receipt_key) with `record_reconcile_receipt`, so the completion write
        simply promotes this same node to status='complete'. ON MATCH is deliberately a NO-OP: a
        replay must never downgrade a complete receipt back to pending. Idempotent."""
        if not operation_id.strip() or operation_kind not in self.RECONCILE_KINDS:
            raise ValueError(f"record_reconcile_intent needs operation_id + kind in {self.RECONCILE_KINDS}")
        self._neo4j.execute(
            """
            MERGE (rc:ScalarReconcile {receipt_key: $receipt_key})
            ON CREATE SET rc.operation_id = $op, rc.operation_kind = $kind,
                          rc.namespace = $namespace, rc.merge_op_id = $merge_op,
                          rc.started_at = datetime(), rc.status = 'pending'
            """,
            params={"receipt_key": self._receipt_key(operation_id, operation_kind, namespace),
                    "op": operation_id.strip(), "kind": operation_kind, "namespace": namespace,
                    "merge_op": (merge_op_id or "").strip() or None},
        )

    @staticmethod
    def _receipt_key(operation_id: str, operation_kind: str, namespace: str | None) -> str:
        """Stable per-(op, kind, namespace) receipt identity. The null namespace gets an explicit
        sentinel so it can never collide with a real silo literally named 'null'."""
        ns = "\x00null" if namespace is None else namespace
        return f"{operation_id.strip()}\x1f{operation_kind}\x1f{ns}"

    def reconcile_complete(
        self, operation_id: str, *, operation_kind: str | None = None,
        namespace: str | None = None, any_namespace: bool = False,
    ) -> bool:
        """True if a COMPLETE :ScalarReconcile receipt exists for this operation. By default the check
        is NAMESPACE-SCOPED (C.4.4): a receipt for tenant-A does NOT mark tenant-B complete. Pass
        `any_namespace=True` for the legacy "any silo finished it" question (observability only —
        never to decide whether a silo's repair may be skipped)."""
        where = "WHERE rc.status = 'complete'"
        params: dict[str, Any] = {"op": operation_id.strip()}
        if operation_kind is not None:
            where += " AND rc.operation_kind = $kind"
            params["kind"] = operation_kind
        if not any_namespace:
            where += (" AND (rc.namespace = $namespace OR "
                      "(rc.namespace IS NULL AND $namespace IS NULL))")
            params["namespace"] = namespace
        rows = self._neo4j.execute(
            f"MATCH (rc:ScalarReconcile {{operation_id: $op}}) {where} RETURN 1 AS ok LIMIT 1", params)
        return bool(rows)

    # ---- fresh-only activation gate (ScalarStateView Piece C.3, migration) --------------------
    #
    # The source_key identity contract (head keyed by source_key; assertion_key built from
    # source_key) has no in-place migration to or from any other version. Rather than silently
    # create the source_key uniqueness constraint over an incompatible store — mixing identity
    # spaces — activation REFUSES to run while any node's identity_version != IDENTITY_VERSION. Every
    # node is stamped identity_version = IDENTITY_VERSION at write time (ON CREATE only), so the gate
    # is EXACT-MATCH: unstamped (v1), older, AND newer (a rolled-back binary meeting v3 nodes) all
    # fail closed. The operator's remedies are an explicit migration or, for a development store,
    # `purge_scalar_state_nodes()`.

    def incompatible_identity_nodes_exist(self) -> dict[str, int]:
        """Count :TypedAssertionHead and :TypedAssertion nodes whose identity_version is INCOMPATIBLE
        with this binary (IS NULL or <> IDENTITY_VERSION — i.e. unstamped, older, OR newer). Returns
        {incompatible_heads, incompatible_assertions}. Zero of both means every node matches this
        binary's contract exactly and the store is safe to activate."""
        rows = self._neo4j.execute(
            """
            CALL {
                MATCH (h:TypedAssertionHead)
                WHERE h.identity_version IS NULL OR h.identity_version <> $v
                RETURN count(h) AS incompatible_heads
            }
            CALL {
                MATCH (a:TypedAssertion)
                WHERE a.identity_version IS NULL OR a.identity_version <> $v
                RETURN count(a) AS incompatible_assertions
            }
            RETURN incompatible_heads, incompatible_assertions
            """,
            params={"v": IDENTITY_VERSION},
        )
        row = rows[0] if rows else {}
        return {
            "incompatible_heads": int(row.get("incompatible_heads", 0) or 0),
            "incompatible_assertions": int(row.get("incompatible_assertions", 0) or 0),
        }

    def assert_scalar_state_activatable(self) -> None:
        """Fail closed unless every scalar-state node matches this binary's identity contract
        EXACTLY. Raises `ScalarStateActivationError` if ANY head or assertion is unstamped, older, or
        newer, so a deploy (including a rollback onto newer-stamped data) can never silently
        establish the source_key identity space over mixed identities. C.4's activation MUST call
        this before enabling scalar state or creating its DDL."""
        counts = self.incompatible_identity_nodes_exist()
        if counts["incompatible_heads"] or counts["incompatible_assertions"]:
            raise ScalarStateActivationError(
                counts["incompatible_heads"], counts["incompatible_assertions"])

    def activate_scalar_state(self) -> dict[str, Any]:
        """Gate, then bring the scalar-state DDL online. Refuses (raising
        `ScalarStateActivationError`) if the store holds any identity-incompatible node (unstamped,
        older, or newer); otherwise runs the gated activation queries
        (`get_scalar_state_activation_queries`), which create the source_key identity constraints and
        drop the superseded v1 head-claim_key constraint. Idempotent on an exact-match store (every
        DDL is IF (NOT) EXISTS). Returns {queries_executed}."""
        self.assert_scalar_state_activatable()
        queries = get_scalar_state_activation_queries()
        for query in queries:
            self._neo4j.execute(query)
        return {"queries_executed": len(queries)}

    def purge_scalar_state_nodes(self) -> dict[str, Any]:
        """DEVELOPMENT/operator escape hatch: hard-delete the ENTIRE scalar-state footprint — the
        event-log/lifecycle nodes (:TypedAssertion, :TypedAssertionHead, :AssertionRebind,
        :ScalarReconcile, :ScalarProjectionRepair), every materialized `kind='scalar_state'` View
        (:Entity, ALL versions, not
        only current), AND the scheduling state (:ScalarConsolidationWatermark). Deleting the log
        without its Views would leave stale projections recallable after their supporting assertions
        are gone; deleting only current Views would leave orphaned historical versions; and leaving
        the watermarks behind would make a purged namespace look already-consolidated so the fresh
        store never backfills. Destructive and irreversible — the caller (an operator action, never an
        automatic startup path) is responsible for confirming it is safe to discard the durable
        assertion log, its projections, and its scheduling cursors. Returns per-label deleted counts."""
        rows = self._neo4j.execute(
            """
            CALL { MATCH (a:TypedAssertion) DETACH DELETE a RETURN count(a) AS assertions }
            CALL { MATCH (h:TypedAssertionHead) DETACH DELETE h RETURN count(h) AS heads }
            CALL { MATCH (r:AssertionRebind) DETACH DELETE r RETURN count(r) AS rebinds }
            CALL { MATCH (rc:ScalarReconcile) DETACH DELETE rc RETURN count(rc) AS receipts }
            CALL { MATCH (rr:ScalarProjectionRepair) DETACH DELETE rr
                   RETURN count(rr) AS projection_repairs }
            CALL { MATCH (v:Entity {view_kind: 'scalar_state'}) DETACH DELETE v RETURN count(v) AS views }
            CALL { MATCH (w:ScalarConsolidationWatermark) DETACH DELETE w RETURN count(w) AS watermarks }
            RETURN assertions, heads, rebinds, receipts, projection_repairs, views, watermarks
            """,
        )
        row = rows[0] if rows else {}
        return {
            "assertions": int(row.get("assertions", 0) or 0),
            "heads": int(row.get("heads", 0) or 0),
            "rebinds": int(row.get("rebinds", 0) or 0),
            "receipts": int(row.get("receipts", 0) or 0),
            "projection_repairs": int(row.get("projection_repairs", 0) or 0),
            "views": int(row.get("views", 0) or 0),
            "watermarks": int(row.get("watermarks", 0) or 0),
        }
