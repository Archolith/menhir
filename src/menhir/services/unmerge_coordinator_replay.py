"""Replay half of the unmerge saga: classification, row replay, and reconciliation sweeps.

``UnmergeReplayMixin`` carries the pure pre-mutation decision (``_classify_replay``, surfaced per
journal row via ``classify_prepared_row``), the observation-only ``reconcile`` entry point, the
live row replay (``replay_prepared_row`` / ``_replay_prepared``), and the ``_reconcile_sweep``
sweep. It is mixed into
:class:`~menhir.services.unmerge_coordinator.UnmergeCoordinator`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from menhir.domain import merge_snapshot as ms
from menhir.services.merge_coordinator import (
    MergeDrift,
    merge_state_fingerprint,
)
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    REPLAYED,
    SKIP,
    SKIPPED,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_RESTORE,
    summarize_outcomes,
)

# These methods used to live in the coordinator module; pin its logger namespace so log records
# stay attributable to menhir.services.unmerge_coordinator (same pattern as the
# metric_write_coordinator_reconcile sibling).
logger = logging.getLogger("menhir.services.unmerge_coordinator")


class UnmergeReplayMixin:
    """Replay classification, row replay, and reconciliation sweeps.

    Expects the host dataclass (:class:`~menhir.services.unmerge_coordinator.UnmergeCoordinator`)
    to provide ``graph_adapter``, ``journal``, the snapshot-to-plan builder ``_build_restore_plan``,
    and the owned-mutation ``_apply`` entry point.
    """

    def _classify_replay(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify a PREPARED unmerge row WITHOUT mutating anything.

        Returns (outcome, diagnostics): exactly the pre-mutation decision that live replay would
        make, so a dry-run can report it without touching the journal or the graph. This method is
        PURE -- it must never call mark_committed / mark_needs_review / _mark_merge_reversed /
        _fire_unmerge_hook / record_attempt / restore_merge_snapshot.
        """
        op_id = str(request["op_id"])
        survivor_uuid = str(request["survivor_uuid"])
        absorbed_uuid = str(request["absorbed_uuid"])
        merge_op_id = str(request["merge_op_id"])
        row = self.journal.get(op_id) or {}
        expected_after = row.get("expected_after_sha256")
        expected_before = request.get("expected_before_sha256")

        observed_fp = merge_state_fingerprint(
            self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid), op_id=op_id
        )

        diagnostics = {
            "op_id": op_id,
            "survivor_uuid": survivor_uuid,
            "absorbed_uuid": absorbed_uuid,
            "merge_op_id": merge_op_id,
            "observed_fp": observed_fp,
            "expected_before": expected_before,
            "expected_after": expected_after,
        }

        if expected_after and observed_fp == expected_after:
            return (WOULD_MARK_ALREADY_APPLIED, diagnostics)
        if expected_before is None:
            diagnostics["observed_error"] = (
                "request has no expected_before_sha256; cannot verify precondition"
            )
            return (WOULD_NEEDS_REVIEW, diagnostics)
        if observed_fp != expected_before:
            diagnostics["observed_error"] = (
                f"precondition drift: observed={observed_fp} "
                f"expected_before={expected_before} expected_after={expected_after}"
            )
            return (WOULD_NEEDS_REVIEW, diagnostics)
        return (WOULD_RESTORE, diagnostics)

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify ONE PREPARED journal row. Pure: performs no durable mutation.

        Returns ``(outcome, diagnostics)``. This is the single classification path shared with the
        CF-20b dispatcher and ``reconcile(dry_run=True)``, so a direct dry-run can never disagree
        with it. Outcomes come from ``saga_reconcile_outcomes``. ``observed_error`` is present in
        the diagnostics whenever the outcome is ``WOULD_NEEDS_REVIEW``.
        """
        op_id = str(row["op_id"])
        kind = row.get("operation_kind")
        diagnostics: dict[str, Any] = {
            "op_id": op_id,
            "operation_kind": kind,
        }

        # 1. A row this coordinator does not handle (defensive only -- the dispatcher routes by
        #    kind, and LEGACY_ENTITY_UNMERGE is a DIFFERENT kind a different coordinator owns).
        if kind != "ENTITY_UNMERGE":
            return (SKIP, diagnostics)

        # 2. request_json that will not parse.
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError, KeyError):
            diagnostics["observed_error"] = "unparseable request_json"
            return (WOULD_NEEDS_REVIEW, diagnostics)

        # 3. The pure PREP -- this coordinator is the only one with this step. A dry-run must
        #    prove these reads SUCCEED, not merely that nothing was written: a restorable row only
        #    reaches WOULD_RESTORE if the snapshot loads AND the restore plan builds.
        try:
            body = ms.load_snapshot(ms.loads(row["before_snapshot_json"]))
            _ = self._build_restore_plan(
                ms.decode_node(body["survivor"]), ms.decode_node(body["absorbed"])
            )
        except (TypeError, ValueError, ms.SnapshotSchemaError) as exc:
            diagnostics["observed_error"] = f"unreplayable row: {exc}"
            return (WOULD_NEEDS_REVIEW, diagnostics)

        # 4. The classification itself. A legacy row missing a field must never abort the scan and
        #    hide every newer row behind it. Narrow on purpose: these are the shapes a malformed ROW
        #    produces. A graph outage is not a row defect, and folding it in would let a caller
        #    acting on this outcome quarantine a good row over a transient failure. The dispatcher
        #    catches the rest, so an observe pass still cannot die on one handler.
        try:
            outcome, diag = self._classify_replay(request)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            diagnostics["observed_error"] = f"unclassifiable row: {type(exc).__name__}: {exc}"
            return (WOULD_NEEDS_REVIEW, diagnostics)

        # 5. The coordinator's own classification. Ensure the shared keys reconcile needs are set.
        if "operation_kind" not in diag:
            diag["operation_kind"] = kind
        return (outcome, diag)

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
        """Restore ONE PREPARED unmerge row. The live counterpart to :meth:`classify_prepared_row`.

        Extracted so the central dispatcher can act on a row it has just CLAIMED. A whole-backlog
        sweep cannot do that: ownership belongs to an individual row, and the claim authorising a
        mutation must immediately precede it.

        **The caller must already hold the right to touch this row.** Ownership is deliberately not
        re-checked here; the only sound place for that check is inside the claim transaction the
        caller holds, and repeating it here would look like a safety net while being a race.

        An unreplayable snapshot quarantines rather than failing: unlike a transient outage, a
        snapshot that cannot be decoded will never decode on a later pass, so retrying it forever
        would starve the backlog instead of surfacing the problem.
        """
        op_id = str(row["op_id"])
        if row.get("operation_kind") != "ENTITY_UNMERGE":
            return SKIPPED, {}
        try:
            request = json.loads(row["request_json"])
            body = ms.load_snapshot(ms.loads(row["before_snapshot_json"]))
            plan = self._build_restore_plan(
                ms.decode_node(body["survivor"]), ms.decode_node(body["absorbed"])
            )
        except (TypeError, ValueError, ms.SnapshotSchemaError) as exc:
            observed = f"unreplayable row: {exc}"
            self.journal.mark_needs_review(op_id, observed_error=observed)
            return DRIFTED, {"observed_error": observed}
        try:
            self._apply(request, plan)
            return REPLAYED, {}
        except MergeDrift as exc:
            return DRIFTED, {"observed_error": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- reported per row; one bad row must not abort
            logger.warning("unmerge saga: replay of op %s failed: %s", op_id, exc)
            return FAILED, {"observed_error": f"{type(exc).__name__}: {exc}"}

    def _reconcile_sweep(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """Replay every ENTITY_UNMERGE left PREPARED by a crash, or report what WOULD happen.

        ``dry_run=True`` routes every scanned row through ``classify_prepared_row`` -- the single
        classification path shared with the CF-20b dispatcher -- and mutates nothing. The
        classification is the observation contract (CF-20a); the counters stay at zero because
        nothing is replayed, and ``outcomes`` carries the per-row decision.
        """
        replayed = 0
        drifted = 0
        failed = 0
        scanned = 0
        outcomes: list[dict[str, Any]] = []
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            scanned += 1
            op_id = str(row["op_id"])
            kind = row.get("operation_kind")
            if dry_run:
                # Every scanned row gets exactly one outcome; a malformed row must not abort the
                # scan (see the note in MergeCoordinator). classify_prepared_row handles the
                # kind-mismatch SKIP, the unparseable request, the pure prep, and the classifier.
                outcome, diag = self.classify_prepared_row(row)
                entry: dict[str, Any] = {
                    "op_id": op_id,
                    "operation_kind": kind,
                    "outcome": outcome,
                }
                if "observed_error" in diag:
                    entry["observed_error"] = diag["observed_error"]
                outcomes.append(entry)
                continue
            outcome = self.replay_prepared_row(row)[0]
            if outcome == REPLAYED:
                replayed += 1
            elif outcome == DRIFTED:
                drifted += 1
            elif outcome == FAILED:
                failed += 1
        if dry_run:
            # replayed/drifted/failed stay 0 in dry-run: they count actions PERFORMED, and a
            # dry-run performs none. The forecast lives in counts/outcomes instead.
            return {
                "replayed": replayed,
                "drifted": drifted,
                "failed": failed,
                "dry_run": True,
                "scanned": scanned,
                "counts": summarize_outcomes(outcomes),
                "outcomes": outcomes,
            }
        return {"replayed": replayed, "drifted": drifted, "failed": failed}
