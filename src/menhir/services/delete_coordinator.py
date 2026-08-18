"""Journaled physical-delete coordinators (plan Phase 6): ENTITY_DELETE and SESSION_TTL_DELETE.

Both delete paths were destructive with no durable before-record:

* an explicit operator delete ran a bare DETACH DELETE -- once gone, gone;
* the session TTL sweep recorded its audit BEFORE deleting, from the CANDIDATE list. Its delete then
  re-filtered on ``scope = 'SESSION'``, so a node promoted in the race window was NOT deleted -- yet
  the audit already claimed it was. It logged its intent and called that a record.

The missing durable-before-delete record is exactly why ~24 nodes destroyed by the degree-zero orphan
cleanup on 2026-07-12 were unrecoverable. Both paths now journal a COMPLETE snapshot as PREPARED
before anything is destroyed, take the exact deleted-uuid list FROM THE MUTATION, and verify absence
before COMMITTED. The audit records what happened, not what was intended.

Evidence that becomes unreferenced is REPORTED, never deleted. Isolation is not authorization --
that inference is what caused the incident above.
"""

from __future__ import annotations

import json
import logging
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import _canonical
from menhir.services.saga_writer_heartbeat import owned_mutation
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    REPLAYED,
    SKIP,
    SKIPPED,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    summarize_outcomes,
)

logger = logging.getLogger(__name__)

NOTHING_TO_DELETE = "NOTHING_TO_DELETE"
PREPARE_FAILED = "PREPARE_FAILED"
SNAPSHOT_FAILED = "SNAPSHOT_FAILED"


class DeleteDrift(RuntimeError):
    """The graph did not reach the expected after-state (targets still present)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeleteCoordinator:
    """The sanctioned entry point for physical deletion of Entity nodes."""

    graph_adapter: Any
    journal: GraphOperationsJournal

    def __post_init__(self) -> None:
        self.journal._ensure_ready()

    # ------------------------------------------------------------------ explicit operator delete
    def delete_entity(self, node_uuid: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Explicit operator delete of ONE Entity, with a complete durable snapshot first.

        This remains intentionally destructive -- the plan does not add a public undelete -- but the
        snapshot is retained so the deletion is auditable and, if a recovery tool is ever built, the
        node is not gone beyond reconstruction.
        """
        return self._run(
            kind="ENTITY_DELETE",
            targets=[node_uuid],
            require_scope=None,
            trigger="operator_delete",
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------ session TTL sweep
    def delete_expired_session_nodes(
        self, *, session_id: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Delete SESSION nodes whose demotion TTL has expired.

        The scope filter stays on the mutation (a node promoted in the race window must survive), but
        the audit is now taken from the mutation's RETURN, so a target that escaped deletion is
        recorded as ``skipped``, never as deleted.
        """
        expired = self.graph_adapter.fetch_ttl_expired_session_uuids(session_id)
        targets = [str(r.get("uuid")) for r in (expired or []) if r.get("uuid")]
        if not targets:
            return {"deleted": [], "skipped": [], "reason": NOTHING_TO_DELETE}
        return self._run(
            kind="SESSION_TTL_DELETE",
            targets=targets,
            require_scope="SESSION",
            trigger="demote_ttl_expiry",
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------ the saga
    def _run(
        self,
        *,
        kind: str,
        targets: list[str],
        require_scope: str | None,
        trigger: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        targets = [t for t in dict.fromkeys(targets) if t]
        if not targets:
            return {"deleted": [], "skipped": [], "reason": NOTHING_TO_DELETE}

        # Complete before-snapshot of every target. Reuses the Phase 4 lossless capture, so a deleted
        # node's labels, typed properties, and full incident graph are all preserved.
        try:
            snapshots = {}
            for uuid in targets:
                state = self.graph_adapter.capture_node_state(uuid)
                if state is not None:
                    snapshots[uuid] = state
        except ms.SnapshotSchemaError as exc:
            return {"deleted": [], "skipped": targets, "reason": SNAPSHOT_FAILED,
                    "diagnostics": {"error": str(exc)}}

        present = sorted(snapshots)
        already_absent = sorted(set(targets) - set(present))
        if not present:
            # Every target is already gone: nothing to destroy, nothing to journal.
            return {"deleted": [], "skipped": [], "already_absent": already_absent,
                    "reason": NOTHING_TO_DELETE}

        # Evidence that these deletions would leave unreferenced. Reported, NOT deleted.
        orphan_evidence = self.graph_adapter.newly_unreferenced_evidence(present)

        if dry_run:
            return {
                "deleted": [], "skipped": [], "dry_run": True,
                "would_delete": present,
                "already_absent": already_absent,
                "newly_unreferenced_evidence": orphan_evidence,
            }

        op_id = uuidlib.uuid4().hex
        request = {
            "op_id": op_id,
            "kind": kind,
            "trigger": trigger,
            "targets": present,
            "require_scope": require_scope,
            "requested_at": _utc_now_iso(),
        }
        snapshot_payload = {
            "schema_version": ms.SCHEMA_VERSION,
            "nodes": snapshots,
            "newly_unreferenced_evidence": orphan_evidence,
        }

        # PREPARED -- durable before anything is destroyed (invariant 3). If this fails, nothing is
        # deleted: a delete may only proceed once its recovery record exists.
        try:
            snapshot_json = ms.dumps(snapshot_payload, enforce_size_limit=False)
            self.journal.prepare(
                operation_kind=kind,
                request_json=_canonical(request),
                target_uuid=present[0] if len(present) == 1 else None,
                target_key=None,  # a delete batch is not a single fenceable key
                before_snapshot_json=snapshot_json,
                op_id=op_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s abstained: PREPARE failed: %s", kind, exc)
            return {"deleted": [], "skipped": present, "reason": PREPARE_FAILED,
                    "diagnostics": {"error": str(exc)}}

        # Everything from here to the terminal journal transition runs under this process's
        # ownership heartbeat (CF-211 part 2), so a reconciler can tell "still deleting here" from
        # "crashed midway". Unlike the other three coordinators the mutation is inline rather than in
        # an _apply, so the scope is opened here -- immediately after PREPARE, which is the first
        # moment a claim exists to hold.
        with owned_mutation(self.journal, op_id, operation_kind=kind):
            return self._mutate_and_verify(
                op_id=op_id, kind=kind, present=present, require_scope=require_scope,
                already_absent=already_absent, orphan_evidence=orphan_evidence,
            )

    def _mutate_and_verify(
        self,
        *,
        op_id: str,
        kind: str,
        present: list[str],
        require_scope: str | None,
        already_absent: list[str],
        orphan_evidence: list[str],
    ) -> dict[str, Any]:
        """Destroy, verify absence, then transition. Runs inside the ownership heartbeat scope."""
        # MUTATE -- the exact deleted set comes back FROM the mutation, not from our intent.
        try:
            deleted = self.graph_adapter.delete_entities_returning_uuids(
                present, require_scope=require_scope
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        deleted = sorted(deleted)
        # A target that survived (e.g. promoted out of SESSION scope between the read and the write)
        # is CONFLICTED, not deleted. The old code would have logged it as deleted.
        skipped = sorted(set(present) - set(deleted))

        # VERIFY absence before COMMITTED. A target we believe we deleted must actually be gone.
        still_present = [u for u in deleted if self.graph_adapter.capture_node_state(u) is not None]
        if still_present:
            self.journal.mark_needs_review(
                op_id,
                observed_error=f"nodes reported deleted are still present: {still_present}",
            )
            raise DeleteDrift(f"{kind} (op {op_id}): {still_present} still present after delete")

        self.journal.mark_committed(op_id)
        return {
            "op_id": op_id,
            "deleted": deleted,
            "skipped": skipped,          # requested, but not deleted (conflicted)
            "already_absent": already_absent,
            "newly_unreferenced_evidence": orphan_evidence,  # reported; never deleted
        }

    # ------------------------------------------------------------------ recovery reads
    def load_snapshot(self, op_id: str) -> dict[str, Any]:
        """The complete before-snapshot of a delete. Survives the nodes it describes."""
        row = self.journal.get(op_id)
        if row is None or not row.get("before_snapshot_json"):
            raise ms.SnapshotSchemaError(f"operation {op_id!r} has no snapshot")
        return ms.loads(row["before_snapshot_json"])

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify ONE PREPARED journal row. Pure: performs no durable mutation.

        A delete left PREPARED by a crash is never replayed -- re-running it would destroy nodes
        that a crash spared. So this only OBSERVES whether the delete already happened: if every
        target is gone the delete completed (mark already applied); if any survive an operator
        decides (needs review).
        """
        operation_kind = row.get("operation_kind")
        if operation_kind not in ("ENTITY_DELETE", "SESSION_TTL_DELETE"):
            return SKIP, {}
        # Structural problems with the ROW are classified; infrastructure failures are NOT.
        #
        # This method is the one seam used by BOTH the live sweep and the dry-run, so the catch here
        # has to be narrow. A broad `except Exception` would fold a transient graph outage into
        # WOULD_NEEDS_REVIEW, and in live mode that quarantines the row -- which fences its
        # participants and demands an operator -- when the correct outcome is to leave it PREPARED
        # and retry later. So capture_node_state below is deliberately OUTSIDE any handler. The
        # central dispatcher wraps handler calls broadly, which is what keeps an OBSERVE pass alive
        # without granting a live sweep permission to quarantine on an outage.
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError, KeyError):
            return WOULD_NEEDS_REVIEW, {"observed_error": "unparseable request_json"}
        try:
            targets = [str(t) for t in request.get("targets") or []]
        except (AttributeError, TypeError) as exc:
            return WOULD_NEEDS_REVIEW, {
                "observed_error": f"unclassifiable row: {type(exc).__name__}: {exc}"
            }
        survivors = [
            u for u in targets if self.graph_adapter.capture_node_state(u) is not None
        ]
        if survivors:
            return WOULD_NEEDS_REVIEW, {
                "survivors": survivors,
                "observed_error": (
                    f"crash left the delete incomplete; still present: {survivors}. "
                    "NOT retried automatically -- deleting them now could destroy nodes the "
                    "crash spared."
                ),
            }
        return WOULD_MARK_ALREADY_APPLIED, {}

    def reconcile(self, *, limit: int = 500, dry_run: bool = True) -> dict[str, Any]:
        """Classify the PREPARED backlog for this saga kind. Observation only.

        Live replay is NOT available here. A per-coordinator sweep cannot acquire the global
        PREPARE gate, cannot establish that a row's original writer is gone, and cannot atomically
        claim an abandoned row before touching the graph -- so replaying from here would mutate
        rows another process may still be executing. There is exactly one live replay authority,
        and it is the central dispatcher.

        The heartbeat that ``_apply`` opens does not close that hole either: it renews on an
        interval, so a reconciler acting on somebody else's row would dispatch its first mutation
        before the first renewal discovered the row was never its to claim.

        ``dry_run`` now defaults to True. Passing False raises rather than silently observing, so a
        caller cannot believe recovery ran.
        """
        if not dry_run:
            raise NotImplementedError(
                "per-coordinator live reconciliation is disabled: recovery must go through the "
                "central dispatcher, which holds the reconciliation gate, checks operation "
                "ownership, and claims an abandoned row before mutating. Use reconcile() to "
                "classify, or _replay_prepared() from an authority that already owns the rows."
            )
        return self._reconcile_sweep(limit=limit, dry_run=True)

    def _replay_prepared(self, *, limit: int = 500) -> dict[str, Any]:
        """The live replay sweep. Callable ONLY by an authority that already owns the rows.

        Private and unreachable through reconcile(). A caller must have taken the reconciliation
        gate, established that each row's original writer is gone, and claimed the row -- none of
        which this method does or can check for itself.

        It exists under a separate name rather than being deleted because it is the saga's only
        executable replay implementation, and the crash-recovery invariants it satisfies still have
        to be provable: a PREPARED row replays exactly once, drift quarantines without mutating, a
        missing precondition fails closed. Deleting it would have removed that evidence along with
        the unsafe entry point.
        """
        return self._reconcile_sweep(limit=limit, dry_run=False)

    def replay_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Resolve ONE PREPARED delete row. The live counterpart to :meth:`classify_prepared_row`.

        Nothing is ever re-executed here, and that is the point: re-running a delete would destroy
        nodes a crash spared. This only records the truth the graph already tells -- every target
        gone means the delete completed and the row commits; any survivor means an operator decides.
        REPLAYED therefore reports "reached a terminal state", not "mutated the graph".

        **The caller must already hold the right to touch this row.** Ownership is deliberately not
        re-checked here; the only sound place for that check is inside the claim transaction the
        caller holds.
        """
        op_id = str(row["op_id"])
        outcome, diagnostics = self.classify_prepared_row(row)
        if outcome == SKIP:
            return SKIPPED, {}
        if outcome == WOULD_MARK_ALREADY_APPLIED:
            self.journal.mark_committed(op_id)
            return REPLAYED, {}
        if outcome == WOULD_NEEDS_REVIEW:
            observed = diagnostics["observed_error"]
            self.journal.mark_needs_review(op_id, observed_error=observed)
            diag: dict[str, Any] = {"observed_error": observed}
            if "survivors" in diagnostics:
                diag["survivors"] = diagnostics["survivors"]
            return DRIFTED, diag
        # A classification this coordinator does not emit. The sweep used to fall through silently;
        # surfacing it is what lets the dispatcher notice a contract break instead of a quiet no-op.
        return FAILED, {"observed_error": f"unhandled delete classification {outcome!r}"}

    def _reconcile_sweep(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """A delete left PREPARED by a crash: determine whether it happened, and record the truth.

        There is nothing to replay -- re-running a delete would destroy nodes that a crash spared. So
        this only OBSERVES: if every target is gone, the delete completed and the row commits; if any
        survive, an operator decides.

        ``dry_run`` performs every read exactly as live mode does but mutates nothing: no journal
        write of any kind. It adds ``scanned``, ``counts`` and the per-row ``outcomes``.
        """
        committed = 0
        review = 0
        scanned = 0
        outcomes: list[dict[str, Any]] = []
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            scanned += 1
            op_id = str(row["op_id"])
            if dry_run:
                # Classified here rather than above the branch so each mode performs EXACTLY one
                # classification per row. classify_prepared_row reads graph state; running it in
                # both the forecast and the action path would double every read in live mode.
                outcome, diagnostics = self.classify_prepared_row(row)
                entry: dict[str, Any] = {
                    "op_id": op_id,
                    "operation_kind": row.get("operation_kind"),
                    "outcome": outcome,
                }
                if "observed_error" in diagnostics:
                    entry["observed_error"] = diagnostics["observed_error"]
                if "survivors" in diagnostics:
                    entry["survivors"] = diagnostics["survivors"]
                outcomes.append(entry)
                continue
            live = self.replay_prepared_row(row)[0]
            if live == REPLAYED:
                committed += 1
            elif live == DRIFTED:
                review += 1
        if dry_run:
            # committed/needs_review stay 0: they count journal transitions PERFORMED, and a
            # dry-run performs none. Reporting them as if they had happened is the same lie as
            # calling the happy path WOULD_COMMIT. The forecast lives in counts/outcomes.
            return {
                "committed": committed,
                "needs_review": review,
                "dry_run": True,
                "scanned": scanned,
                "counts": summarize_outcomes(outcomes),
                "outcomes": outcomes,
            }
        return {"committed": committed, "needs_review": review}


__all__ = ["DeleteCoordinator", "DeleteDrift", "NOTHING_TO_DELETE", "PREPARE_FAILED"]
