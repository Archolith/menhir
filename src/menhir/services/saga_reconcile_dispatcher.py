"""Central PREPARED-backlog dispatcher for saga recovery (CF-20b, observe mode).

Replaces four independent scans with one. Each coordinator's ``reconcile`` currently walks the whole
journal and filters by ``operation_kind``, so with four coordinators the backlog is read four times
and every coordinator sees -- and silently skips -- the other three's rows. Worse, each scan is
capped at the oldest 500 rows, so a row that never leaves PREPARED pins that page and hides every
newer row behind it.

This dispatcher scans once, exhaustively, and routes each row to exactly one handler. Two things it
adds that a per-coordinator scan structurally cannot:

* **an ownership veto applied before any saga logic.** Whether the original writer is still alive is
  a property of the row, identical for every saga type, and it must be checked before a coordinator
  reads graph state -- a row being actively mutated by another process is not a row whose graph state
  means anything yet.
* **unknown kinds as a first-class outcome.** A per-coordinator scan expresses "not mine" and "not
  anyone's" identically, as a silent ``continue``. That is what made ``LEGACY_ENTITY_UNMERGE`` rows
  invisible to every reconciler in the system (CF-209): they are written by the legacy coordinator
  and no coordinator claims them, so a crash leaving one PREPARED went unreported. Those rows now
  carry an explicit non-replayable disposition and quarantine; the outcome is reserved for kinds
  nobody can account for at all.

Observe mode only. Nothing here mutates, and live activation is deliberately refused: it requires
CF-20c's global PREPARE gate and reconciliation lease, without which a startup reconciler can race a
writer that is still running.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_reconcile_dispatcher_routing import (
    LEGACY_UNMERGE_DISPOSITION,
    NonReplayableKind,
    build_handlers,
)
from menhir.services.saga_reconcile_dispatcher_run import ReconcileRun
from menhir.services.saga_reconcile_gate import (
    GateHeartbeat,
    ReconciliationGate,
    ReconciliationLeaseLost,
)
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    FAILED,
    LIVE_OWNER,
    OWNER_UNKNOWN,
    UNKNOWN_KIND,
    WOULD_NEEDS_REVIEW,
    summarize_live_outcomes,
    summarize_outcomes,
)

logger = logging.getLogger(__name__)

#: How many example op_ids to keep per outcome. A preflight needs a handle to investigate with,
#: not the whole backlog -- a quarantine storm would otherwise make the summary itself unreadable.
_EXAMPLES_PER_OUTCOME = 5

#: Default ceiling on quarantine-bound rows before the run is called systemic rather than row-local.
DEFAULT_MAX_NEEDS_REVIEW = 25


@dataclass
class SagaReconcileDispatcher:
    """Scans the PREPARED backlog once and classifies every row. Mutates nothing."""

    journal: GraphOperationsJournal
    handlers: Mapping[str, Any]
    max_needs_review: int = DEFAULT_MAX_NEEDS_REVIEW
    batch_size: int = 500

    def observe(self, *, now: datetime | None = None) -> ReconcileRun:
        """Classify the complete PREPARED backlog without mutating anything.

        Exhaustive by construction: it consumes ``journal.iter_by_state``, whose cursor steps past a
        row it cannot resolve, so no single bad row can stall the pass.
        """
        run = ReconcileRun(run_id=uuidlib.uuid4().hex)
        moment = now or datetime.now(timezone.utc)
        oldest: str | None = None

        for row in self.journal.iter_by_state("PREPARED", batch_size=self.batch_size):
            run.scanned += 1
            op_id = str(row.get("op_id"))
            kind = row.get("operation_kind")

            created_at = row.get("created_at")
            if isinstance(created_at, str) and created_at and (oldest is None or created_at < oldest):
                oldest = created_at

            outcome, diagnostics = self._classify(row, kind)

            entry: dict[str, Any] = {
                "op_id": op_id,
                "operation_kind": kind,
                "outcome": outcome,
            }
            if "observed_error" in diagnostics:
                entry["observed_error"] = diagnostics["observed_error"]
            if "survivors" in diagnostics:
                entry["survivors"] = diagnostics["survivors"]
            if diagnostics.get("own_claim"):
                entry["own_claim"] = True
            run.rows.append(entry)

            run.counts_by_kind[str(kind)] = run.counts_by_kind.get(str(kind), 0) + 1
            examples = run.examples.setdefault(outcome, [])
            if len(examples) < _EXAMPLES_PER_OUTCOME:
                examples.append(op_id)

        run.counts = summarize_outcomes(run.rows)
        run.oldest_prepared_at = oldest
        run.oldest_prepared_age_seconds = self._age_seconds(oldest, moment)
        self._assess_readiness(run)

        logger.info(
            "saga reconcile run %s (observe): scanned=%d counts=%s write_ready=%s reasons=%s",
            run.run_id, run.scanned, run.counts, run.write_ready, run.blocking_reasons,
        )
        return run

    def run(
        self,
        *,
        dry_run: bool = True,
        now: datetime | None = None,
        gate: ReconciliationGate | None = None,
    ) -> ReconcileRun:
        """Observe-only entry point. ``dry_run=False`` is refused, not silently downgraded.

        Live replay needs the global PREPARE gate and the reconciliation lease from CF-20c. Without
        them a starting reconciler can replay an operation whose original writer is still executing
        it, which is the one failure this whole design exists to prevent. Raising is the safe
        response: quietly observing instead would let a caller believe recovery had run.
        """
        if not dry_run:
            if gate is None:
                raise NotImplementedError(
                    "live saga reconciliation requires the CF-20c reconciliation gate: pass a HELD "
                    "ReconciliationGate. Without it the global PREPARE pause is not in force, so a "
                    "writer could insert a PREPARED row after recovery started reading the "
                    "backlog. Use observe() / run(dry_run=True) to classify instead."
                )
            return self.replay(gate=gate, now=now)
        return self.observe(now=now)

    def replay(
        self,
        *,
        gate: ReconciliationGate,
        now: datetime | None = None,
    ) -> ReconcileRun:
        """Drain the abandoned PREPARED backlog for real (CF-20c). Requires a HELD gate.

        The order of operations per row is the whole safety argument, and none of it is
        rearrangeable:

        1. **Verify the gate before every side effect.** Not once at the top: a pass over a large
           backlog can outlive a lease, and the interesting failure is losing the gate midway while
           still believing we hold it. Losing it aborts, because another reconciler may already
           have taken it and begun replaying these same rows.
        2. **Classify ownership before touching the graph.** A row a live writer still owns has
           mid-flight graph state, so reading it proves nothing and acting on it double-applies.
        3. **Claim the row atomically before any graph access.** The claim converts "this looks
           abandoned" into "this is mine". Replaying first and claiming afterwards would let two
           reconcilers both mutate before either journal transition resolved the conflict.
        4. **Only then replay.**

        A lost claim is a normal outcome, not an error: another reconciler legitimately took the
        row between our classification and our claim, and it will handle it.

        Row-local failures continue the pass; systemic ones stop it. That distinction is the
        circuit breaker -- one irreconcilable operation must not block recovery of the rest, while
        lease loss or a quarantine storm means the deployment's assumptions are wrong and startup
        must not proceed.
        """
        run = ReconcileRun(run_id=uuidlib.uuid4().hex, dry_run=False)
        moment = now or datetime.now(timezone.utc)
        oldest: str | None = None
        quarantined = 0

        # Renewal runs independently of the row loop. Renewing between rows would inherit the very
        # assumption this design discarded -- that a single mutation completes within some known
        # wall-clock bound -- and a one-row backlog would never reach a second check at all.
        heartbeat = GateHeartbeat(gate).start()
        try:
            self._replay_rows(run, gate, heartbeat)
        finally:
            heartbeat.stop()

        run.counts = summarize_live_outcomes(run.rows)
        run.oldest_prepared_at = run.oldest_prepared_at
        run.oldest_prepared_age_seconds = self._age_seconds(run.oldest_prepared_at, moment)

        # The LAST thing before a readiness verdict. Every per-row check happens before its own
        # side effect, so without this the final row's mutation is never covered and a run could
        # report write_ready after the PREPARE pause had already lapsed.
        if not run.aborted:
            try:
                gate.verify_still_held()
            except ReconciliationLeaseLost as exc:
                run.aborted = True
                run.abort_reason = f"reconciliation gate lost before the readiness verdict: {exc}"
                logger.error("saga reconcile run %s ABORTED at final check: %s", run.run_id, exc)

        self._assess_live_readiness(run)

        logger.info(
            "saga reconcile run %s (live): scanned=%d counts=%s aborted=%s write_ready=%s "
            "reasons=%s",
            run.run_id, run.scanned, run.counts, run.aborted, run.write_ready,
            run.blocking_reasons,
        )
        return run

    def _replay_rows(
        self, run: ReconcileRun, gate: ReconciliationGate, heartbeat: GateHeartbeat
    ) -> None:
        """The row loop. Stops at the first systemic condition; row-local outcomes continue."""
        oldest: str | None = None
        quarantined = 0

        for row in self.journal.iter_by_state("PREPARED", batch_size=self.batch_size):
            run.scanned += 1
            op_id = str(row.get("op_id"))
            kind = str(row.get("operation_kind"))

            created_at = row.get("created_at")
            if isinstance(created_at, str) and created_at and (
                oldest is None or created_at < oldest
            ):
                oldest = created_at

            # The heartbeat thread is the authority on whether the gate is still ours; this
            # durable re-read catches the case it cannot see -- an operator forcing a takeover
            # between renewals. Both must hold before any side effect.
            if heartbeat.lost:
                run.aborted = True
                run.abort_reason = "reconciliation gate renewal failed; the gate is no longer held"
                logger.error(
                    "saga reconcile run %s ABORTED before op %s: gate renewal failed",
                    run.run_id, op_id,
                )
                break
            try:
                gate.verify_still_held()
            except ReconciliationLeaseLost as exc:
                run.aborted = True
                run.abort_reason = f"reconciliation gate lost mid-pass: {exc}"
                logger.error(
                    "saga reconcile run %s ABORTED before op %s: %s", run.run_id, op_id, exc
                )
                break

            outcome, diagnostics = self._replay_one(row, op_id=op_id, kind=kind)
            self._record(run, op_id=op_id, kind=kind, outcome=outcome, diagnostics=diagnostics)

            if outcome == DRIFTED:
                quarantined += 1
                if quarantined > self.max_needs_review:
                    run.aborted = True
                    run.abort_reason = (
                        f"{quarantined} rows quarantined, above the {self.max_needs_review} "
                        "ceiling; treating as systemic rather than row-local"
                    )
                    logger.error(
                        "saga reconcile run %s ABORTED: %s", run.run_id, run.abort_reason
                    )
                    break

        run.oldest_prepared_at = oldest

    def _replay_one(
        self, row: Mapping[str, Any], *, op_id: str, kind: str
    ) -> tuple[str, dict[str, Any]]:
        """Ownership veto, atomic claim, then the handler's live action, for exactly one row."""
        ownership = oo.classify_ownership(row)
        if ownership == LIVE_OWNER:
            return LIVE_OWNER, {"own_claim": oo.is_own_claim(row)}
        if ownership == OWNER_UNKNOWN:
            return OWNER_UNKNOWN, {}

        handler = self.handlers.get(kind)
        if handler is None:
            return UNKNOWN_KIND, {
                "observed_error": f"no reconciler claims operation_kind {kind!r}"
            }

        # Claim BEFORE any graph access. The classification above is advisory; the claim re-runs
        # the same rule inside its own transaction and is the only authoritative answer.
        if not self.journal.claim_abandoned_operation(op_id):
            return LIVE_OWNER, {
                "observed_error": (
                    "claim lost between classification and claim; another reconciler owns this row"
                )
            }

        # A recorded non-replayable kind quarantines here rather than inside the handler: the
        # disposition is pure data with no journal of its own, and the dispatcher owns the journal.
        # Keeping the write here means exactly one place performs it.
        if isinstance(handler, NonReplayableKind):
            self.journal.mark_needs_review(op_id, observed_error=handler.reason)
            return DRIFTED, {"observed_error": handler.reason, "non_replayable": True}

        try:
            return handler.replay_prepared_row(row)
        except Exception as exc:  # noqa: BLE001 -- one defective handler must not abort the pass
            logger.warning(
                "saga reconcile: handler for %s raised on op %s: %s", kind, op_id, exc
            )
            return FAILED, {
                "observed_error": f"handler for {kind!r} raised: {type(exc).__name__}: {exc}"
            }

    @staticmethod
    def _record(
        run: ReconcileRun,
        *,
        op_id: str,
        kind: str,
        outcome: str,
        diagnostics: Mapping[str, Any],
    ) -> None:
        entry: dict[str, Any] = {"op_id": op_id, "operation_kind": kind, "outcome": outcome}
        if "observed_error" in diagnostics:
            entry["observed_error"] = diagnostics["observed_error"]
        if diagnostics.get("own_claim"):
            entry["own_claim"] = True
        run.rows.append(entry)
        run.counts_by_kind[kind] = run.counts_by_kind.get(kind, 0) + 1
        examples = run.examples.setdefault(outcome, [])
        if len(examples) < _EXAMPLES_PER_OUTCOME:
            examples.append(op_id)

    def _assess_live_readiness(self, run: ReconcileRun) -> None:
        """Whether saga writers may be admitted after this pass.

        Stricter than the observe-mode verdict, deliberately: after a live pass a residual blocker
        means recovery could NOT resolve the backlog, which is a stronger statement than a forecast
        that it might not. DRIFTED does not block on its own -- a quarantined row is recovery
        working correctly, and the storm ceiling above is what turns volume into a systemic verdict.
        """
        reasons: list[str] = []
        if run.aborted:
            reasons.append(run.abort_reason or "recovery aborted")
        for name, why in (
            (UNKNOWN_KIND, "PREPARED row(s) of a kind no reconciler claims"),
            (OWNER_UNKNOWN, "PREPARED row(s) with unprovable ownership"),
            (FAILED, "PREPARED row(s) whose replay failed and remain unresolved"),
        ):
            count = run.counts.get(name, 0)
            if count:
                reasons.append(f"{count} {why}")

        # A row THIS process still claims is not the healthy transient that a LIVE_OWNER row
        # normally is. Recovery runs before any local writer is admitted, so this process has no
        # legitimate way to own a PREPARED row: such a row can only be residue from an earlier
        # initialisation attempt in the same PID whose replay left it PREPARED. Reading it as a
        # live peer is what let a first startup fail on a FAILED replay and a second startup
        # succeed on the same unresolved row, its claim now fresh and its outcome LIVE_OWNER.
        own_claims = sum(
            1
            for entry in run.rows
            if entry.get("outcome") == LIVE_OWNER and entry.get("own_claim")
        )
        if own_claims:
            reasons.append(
                f"{own_claims} PREPARED row(s) still claimed by this process from an earlier "
                "startup attempt"
            )

        run.blocking_reasons = reasons
        run.write_ready = not reasons

    # ------------------------------------------------------------------ internals

    def _classify(self, row: Mapping[str, Any], kind: object) -> tuple[str, dict[str, Any]]:
        """Ownership veto first, then route to the owning coordinator.

        Ownership is checked before saga logic on purpose. If another process is still executing
        this operation, its graph state is mid-flight and a coordinator's precondition comparison
        against it would be meaningless at best and misleading at worst.
        """
        ownership = oo.classify_ownership(row)
        if ownership == LIVE_OWNER:
            return LIVE_OWNER, {"own_claim": oo.is_own_claim(row)}
        if ownership == OWNER_UNKNOWN:
            return OWNER_UNKNOWN, {}

        handler = self.handlers.get(str(kind))
        if handler is None:
            return UNKNOWN_KIND, {
                "observed_error": f"no reconciler claims operation_kind {kind!r}"
            }

        try:
            return handler.classify_prepared_row(row)
        except Exception as exc:  # noqa: BLE001
            # A handler is contractually required not to raise, but a dispatcher that trusts that
            # would let one defective handler abort the whole pass and hide the rest of the backlog.
            return WOULD_NEEDS_REVIEW, {
                "observed_error": (
                    f"handler for {kind!r} raised: {type(exc).__name__}: {exc}"
                )
            }

    @staticmethod
    def _age_seconds(created_at: str | None, moment: datetime) -> float | None:
        if not created_at:
            return None
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - parsed).total_seconds())

    def _assess_readiness(self, run: ReconcileRun) -> None:
        """Advisory verdict for this run. Nothing enforces it until CF-20c gates writers on it.

        Distinguishes row-local from systemic, per the plan's quarantine-storm hazard. A handful of
        genuinely irreconcilable rows is row-local and does not block; an unclassifiable row, an
        ambiguous owner, or a quarantine explosion is systemic.

        LIVE_OWNER deliberately does NOT block. A live writer is normal and transient -- the correct
        response is to let it finish, not to refuse startup.
        """
        reasons: list[str] = []

        unknown = run.counts.get(UNKNOWN_KIND, 0)
        if unknown:
            reasons.append(
                f"{unknown} PREPARED row(s) of a kind no reconciler claims; recovery cannot reason "
                "about them"
            )

        owner_unknown = run.counts.get(OWNER_UNKNOWN, 0)
        if owner_unknown:
            reasons.append(
                f"{owner_unknown} PREPARED row(s) with unprovable ownership; during a mixed-version "
                "rollout an older writer may still own them"
            )

        needs_review = run.counts.get(WOULD_NEEDS_REVIEW, 0)
        if needs_review > self.max_needs_review:
            reasons.append(
                f"{needs_review} row(s) would be quarantined, above the {self.max_needs_review} "
                "ceiling; treat as systemic rather than row-local"
            )

        run.blocking_reasons = reasons
        run.write_ready = not reasons


__all__ = [
    "DEFAULT_MAX_NEEDS_REVIEW",
    "ReconcileRun",
    "SagaReconcileDispatcher",
    "build_handlers",
    "NonReplayableKind",
    "LEGACY_UNMERGE_DISPOSITION",
]
