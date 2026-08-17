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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import _canonical
from menhir.services.saga_reconcile_outcomes import (
    SKIP,
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

    def reconcile(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
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
            operation_kind = row.get("operation_kind")
            if operation_kind not in ("ENTITY_DELETE", "SESSION_TTL_DELETE"):
                if dry_run:
                    outcomes.append({
                        "op_id": op_id,
                        "operation_kind": operation_kind,
                        "outcome": SKIP,
                    })
                continue
            try:
                request = json.loads(row["request_json"])
            except (TypeError, ValueError):
                if dry_run:
                    outcomes.append({
                        "op_id": op_id,
                        "operation_kind": operation_kind,
                        "outcome": WOULD_NEEDS_REVIEW,
                    })
                else:
                    self.journal.mark_needs_review(op_id, observed_error="unparseable request_json")
                    review += 1
                continue
            targets = [str(t) for t in request.get("targets") or []]
            survivors = [
                u for u in targets if self.graph_adapter.capture_node_state(u) is not None
            ]
            if survivors:
                if dry_run:
                    outcomes.append({
                        "op_id": op_id,
                        "operation_kind": operation_kind,
                        "outcome": WOULD_NEEDS_REVIEW,
                        "survivors": survivors,
                    })
                else:
                    self.journal.mark_needs_review(
                        op_id,
                        observed_error=(
                            f"crash left the delete incomplete; still present: {survivors}. "
                            "NOT retried automatically -- deleting them now could destroy nodes the "
                            "crash spared."
                        ),
                    )
                    review += 1
            else:
                if dry_run:
                    outcomes.append({
                        "op_id": op_id,
                        "operation_kind": operation_kind,
                        "outcome": WOULD_MARK_ALREADY_APPLIED,
                    })
                else:
                    self.journal.mark_committed(op_id)
                    committed += 1
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
