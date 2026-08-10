"""Typed-assertion binding/projection repair and current-claim query operations."""

from __future__ import annotations

import json
from typing import Any, Callable

from menhir.domain.typed_assertion import IDENTITY_VERSION, TypedAssertion, normalize_scalar
from menhir.infrastructure.schema import get_scalar_state_activation_queries

from menhir.infrastructure.typed_assertion_models import (
    ScalarStateActivationError,
    _RECORD_CYPHER,
)

class TypedAssertionRepairMixin:
    def head_uuids_for_source_key(self, source_key: str) -> list[dict[str, Any]]:
        """Heads for a binding-stable source_key (episode+span+ordinal), regardless of current
        subject_uuid. source_key is DB-unique on the head, so at most one is returned; a lookup that
        finds >1 is a schema/migration violation the caller must fail closed on, never silently pick."""
        rows = self._neo4j.execute(
            "MATCH (h:TypedAssertionHead {source_key: $sk}) RETURN h.claim_key AS claim_key, "
            "h.subject_uuid AS subject_uuid",
            params={"sk": source_key},
        )
        return [dict(r) for r in rows]

    # NOTE: an in-place `backfill_head_source_keys()` migration once lived here. It was removed:
    # deriving a source_key onto legacy heads is precisely the mixed-identity migration the
    # fresh-only activation gate forbids. Legacy/incompatible stores must be purged
    # (`purge_scalar_state_nodes`) or migrated out-of-band by an operator, never silently backfilled.

    def orphaned_subject_uuids(self, *, limit: int = 200) -> list[str]:
        """Distinct subject_uuids on current, FULLY-BOUND :TypedAssertion nodes whose Entity no
        longer exists — assertions orphaned by a merge whose post-commit rebind did not run.
        Excludes binding_pending (C.4's deliberately-unresolved advisory assertions are NOT
        merge-crash orphans). A repair pass resolves each to its survivor via merge lineage."""
        rows = self._neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE NOT coalesce(a.superseded, false) AND NOT coalesce(a.binding_pending, false)
            WITH DISTINCT a.subject_uuid AS uuid
            WHERE uuid IS NOT NULL AND NOT EXISTS { MATCH (e:Entity {uuid: uuid}) }
            RETURN uuid LIMIT $limit
            """,
            params={"limit": int(limit)},
        )
        return [str(r["uuid"]) for r in rows if r.get("uuid")]

    def pending_advisory_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Current :TypedAssertion rows that need binding repair (ScalarStateView C.4.4): either
        binding_pending advisories (awaiting a UNIQUE entity binding; subject is the
        `unbound:<source_key>` sentinel) OR projection_pending rows (already bound, but their View
        rebuild has not yet completed — a crash-recovery case). Neither is a merge orphan
        (`orphaned_subject_uuids` excludes both), so nothing else resolves them. Returns every field
        needed to RECONSTRUCT + re-record an advisory (assertion_key omits subject_uuid, so a
        re-record with the resolved subject lands on the SAME node via the C.4.3 pending->bound
        adoption) PLUS `binding_pending`/`subject_uuid` so the repair pass can tell an advisory
        (resolve + re-record) from a projection-only row (already bound -> just rebuild). Only current
        rows (`NOT superseded`) — a superseded row was already replaced and must not be revived.

        NAMESPACE ALLOWLIST: when `namespaces` is given, ONLY rows in that set are returned, under a
        SINGLE global `limit` — one true bound with fairness ACROSS the allowlisted tenants, never a
        per-namespace loop that would multiply the bound or starve later tenants.

        FAIRNESS: a bounded scan must be EVENTUALLY COMPLETE — an unresolved first page must never
        permanently starve a newer, now-bindable row. Rows are ordered UNATTEMPTED-first, then
        LEAST-RECENTLY-attempted (`binding_repair_attempted_at`, stamped by
        `mark_binding_repair_attempted` after every attempt regardless of outcome), then
        learned_at/assertion_id as a stable tiebreak. So each run advances the retry frontier and the
        whole backlog is revisited over successive runs (rows need repeated attempts as the graph
        changes, so a monotonic learned_at cursor would be wrong here)."""
        where = [
            "NOT coalesce(a.superseded, false)",
            "(coalesce(a.binding_pending, false) OR coalesce(a.projection_pending, false))",
        ]
        params: dict[str, Any] = {"limit": int(limit)}
        if namespaces is not None:
            where.append("a.namespace IN $namespaces")
            params["namespaces"] = list(namespaces)
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion)
            WHERE {' AND '.join(where)}
            RETURN a.assertion_id AS assertion_id, a.source_key AS source_key,
                   a.subject_uuid AS subject_uuid, a.subject_display AS subject_display,
                   coalesce(a.binding_pending, false) AS binding_pending,
                   coalesce(a.projection_pending, false) AS projection_pending,
                   a.attribute AS attribute, a.scope AS scope, a.value_kind AS value_kind,
                   a.unit AS unit, a.operation AS operation, a.value_json AS value_json,
                   a.stated_span AS stated_span, a.episode_uuid AS episode_uuid,
                   a.span_start AS span_start, a.span_end AS span_end,
                   a.claim_ordinal AS claim_ordinal,
                   toString(a.valid_at) AS valid_at, toString(a.learned_at) AS learned_at,
                   a.time_basis AS time_basis, a.perceiver_version AS perceiver_version,
                   a.namespace AS namespace
            ORDER BY
                CASE WHEN a.binding_repair_attempted_at IS NULL THEN 0 ELSE 1 END,
                a.binding_repair_attempted_at ASC,
                a.learned_at ASC, a.assertion_id ASC
            LIMIT $limit
            """,
            params=params,
        )
        return [self._hydrate(dict(r)) for r in rows]

    def mark_binding_repair_attempted(self, assertion_ids: list[str], *, at: str) -> int:
        """Stamp `binding_repair_attempted_at` on every row the repair pass just examined — regardless
        of outcome (bound, zero/multiple match, store-pending, mismatch, or a rebuild error). This
        advances the fairness frontier so `pending_advisory_assertions` selects the NEXT
        least-recently-tried rows on the following run, making a bounded repair eventually complete.
        Returns the count stamped. A no-op on an empty list."""
        ids = [i for i in (assertion_ids or []) if i]
        if not ids:
            return 0
        rows = self._neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE a.assertion_id IN $ids
            SET a.binding_repair_attempted_at = datetime($at)
            RETURN count(a) AS stamped
            """,
            params={"ids": ids, "at": at},
        )
        return int(rows[0].get("stamped", 0) or 0) if rows else 0

    def mark_projection_complete(self, assertion_ids: list[str]) -> int:
        """Clear the `projection_pending` crash-recovery marker on assertions whose ScalarStateView
        rebuild has SUCCEEDED (C.4.4.2). Called ONLY after a successful rebuild, so a crash between the
        binding write (which set projection_pending) and this clear leaves the row discoverable by the
        repair pass. Returns the count cleared. No-op on an empty list. Idempotent."""
        ids = [i for i in (assertion_ids or []) if i]
        if not ids:
            return 0
        rows = self._neo4j.execute(
            """
            MATCH (a:TypedAssertion)
            WHERE a.assertion_id IN $ids
            SET a.projection_pending = false
            RETURN count(a) AS cleared
            """,
            params={"ids": ids},
        )
        return int(rows[0].get("cleared", 0) or 0) if rows else 0

    def entity_exists(self, uuid: str) -> bool:
        """True if an :Entity with this uuid currently exists — the fail-closed survivor check for the
        C.4.4 orphan pass (a chain whose terminal survivor was itself deleted must stay unresolved,
        never reported repaired against a dead uuid).

        Deliberately UUID-GLOBAL, matching the identity semantics assertion binding already uses
        (`_RECORD_CYPHER` binds via `OPTIONAL MATCH (n:Entity {uuid: $subject_uuid})` with no namespace
        predicate). Canonical entities are NOT tenant-scoped in this schema; namespace lives on the
        ASSERTIONS and Views. A namespace predicate here would invent a second `Entity.namespace`
        contract and would reject valid survivors."""
        if not (uuid or "").strip():
            return False
        rows = self._neo4j.execute(
            "MATCH (e:Entity {uuid: $u}) RETURN 1 AS ok LIMIT 1", params={"u": uuid.strip()})
        return bool(rows)

    def namespaces_for_operation(
        self, *, merge_op_id: str, absorbed_uuid: str | None = None
    ) -> list[str]:
        """Distinct namespaces of the assertions a lifecycle op AFFECTS — the fail-closed way to derive
        its namespace for scoped reconciliation (the journal pair rows carry none). Union of:
          * the op's own :AssertionRebind records (authoritative AFTER a partial/complete rebind — each
            record persists the moved assertion's namespace), and
          * current assertions still sitting on the ABSORBED uuid (the not-yet-rebound case).
        Deliberately EXCLUDES survivor-native assertions: a tenant-A merge whose survivor happens to
        hold unrelated tenant-B assertions is a tenant-A operation, not a multi-namespace violation."""
        rows = self._neo4j.execute(
            "MATCH (r:AssertionRebind {merge_op_id: $op}) RETURN DISTINCT r.namespace AS namespace",
            params={"op": (merge_op_id or "").strip()},
        )
        out: list[Any] = [r.get("namespace") for r in rows]
        if absorbed_uuid:
            rows2 = self._neo4j.execute(
                """
                MATCH (a:TypedAssertion {subject_uuid: $absorbed})
                WHERE NOT coalesce(a.superseded, false)
                RETURN DISTINCT a.namespace AS namespace
                """,
                params={"absorbed": absorbed_uuid},
            )
            out.extend(r.get("namespace") for r in rows2)
        seen: list[Any] = []
        for ns in out:
            if ns not in seen:
                seen.append(ns)
        return seen

    def namespaces_for_unmerge(
        self, *, unmerge_op_id: str, merge_op_id: str
    ) -> list[str]:
        """Namespaces an UNMERGE affects. Union of:
          * the forward merge's surviving :AssertionRebind records (the rows it still has to restore),
            and
          * this unmerge's OWN :ScalarReconcile markers, pending or complete.

        The second source is load-bearing: restore DELETES the rebind records, so after a
        restore-succeeded/rebuild-failed crash the first source is empty while the operation is still
        receiptless. The pending marker written before restore keeps the namespace discoverable, so the
        retry finds the silo instead of skipping it forever. Union (not fallback) because a partially
        restored op may legitimately have evidence in both places."""
        rows = self._neo4j.execute(
            "MATCH (r:AssertionRebind {merge_op_id: $op}) RETURN DISTINCT r.namespace AS namespace",
            params={"op": (merge_op_id or "").strip()},
        )
        out: list[Any] = [r.get("namespace") for r in rows]
        rows2 = self._neo4j.execute(
            """
            MATCH (rc:ScalarReconcile {operation_id: $op, operation_kind: 'ENTITY_UNMERGE'})
            RETURN DISTINCT rc.namespace AS namespace
            """,
            params={"op": (unmerge_op_id or "").strip()},
        )
        out.extend(r.get("namespace") for r in rows2)
        seen: list[Any] = []
        for ns in out:
            if ns not in seen:
                seen.append(ns)
        return seen

    def orphaned_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Distinct (subject_uuid, namespace) work items for current, FULLY-BOUND assertions whose
        Entity no longer exists — orphaned by a merge whose post-commit rebind never ran (ScalarStateView
        C.4.4). Excludes binding_pending (advisories are the pending-binding pass's job). Carries the
        durable NAMESPACE so the repair rebuilds/retires Views in the orphan's own silo, and supports a
        namespace ALLOWLIST. FAIRNESS (same contract as pending repair): unattempted work first, then
        least-recently `orphan_repair_attempted_at` (stamped by `mark_orphan_repair_attempted`), then a
        stable uuid/namespace tiebreak — so a bounded scan is eventually complete and an unresolvable
        first page never starves a later resolvable orphan."""
        where = ["NOT coalesce(a.superseded, false)", "NOT coalesce(a.binding_pending, false)"]
        params: dict[str, Any] = {"limit": int(limit)}
        if namespaces is not None:
            where.append("a.namespace IN $namespaces")
            params["namespaces"] = list(namespaces)
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedAssertion)
            WHERE {' AND '.join(where)}
            WITH a.subject_uuid AS subject_uuid, a.namespace AS namespace,
                 min(a.orphan_repair_attempted_at) AS attempted
            WHERE subject_uuid IS NOT NULL
              AND NOT EXISTS {{ MATCH (e:Entity {{uuid: subject_uuid}}) }}
            RETURN subject_uuid, namespace,
                   CASE WHEN attempted IS NULL THEN 0 ELSE 1 END AS was_attempted, attempted
            ORDER BY was_attempted ASC, attempted ASC, subject_uuid ASC, namespace ASC
            LIMIT $limit
            """,
            params=params,
        )
        return [{"subject_uuid": str(r["subject_uuid"]), "namespace": r.get("namespace")}
                for r in rows if r.get("subject_uuid")]

    def mark_orphan_repair_attempted(
        self, work_items: list[dict[str, Any]], *, at: str
    ) -> int:
        """Stamp `orphan_repair_attempted_at` on the assertions of the orphan work items the pass just
        examined — keyed by (subject_uuid, namespace) so a tenant-A attempt never stamps a tenant-B
        assertion sharing the subject_uuid. Regardless of outcome (repaired, unresolved, errored), this
        advances the fairness frontier so `orphaned_assertions` selects the NEXT least-recently-tried
        orphans next run. Returns the count stamped. No-op on an empty list."""
        items = [
            {"uuid": str(w.get("subject_uuid") or ""), "namespace": w.get("namespace")}
            for w in (work_items or []) if str(w.get("subject_uuid") or "")
        ]
        if not items:
            return 0
        rows = self._neo4j.execute(
            """
            UNWIND $items AS item
            MATCH (a:TypedAssertion {subject_uuid: item.uuid})
            WHERE a.namespace = item.namespace OR (a.namespace IS NULL AND item.namespace IS NULL)
            SET a.orphan_repair_attempted_at = datetime($at)
            RETURN count(a) AS stamped
            """,
            params={"items": items, "at": at},
        )
        return int(rows[0].get("stamped", 0) or 0) if rows else 0

    def current_for_claim(self, claim_key: str) -> dict[str, Any] | None:
        """The single current assertion for a claim (head -> CURRENT), or None."""
        rows = self._neo4j.execute(
            """
            MATCH (h:TypedAssertionHead {claim_key: $ck})-[:CURRENT]->(a:TypedAssertion)
            RETURN a.assertion_id AS assertion_id, a.assertion_key AS assertion_key,
                   a.value AS value, a.value_json AS value_json,
                   a.evidence_tier AS evidence_tier, a.perceiver_version AS perceiver_version
            LIMIT 1
            """,
            params={"ck": claim_key},
        )
        return self._hydrate(dict(rows[0])) if rows else None

    # ---- helpers -----------------------------------------------------------------------------

    @staticmethod
    def _to_params(a: TypedAssertion) -> dict[str, Any]:
        return {
            "identity_version": IDENTITY_VERSION,
            "claim_key": a.claim_key,
            "source_key": a.source_key,
            "assertion_key": a.assertion_key,
            "subject_uuid": a.subject_uuid.strip(),
            "subject_display": a.subject_display,
            "attribute": a.attribute.strip().lower(),
            "scope": a.scope.strip().lower(),
            "value_kind": a.value_kind.strip().lower(),
            "unit": (a.unit or "").strip().lower(),
            "operation": a.operation.strip().lower(),
            "value_norm": normalize_scalar(a.value),
            "value_json": json.dumps(a.value, ensure_ascii=False),
            "stated_span": a.stated_span.strip(),
            "span_start": int(a.span_start), "span_end": int(a.span_end),
            "claim_ordinal": int(a.claim_ordinal),
            "episode_uuid": a.episode_uuid.strip(),
            "valid_at": a.valid_at, "learned_at": a.learned_at,
            "time_basis": a.time_basis,
            "evidence_tier": a.evidence_tier, "evidence_rank": a.evidence_rank,
            "perceiver_version": a.perceiver_version, "perceiver_rank": a.perceiver_rank,
            "namespace": a.namespace,
            "absolute_semantics": (a.absolute_semantics or "ordinary").strip().lower(),
            "metadata_json": json.dumps(a.metadata or {}, ensure_ascii=False),
            "name_embedding": a.name_embedding,
            "embed_version": a.embed_version,
        }

    @staticmethod
    def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
        """Decode value_json back to its typed shape (number/range/bool/string)."""
        raw = row.get("value_json")
        if raw is not None:
            try:
                row["value"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        return row
