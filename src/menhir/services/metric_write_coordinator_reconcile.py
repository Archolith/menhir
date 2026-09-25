"""Replay half of the Metric saga: classification, preconditioned mutation, and sweeps.

``MetricReconcileMixin`` carries the pure decision (``_classify_replay``, surfaced per journal row
via ``classify_prepared_row``), the mutation that branches on it (``_apply`` / ``_apply_owned``),
and the sweeps (``reconcile``, ``_replay_prepared``, ``replay_prepared_row``,
``_reconcile_sweep``). It is mixed into
:class:`~menhir.services.metric_write_coordinator.MetricWriteCoordinator`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from typing import Any

from menhir.services.metric_write_coordinator_common import MetricDrift, state_fingerprint
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    REPLAYED,
    SKIP,
    SKIPPED,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    summarize_outcomes,
)
from menhir.services.saga_writer_heartbeat import owned_mutation

# These methods used to live in the coordinator module; pin its logger namespace so log records
# stay attributable to menhir.services.metric_write_coordinator (same pattern as the
# scalar_state_service_* siblings).
logger = logging.getLogger("menhir.services.metric_write_coordinator")


class MetricReconcileMixin:
    """Replay classification, mutation, and reconciliation sweeps.

    Expects the host dataclass (:class:`MetricWriteCoordinator`) to provide ``graph_adapter``,
    ``journal``, and the producer half's ``_saga_write`` companion ``_apply`` entry point.
    """

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify ONE PREPARED journal row. Pure: performs no durable mutation.

        ``row`` is a journal row dict (``op_id``, ``operation_kind``, ``request_json``,
        ...), NOT the parsed request. This thin wrapper layers the row-level concerns on
        top of ``_classify_replay``: the kind check, the ``request_json`` parse, and the
        "classifier raised" guard. A malformed row must never propagate an exception out
        of here -- one bad old row must not abort a scan and hide every newer row behind it.
        """
        operation_kind = row.get("operation_kind")
        if operation_kind != "METRIC_WRITE":
            return SKIP, {}
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError, KeyError):
            return WOULD_NEEDS_REVIEW, {"observed_error": "unparseable request_json"}
        # Narrow on purpose: these are the shapes a malformed ROW produces. A graph outage is not a
        # row defect, and folding it in would let a caller acting on this outcome quarantine a good
        # row over a transient failure. The dispatcher catches the rest, so an observe pass survives.
        try:
            return self._classify_replay(request)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return (
                WOULD_NEEDS_REVIEW,
                {"observed_error": f"unclassifiable row: {type(exc).__name__}: {exc}"},
            )

    def _classify_replay(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify a PREPARED request WITHOUT mutating anything (CF-20a observation contract).

        Returns ``(outcome, diagnostics)`` where ``outcome`` is one of the CF-20a replay
        vocabulary and ``diagnostics`` carries everything a live replay needs to act without
        re-reading the graph: ``op_id``, ``view_key``, ``observed``, ``observed_fp``,
        ``expected_before``, ``expected_after``, and for the two WOULD_NEEDS_REVIEW cases the
        exact ``observed_error`` string a live replay would pass to ``mark_needs_review``.

        Pure by construction: it only reads (the journal row and the graph) and never calls
        ``mark_committed``, ``mark_needs_review``, ``record_attempt``, or ``record_metric``.
        A dry-run can call it freely; ``_apply`` calls it exactly once and branches on the
        outcome, so the decision and the mutation cannot drift apart.
        """
        op_id = str(request["op_id"])
        key = str(request["view_key"])
        row = self.journal.get(op_id) or {}
        expected_after = row.get("expected_after_sha256")
        expected_before = request.get("expected_before_sha256")

        observed = self.graph_adapter.fetch_metric_state(view_key=key)
        observed_fp = state_fingerprint(observed, view_key=key)

        diag: dict[str, Any] = {
            "op_id": op_id,
            "view_key": key,
            "observed": observed,
            "observed_fp": observed_fp,
            "expected_before": expected_before,
            "expected_after": expected_after,
        }

        # (2) Already applied by a previous attempt that crashed before COMMITTED. The receipt
        # pointer proves THIS op produced it, so replaying is a no-op, not a conflict.
        if expected_after and observed_fp == expected_after:
            return WOULD_MARK_ALREADY_APPLIED, diag

        # (3) Neither expected state: refuse to mutate. A half-applied or externally-modified node
        # is an operator decision, not something a background replay should paper over.
        #
        # FAIL CLOSED: a request with no frozen precondition cannot be verified, so it must NOT be
        # applied. Waving it through would mutate a possibly-drifted graph with no check at all --
        # the opposite of the guarantee this contract exists to provide.
        if expected_before is None:
            diag["observed_error"] = (
                "request has no expected_before_sha256; cannot verify precondition"
            )
            return WOULD_NEEDS_REVIEW, diag
        if observed_fp != expected_before:
            diag["observed_error"] = (
                f"precondition drift: observed={observed_fp} "
                f"expected_before={expected_before} expected_after={expected_after}"
            )
            return WOULD_NEEDS_REVIEW, diag

        # (1) Expected before-state -> apply.
        return WOULD_REPLAY, diag

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run the saga body under this process's ownership heartbeat (CF-211 part 2).

        The heartbeat is what lets a reconciler tell "still running here" from "crashed midway": it
        renews the claim on a thread, independently of the blocking driver call, and publishes a
        revocation predicate that stops any further statement being dispatched once the claim is
        lost. Wrapping the whole body means the claim is held from PREPARE through the terminal
        journal transition, which is the interval a reconciler must not replay across.

        The TTL is derived for METRIC_WRITE specifically, so its statement count -- not a shared
        constant -- determines how long an expired claim takes to become recoverable.
        """
        with owned_mutation(
            self.journal, str(request["op_id"]), operation_kind="METRIC_WRITE"
        ):
            return self._apply_owned(request)

    def _apply_owned(self, request: dict[str, Any]) -> dict[str, Any]:
        """Preconditioned, idempotent graph mutation for a PREPARED request (plan E3).

        Exactly three accepted states -- the precondition is checked BEFORE any mutation, so a
        drifted graph is never touched:

          1. observed == expected BEFORE-state  -> apply, verify the after-state, COMMITTED
          2. observed == expected AFTER-state   -> already applied (crash before COMMITTED):
                                                   no-op replay, COMMITTED
          3. anything else                      -> DO NOT MUTATE; NEEDS_REVIEW for an operator

        Used by both the initial write and reconciliation replay -- one code path, so a replay
        cannot diverge from the original.

        The pre-mutation decision lives in ``_classify_replay``; this method calls it once,
        branches on the outcome, and reuses the observed state out of the diagnostics so there
        is exactly one ``fetch_metric_state`` call on the precondition path.
        """
        outcome, diag = self._classify_replay(request)
        op_id = diag["op_id"]
        key = diag["view_key"]
        expected_after = diag["expected_after"]
        expected_before = diag["expected_before"]
        observed_fp = diag["observed_fp"]

        # (2) Already applied by a previous attempt that crashed before COMMITTED. The receipt
        # pointer proves THIS op produced it, so replaying is a no-op, not a conflict.
        if outcome == WOULD_MARK_ALREADY_APPLIED:
            self.journal.mark_committed(op_id)
            return {
                "uuid": str(request["metric_uuid"]), "view_key": key, "op_id": op_id,
                "created": False, "replayed": True,
            }

        # (3) Neither expected state: refuse to mutate. A half-applied or externally-modified node
        # is an operator decision, not something a background replay should paper over.
        #
        # FAIL CLOSED: a request with no frozen precondition cannot be verified, so it must NOT be
        # applied. Waving it through would mutate a possibly-drifted graph with no check at all --
        # the opposite of the guarantee this contract exists to provide.
        if outcome == WOULD_NEEDS_REVIEW:
            self.journal.mark_needs_review(op_id, observed_error=diag["observed_error"])
            if expected_before is None:
                raise MetricDrift(
                    f"op {op_id} for {key!r} has no frozen precondition; NOT mutating (fail closed)"
                )
            raise MetricDrift(
                f"precondition drift for {key!r} (op {op_id}): the graph is in neither the "
                f"expected before- nor after-state; NOT mutating"
            )

        # (1) Expected before-state -> apply.
        try:
            res = self.graph_adapter.record_metric(
                subject=request["subject"],
                counter=request["counter"],
                value=float(request["value"]),
                namespace=request["namespace"],
                valid_at=request["valid_at"],
                source=request["source"],
                receipt_op_id=op_id,
                # the uuid frozen at PREPARE: a replay recreates THIS node, never a fork
                node_uuid=str(request["metric_uuid"]),
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        # LWW stale-skip: the counter kind is an LWW register, so _write_version REFUSES to let a
        # temporally-older value overwrite a newer current version -- it returns without writing.
        # The fence serialises ops (the frozen valid_at is never older than the current node's), so
        # this should be unreachable. Handle it explicitly anyway: silently falling through would
        # produce a cryptic "fingerprint mismatch" and fence the key, when the real cause is a stale
        # replay. Fail with an accurate diagnosis instead of a misleading one.
        if res.get("stale_skipped"):
            self.journal.mark_needs_review(
                op_id,
                observed_error=(
                    "LWW stale-skip: the graph holds a NEWER version than this operation's frozen "
                    f"valid_at ({request.get('valid_at')}); the write was refused, not applied"
                ),
            )
            raise MetricDrift(
                f"stale replay for {key!r} (op {op_id}): a newer version is current; NOT applied"
            )

        # Verify the mutation produced exactly the after-state frozen at PREPARE.
        after = self.graph_adapter.fetch_metric_state(view_key=key)
        actual = state_fingerprint(after, view_key=key)
        if expected_after and actual != expected_after:
            self.journal.mark_needs_review(
                op_id, observed_error=f"after-state mismatch expected={expected_after} actual={actual}"
            )
            raise MetricDrift(
                f"after-state drift for {key!r} (op {op_id}): expected {expected_after}, "
                f"observed {actual}"
            )

        self.journal.mark_committed(op_id)
        res["op_id"] = op_id
        return res

    # ------------------------------------------------------------------ reconciliation
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
        """Replay ONE PREPARED metric write. The live counterpart to :meth:`classify_prepared_row`.

        Extracted so the central dispatcher can replay a row it has just CLAIMED. A whole-backlog
        sweep cannot serve that purpose: ownership belongs to an individual row, and the claim that
        authorises a mutation has to immediately precede that mutation.

        **The caller must already hold the right to touch this row.** Ownership is deliberately not
        re-checked here; the only sound place for that check is inside the claim transaction the
        caller holds.
        """
        op_id = str(row["op_id"])
        if row.get("operation_kind") != "METRIC_WRITE":
            return SKIPPED, {}
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError):
            self.journal.mark_needs_review(op_id, observed_error="unparseable request_json")
            return DRIFTED, {"observed_error": "unparseable request_json"}
        try:
            self._apply(request)
            logger.info(
                "metric saga: replayed PREPARED op %s (%s)", op_id, request.get("view_key")
            )
            return REPLAYED, {}
        except MetricDrift as exc:
            logger.warning("metric saga: op %s drifted; left NEEDS_REVIEW", op_id)
            return DRIFTED, {"observed_error": str(exc)}
        except Exception as exc:  # noqa: BLE001 -- reported per row; one bad row must not abort
            logger.warning("metric saga: replay of op %s failed: %s", op_id, exc)
            return FAILED, {"observed_error": f"{type(exc).__name__}: {exc}"}

    def _reconcile_sweep(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """Replay every PREPARED METRIC_WRITE left by a crash (plan E4).

        Runs at startup, AFTER schema readiness and BEFORE scheduler jobs register, so no new
        writer competes with an in-flight operation. Replay is idempotent: record_metric is
        signature-idempotent and the request is frozen, so re-applying a completed mutation
        converges to the same node instead of creating a second version.

        Drift is reported, never repaired: a NEEDS_REVIEW row stays for an operator.

        With ``dry_run=True`` the same deterministic decision is made via ``_classify_replay``
        for every row but NOTHING is mutated -- no ``_apply``, no ``mark_needs_review``, no
        graph write. Each row is classified and reported in ``outcomes``; live mode reaches the
        same classification and then acts on it, so a dry-run summary and a live replay can
        never diverge.
        """
        replayed = 0
        drifted = 0
        failed = 0
        scanned = 0
        outcomes: list[dict[str, Any]] = []
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            scanned += 1
            operation_kind = row.get("operation_kind")
            op_id = str(row["op_id"])
            if dry_run:
                if operation_kind != "METRIC_WRITE":
                    outcomes.append(
                        {"op_id": op_id, "operation_kind": operation_kind, "outcome": SKIP}
                    )
                    continue
                try:
                    json.loads(row["request_json"])
                except (TypeError, ValueError):
                    outcomes.append(
                        {"op_id": op_id, "operation_kind": operation_kind,
                         "outcome": WOULD_NEEDS_REVIEW,
                         "observed_error": "unparseable request_json"}
                    )
                    continue
                # A malformed row must not abort the scan -- see the note in MergeCoordinator:
                # one unclassifiable old row must never hide the newer rows behind it. The
                # row-level guard lives in classify_prepared_row so the dispatcher and a direct
                # dry-run can never disagree.
                outcome, diag = self.classify_prepared_row(row)
                entry: dict[str, Any] = {
                    "op_id": op_id, "operation_kind": operation_kind, "outcome": outcome
                }
                if "observed_error" in diag:
                    entry["observed_error"] = diag["observed_error"]
                outcomes.append(entry)
                continue
            live = self.replay_prepared_row(row)[0]
            if live == REPLAYED:
                replayed += 1
            elif live == DRIFTED:
                drifted += 1
            elif live == FAILED:
                failed += 1
        result: dict[str, Any] = {"replayed": replayed, "drifted": drifted, "failed": failed}
        if dry_run:
            # replayed/drifted/failed stay 0 in dry-run: they count actions PERFORMED, and a
            # dry-run performs none. The forecast lives in counts/outcomes instead.
            result["dry_run"] = True
            result["scanned"] = scanned
            result["counts"] = summarize_outcomes(outcomes)
            result["outcomes"] = outcomes
        return result
