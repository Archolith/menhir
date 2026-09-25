"""Merge/unmerge lifecycle + orphan repair half of the scalar_state_service facade.

`ScalarMergeLifecycleMixin` carries `handle_merge` / `handle_unmerge` (Piece C.3), the
transitive repair pass for receiptless lifecycle ops, and the scheduled orphan-rebind pass.
The mixin is composed into `ScalarStateService` by the facade module.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.clock import utc_now_iso as _utc_now_iso
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.scalar_state_service_common import _projection_complete

# Preserve the facade module's logger name so log filtering is unaffected by the split.
logger = logging.getLogger("menhir.services.scalar_state_service")


class ScalarMergeLifecycleMixin:
    """Merge-lifecycle reconciliation methods for ScalarStateService."""

    # ---- merge lifecycle (Piece C.3) ---------------------------------------------------------

    def handle_merge(
        self, *, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """After an ENTITY_MERGE commits, reconcile scalar state onto the survivor. Order matters:
        (1) RETIRE every scalar View still keyed on the absorbed (now-deleted) entity; (2) REBIND
        the absorbed entity's assertions onto the survivor, journaled per merge_op_id (event-log
        rebinding, not key rewrite, so a shared slot can't collide on a View key); (3) REBUILD the
        survivor's Views (the fold resolves overlapping slots by latest anchor). Only after ALL
        THREE succeed is a :ScalarReconcile receipt written — so a crash between rebind and rebuild
        (which leaves no dead subject_uuid for the orphan scan) is still found by the repair pass as
        a committed merge WITHOUT a receipt. Idempotent."""
        retired_absorbed: list[str] = []
        for view in self._views.list_scalar_state_views(subject_uuid=absorbed_uuid, namespace=namespace):
            if self._views.retire_scalar_state(view_key=str(view.get("view_key"))):
                retired_absorbed.append(str(view.get("view_key")))
        # Retire absorbed entity's scalar_history Views too (stale current history on a dead entity).
        retired_history: list[str] = []
        if hasattr(self._views, "list_scalar_history_views"):
            for view in self._views.list_scalar_history_views(
                    subject_uuid=absorbed_uuid, namespace=namespace):
                if self._views.retire_scalar_history(view_key=str(view.get("view_key"))):
                    retired_history.append(str(view.get("view_key")))
        rebound = self._assertions.rebind_assertions(
            absorbed_uuid=absorbed_uuid, survivor_uuid=survivor_uuid, merge_op_id=merge_op_id,
            namespace=namespace)
        rebuild = self.rebuild_scalar_projections(
            survivor_uuid, namespace=namespace,
            history_enabled=self._scalar_history_enabled)
        # Receipt is the LAST step: its presence means every stage completed. Keyed by the merge's
        # OWN op_id + ENTITY_MERGE, so the repair pass can find a committed merge without one.
        # NAMESPACE-KEYED receipt (C.4.4): this reconciliation covered exactly ONE silo, so its proof
        # of completion is scoped to that silo. A global receipt would let a tenant-A success certify a
        # tenant-B failure of the same op and permanently mask tenant-B's missing View.
        # Do NOT record the receipt if history rebuild errored — leave receiptless for scheduler retry.
        history = rebuild.get("history")
        history_errored = not _projection_complete(rebuild)
        if not history_errored:
            self._assertions.record_reconcile_receipt(
                operation_id=merge_op_id, operation_kind="ENTITY_MERGE", namespace=namespace)
        else:
            logger.warning(
                "merge reconciliation for %s: history rebuild failed; receipt withheld for retry",
                merge_op_id)
        _audit.audit(
            "merge", "reconciled" if not history_errored else "partial",
            namespace=namespace, subject_uuid=survivor_uuid,
            details={"absorbed_uuid": absorbed_uuid, "merge_op_id": merge_op_id,
                     "retired_absorbed": len(retired_absorbed),
                     "retired_history": len(retired_history),
                     "rebound": rebound["rebound"],
                     "history_errored": history_errored},
        )
        return {
            "absorbed_uuid": absorbed_uuid, "survivor_uuid": survivor_uuid,
            "merge_op_id": merge_op_id,
            "retired_absorbed": len(retired_absorbed),
            "retired_history": len(retired_history),
            "rebound": rebound["rebound"],
            "survivor_rebuild": rebuild,
            "history_errored": history_errored,
        }

    def handle_unmerge(
        self, *, survivor_uuid: str, absorbed_uuid: str, merge_op_id: str,
        unmerge_op_id: str, namespace: str | None = None,
    ) -> dict[str, Any]:
        """Inverse of `handle_merge`, scoped to the SAME merge_op_id whose rebind this reverses: move
        exactly that op's rebound assertions back to their recorded owners, then rebuild every
        affected entity. In a chain A->B->C, unmerging op2 (C->B) returns op2's assertions (A-origin
        AND B-native) to B; a later unmerge of op1 (B->A) returns only op1's (A-origin) to A. The
        receipt is keyed by the UNMERGE's OWN op_id + ENTITY_UNMERGE (the forward merge is marked
        REVERSED, so a merge-only scan would never revisit it — blocker 3). Idempotent.

        INTENT BEFORE RESTORE: `restore_rebound_assertions` deletes the forward op's :AssertionRebind
        records, which are otherwise the only durable carrier of this unmerge's namespace. Writing the
        pending marker FIRST means a restore-succeeded/rebuild-failed crash still leaves discoverable
        namespace evidence, so the retry finds the receiptless unmerge instead of skipping it forever.
        Namespace evidence must never be consumed before the namespace-keyed receipt is complete."""
        self._assertions.record_reconcile_intent(
            operation_id=unmerge_op_id, operation_kind="ENTITY_UNMERGE", merge_op_id=merge_op_id,
            namespace=namespace)
        restored = self._assertions.restore_rebound_assertions(
            merge_op_id=merge_op_id, namespace=namespace)
        rebuilt: dict[str, Any] = {}
        history_errored = False
        for uuid in {survivor_uuid, absorbed_uuid, *restored.get("from_uuids", [])}:
            r = self.rebuild_scalar_projections(
                uuid, namespace=namespace,
                history_enabled=self._scalar_history_enabled)
            rebuilt[uuid] = r
            if not _projection_complete(r):
                history_errored = True
        # Do NOT record the receipt if any entity's history rebuild errored — leave receiptless
        # for scheduler retry (state rebuilds are idempotent).
        if not history_errored:
            self._assertions.record_reconcile_receipt(
                operation_id=unmerge_op_id, operation_kind="ENTITY_UNMERGE", merge_op_id=merge_op_id,
                namespace=namespace)
        else:
            logger.warning(
                "unmerge reconciliation for %s: history rebuild failed on one or more entities; "
                "receipt withheld for retry", unmerge_op_id)
        _audit.audit(
            "unmerge", "reconciled" if not history_errored else "partial",
            namespace=namespace, subject_uuid=survivor_uuid,
            details={"absorbed_uuid": absorbed_uuid, "merge_op_id": merge_op_id,
                     "unmerge_op_id": unmerge_op_id, "restored": restored["restored"],
                     "rebuilt_uuids": sorted(rebuilt.keys()),
                     "history_errored": history_errored},
        )
        return {
            "survivor_uuid": survivor_uuid, "absorbed_uuid": absorbed_uuid,
            "merge_op_id": merge_op_id, "unmerge_op_id": unmerge_op_id,
            "restored": restored["restored"], "rebuilt": {u: r for u, r in rebuilt.items()},
            "history_errored": history_errored,
        }

    @staticmethod
    def _downstream_index(merges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Index committed merges by their ABSORBED uuid. A merge chain A->B->C is expressed as
        op1{absorbed:A, survivor:B} and op2{absorbed:B, survivor:C}: op2 is DOWNSTREAM of op1 because
        op2.absorbed == op1.survivor. So `index[survivor]` yields the ops to replay after a given op."""
        index: dict[str, list[dict[str, Any]]] = {}
        for op in merges:
            if str(op.get("op_id") or ""):
                index.setdefault(str(op.get("absorbed_uuid")), []).append(op)
        return index

    def _resolve_unmerge_namespaces(
        self, *, unmerge_op_id: str, merge_op_id: str, allowed: set[str] | None,
    ) -> list[str | None]:
        """Namespaces an unmerge affects: the forward merge's surviving rebind records UNION this
        unmerge's own reconcile markers. The marker source is what survives a
        restore-succeeded/rebuild-failed crash (restore deletes the rebind records), keeping a
        receiptless unmerge discoverable on the next scheduled pass. Filtered by the allowlist."""
        nss = self._assertions.namespaces_for_unmerge(
            unmerge_op_id=unmerge_op_id, merge_op_id=merge_op_id)
        if allowed is not None:
            nss = [ns for ns in nss if ns in allowed]
        return list(nss)

    def _resolve_op_namespaces(
        self, *, merge_op_id: str, absorbed_uuid: str | None, allowed: set[str] | None,
    ) -> list[str | None]:
        """Namespaces a lifecycle op AFFECTS, derived from the op's OWN assertions (its
        :AssertionRebind records + any assertions still on the absorbed uuid) — never from
        survivor-native rows, so a tenant-A merge whose survivor holds unrelated tenant-B assertions
        is a tenant-A op, not a multi-namespace violation. An op legitimately spanning TWO silos yields
        BOTH, and each is reconciled + receipted INDEPENDENTLY (one silo's success cannot certify the
        other). Filtered by an explicit allowlist. Empty = nothing of ours to do."""
        nss = self._assertions.namespaces_for_operation(
            merge_op_id=merge_op_id, absorbed_uuid=absorbed_uuid)
        if allowed is not None:
            nss = [ns for ns in nss if ns in allowed]
        return list(nss)

    def _replay_merge_chain(
        self, op: dict[str, Any], *, downstream: dict[str, list[dict[str, Any]]],
        visited: set[tuple[str | None, str]], namespace: str | None,
        repaired: list[str], errored: list[str], downstream_replayed: list[str],
    ) -> None:
        """Re-run one merge's reconciliation, then WALK FORWARD along the merge lineage and re-run
        every downstream merge — EVEN THOSE WITH A RECEIPT (carried C.3 obligation): if A->B (op1)
        failed but B->C (op2) succeeded, repairing op1 rebinds A's assertions onto B and op2's receipt
        is now stale. `handle_merge` is idempotent, so replaying a completed downstream op is safe.
        `visited` is keyed by (namespace, op_id) so the same op can be reconciled independently per
        silo. PER-OP ISOLATION: an op whose reconciliation raises is recorded in `errored` and its
        downstream is NOT traversed (its assertions did not move), but sibling chains proceed. The root
        (first) op goes to `repaired`; successfully-replayed downstream ops to `downstream_replayed`."""
        queue: list[tuple[dict[str, Any], bool]] = [(op, False)]
        while queue:
            cur, is_down = queue.pop(0)
            op_id = str(cur.get("op_id") or "")
            key = (namespace, op_id)
            if not op_id or key in visited:
                continue
            visited.add(key)
            try:
                self.handle_merge(
                    absorbed_uuid=str(cur.get("absorbed_uuid")),
                    survivor_uuid=str(cur.get("survivor_uuid")),
                    merge_op_id=op_id, namespace=namespace)
            except Exception:  # noqa: BLE001 - isolate one op; do not traverse its (unmoved) downstream
                errored.append(op_id)
                logger.exception(
                    "reconciliation errored on merge op_id=%s (ns=%s); leaving for retry",
                    op_id, namespace)
                continue
            (downstream_replayed if is_down else repaired).append(op_id)
            for down in downstream.get(str(cur.get("survivor_uuid")), []):
                if (namespace, str(down.get("op_id") or "")) not in visited:
                    queue.append((down, True))

    def repair_incomplete_reconciliations(
        self, *, committed_merges: list[dict[str, Any]] | None = None,
        committed_unmerges: list[dict[str, Any]] | None = None,
        allowed_namespaces: set[str] | None = None,
    ) -> dict[str, Any]:
        """Repair pass (blocker 3/4 + C.4.4 transitive): re-run scalar reconciliation for every
        committed lifecycle op lacking a receipt of its OWN kind — covering the rebind-ok/rebuild-failed
        merge (no dead subject_uuid) AND the committed-unmerge-whose-scalar-hook-failed. Each op's
        NAMESPACE is derived fail-closed from its own assertions (not a caller default), scoping rebind/
        fold/rebuild to that silo; an op outside an explicit `allowed_namespaces` allowlist is skipped,
        and an op spanning >1 namespace is errored (never guessed). TRANSITIVE: re-running a merge also
        replays every downstream merge in its lineage. PER-OP ISOLATION: one failing op is recorded in
        `errored_*` (no receipt) while unrelated roots proceed. Inputs come from the operations journal:
        committed_merges [{op_id, absorbed_uuid, survivor_uuid}] (FULL lineage), committed_unmerges
        [{op_id, merge_op_id, absorbed_uuid, survivor_uuid}]. Idempotent."""
        merges = [op for op in (committed_merges or []) if str(op.get("op_id") or "")]
        downstream = self._downstream_index(merges)
        visited: set[tuple[str | None, str]] = set()
        repaired_merges: list[str] = []
        errored_merges: list[str] = []
        downstream_replayed: list[str] = []
        for op in merges:
            op_id = str(op.get("op_id"))
            # Each affected silo is checked and repaired INDEPENDENTLY: a receipt for tenant-A does not
            # mark tenant-B complete, so a partially-failed multi-silo op is still found.
            for ns in self._resolve_op_namespaces(
                    merge_op_id=op_id, absorbed_uuid=str(op.get("absorbed_uuid")),
                    allowed=allowed_namespaces):
                if self._assertions.reconcile_complete(
                        op_id, operation_kind="ENTITY_MERGE", namespace=ns):
                    continue
                if (ns, op_id) in visited:
                    continue
                self._replay_merge_chain(
                    op, downstream=downstream, visited=visited, namespace=ns,
                    repaired=repaired_merges, errored=errored_merges,
                    downstream_replayed=downstream_replayed)
        repaired_unmerges: list[str] = []
        errored_unmerges: list[str] = []
        for op in committed_unmerges or []:
            op_id = str(op.get("op_id") or "")
            if not op_id:
                continue
            # an unmerge's affected silos come from the FORWARD merge's rebind records (the exact rows
            # it will restore) UNION its own reconcile markers — never from either entity's current
            # assertions. The marker source covers the restore-ok/rebuild-failed crash, where restore
            # already deleted the rebind records but the op is still receiptless.
            for ns in self._resolve_unmerge_namespaces(
                    unmerge_op_id=op_id, merge_op_id=str(op.get("merge_op_id")),
                    allowed=allowed_namespaces):
                if self._assertions.reconcile_complete(
                        op_id, operation_kind="ENTITY_UNMERGE", namespace=ns):
                    continue
                try:
                    self.handle_unmerge(
                        survivor_uuid=str(op.get("survivor_uuid")),
                        absorbed_uuid=str(op.get("absorbed_uuid")),
                        merge_op_id=str(op.get("merge_op_id")), unmerge_op_id=op_id, namespace=ns)
                    repaired_unmerges.append(op_id)
                except Exception:  # noqa: BLE001 - isolate one unmerge; others + orphan pass proceed
                    errored_unmerges.append(op_id)
                    logger.exception(
                        "reconciliation errored on unmerge op_id=%s (ns=%s); leaving for retry",
                        op_id, ns)
        # downstream ops replayed only because an upstream op was repaired (excl. those that are roots).
        downstream_only = sorted(set(downstream_replayed) - set(repaired_merges))
        return {"repaired": len(repaired_merges) + len(repaired_unmerges),
                "merge_op_ids": repaired_merges, "unmerge_op_ids": repaired_unmerges,
                "errored_merge_op_ids": sorted(set(errored_merges)),
                "errored_unmerge_op_ids": sorted(set(errored_unmerges)),
                "downstream_replayed_op_ids": downstream_only}

    @staticmethod
    def _terminal_survivor(op: dict[str, Any], op_by_absorbed: dict[str, dict[str, Any]]) -> str:
        """Walk a merge chain forward to its FINAL survivor. `op_by_absorbed` maps an absorbed uuid to
        the op that absorbed it, so if survivor S was itself later absorbed, follow to that op's
        survivor. Cycle-guarded."""
        seen: set[str] = set()
        survivor = str(op.get("survivor_uuid") or "")
        while survivor in op_by_absorbed and survivor not in seen:
            seen.add(survivor)
            survivor = str(op_by_absorbed[survivor].get("survivor_uuid") or "")
        return survivor

    def repair_orphaned_assertions(
        self, *, committed_merges: list[dict[str, Any]] | None = None,
        allowed_namespaces: set[str] | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        """Scheduled orphan-rebind pass (carried C.3 obligation): resolve assertions whose subject
        Entity no longer exists — orphaned by a merge whose post-commit rebind never ran — to their
        surviving entity via merge lineage, then rebind + rebuild TRANSITIVELY along the chain. Each
        orphan work item is `{subject_uuid, namespace}`; the View is rebuilt/retired in the orphan's OWN
        durable namespace (never the default silo). `allowed_namespaces` is a fail-closed allowlist
        (also applied in the query). `committed_merges` is the FULL merge lineage (its `limit` is a
        history cap, independent of the orphan work `limit`). FAIL-CLOSED SURVIVOR: the chain's terminal
        survivor Entity must exist — a chain ending at another deleted entity stays `unresolved`, never
        reported repaired against a dead uuid. An orphan with no lineage is `unresolved` too. Per-row
        exceptions are isolated (`errored`). EVERY examined orphan is stamped so a bounded scan is
        eventually complete. Idempotent."""
        ns_arg = sorted(allowed_namespaces) if allowed_namespaces is not None else None
        work = self._assertions.orphaned_assertions(namespaces=ns_arg, limit=limit)
        if not work:
            return {"orphans": 0, "repaired": [], "unresolved": [], "errored": [],
                    "replayed_op_ids": []}
        # Stamp EVERY examined orphan up front — BEFORE the repair relocates it. The stamp query
        # matches by (subject_uuid, namespace); a successful rebind moves the assertion off its dead
        # subject_uuid onto the live survivor, so a stamp applied AFTER the repair would silently match
        # nothing for exactly the repaired rows (the common success case) and never advance their
        # frontier. Rebind only sets subject_uuid/rebound_at, so the stamp rides along on the moved
        # node. Stamping here (not per-outcome) keeps the contract literal: regardless of
        # repaired/unresolved/errored, an examined orphan is stamped so a bounded scan is eventually
        # complete and no sibling starves. Keyed by (subject_uuid, namespace) so tenant-A never stamps
        # tenant-B on a shared dead uuid.
        examined = [
            {"subject_uuid": str(item.get("subject_uuid") or ""), "namespace": item.get("namespace")}
            for item in work if str(item.get("subject_uuid") or "")
        ]
        if examined:
            self._assertions.mark_orphan_repair_attempted(examined, at=_utc_now_iso())
        merges = [op for op in (committed_merges or []) if str(op.get("op_id") or "")]
        downstream = self._downstream_index(merges)
        op_by_absorbed: dict[str, dict[str, Any]] = {}
        for op in merges:
            op_by_absorbed.setdefault(str(op.get("absorbed_uuid")), op)   # the op that absorbed it
        visited: set[tuple[str | None, str]] = set()
        replayed: list[str] = []
        chain_errored: list[str] = []
        repaired: list[str] = []
        unresolved: list[str] = []
        errored: list[str] = []
        for item in work:
            orphan = str(item.get("subject_uuid") or "")
            row_ns = item.get("namespace")
            if not orphan:
                continue
            if allowed_namespaces is not None and row_ns not in allowed_namespaces:
                unresolved.append(orphan)   # defensive: never touch a row outside the allowlist
                continue
            op = op_by_absorbed.get(orphan)
            if op is None:
                unresolved.append(orphan)   # no lineage -> cannot resolve a survivor
                continue
            # fail-closed survivor: the terminal survivor Entity must exist. Checked UUID-GLOBALLY,
            # matching assertion binding's identity semantics — canonical entities are not
            # tenant-scoped; namespace applies to the assertions and Views we then rebuild.
            terminal = self._terminal_survivor(op, op_by_absorbed)
            if not terminal or not self._assertions.entity_exists(terminal):
                unresolved.append(orphan)   # chain ends at a dead entity -> leave for an operator
                continue
            before_err = len(chain_errored)
            self._replay_merge_chain(
                op, downstream=downstream, visited=visited, namespace=row_ns,
                repaired=[], errored=chain_errored, downstream_replayed=replayed)
            if len(chain_errored) > before_err:
                errored.append(orphan)      # this orphan's chain hit a per-op failure
            else:
                repaired.append(orphan)
        return {"orphans": len(work), "repaired": repaired, "unresolved": unresolved,
                "errored": errored, "replayed_op_ids": sorted(op for _ns, op in visited)}
