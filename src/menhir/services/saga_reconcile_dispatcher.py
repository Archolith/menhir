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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_reconcile_outcomes import (
    LIVE_OWNER,
    OWNER_UNKNOWN,
    UNKNOWN_KIND,
    WOULD_NEEDS_REVIEW,
    summarize_outcomes,
)

logger = logging.getLogger(__name__)

#: How many example op_ids to keep per outcome. A preflight needs a handle to investigate with,
#: not the whole backlog -- a quarantine storm would otherwise make the summary itself unreadable.
_EXAMPLES_PER_OUTCOME = 5

#: Default ceiling on quarantine-bound rows before the run is called systemic rather than row-local.
DEFAULT_MAX_NEEDS_REVIEW = 25


@dataclass(frozen=True)
class NonReplayableKind:
    """A recorded decision that a saga kind must never be recovered by replaying it (CF-209).

    This is a THIRD disposition, and the distinction it draws is the point. Before it, the routing
    table could only say "a coordinator owns this kind" or nothing at all, and nothing at all was
    reported as UNKNOWN_KIND -- which reads as "something unexpected is in the journal". For
    ``LEGACY_ENTITY_UNMERGE`` that was the wrong message twice over: its presence is entirely
    expected, and UNKNOWN_KIND blocks write-readiness for the WHOLE run, so one such row would
    stall recovery of every other row in the backlog.

    Quarantine, never replay. The decision rests on the legacy lane's own contract:

    * The forward operation requires ``acknowledge_degraded=True`` -- an explicit human
      acknowledgement, given for one specific invocation. Replaying it unattended means a machine
      re-asserting that acknowledgement in a situation the human never saw.
    * The restore is NEVER exact (``exact: False``, unconditionally). ``legacy_unmerge_coordinator``
      exists to avoid "handing back a partially-restored graph and letting an operator believe it is
      repaired" -- and an unwatched replay produces exactly that, with a degradation list nobody
      read.
    * The restore is multi-part (labels, properties, in/out relationships, episode rebinding) and
      the legacy snapshot is lossy, so a crash can leave it partially applied with no reliable way
      to tell how far it got.

    So the row is routed to WOULD_NEEDS_REVIEW: visible, counted, and adjudicable by an operator
    through the existing NEEDS_REVIEW clearance, which releases the participant fence when they
    resolve it. The two entity UUIDs stay fenced until then, deliberately -- the graph really is in
    an unknown partial state, and letting other operations touch those nodes meanwhile is worse
    than making a human look.

    Kinds that are genuinely unexpected (``METRIC_MIGRATE``, ``METRIC_REVERSE``: declared in
    ``OPERATION_KINDS`` but written by no code in the tree) stay unmapped on purpose. "We decided
    this cannot be replayed" and "we do not know what this is" are different facts and must not
    collapse into one outcome.
    """

    kind: str
    reason: str

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Always quarantine. No graph read, because no graph state could change the answer."""
        return WOULD_NEEDS_REVIEW, {"observed_error": self.reason}


#: The recorded disposition for legacy unmerge rows. Held as a module constant so the routing table
#: keeps one home and the decision is greppable from the kind name.
LEGACY_UNMERGE_DISPOSITION = NonReplayableKind(
    kind="LEGACY_ENTITY_UNMERGE",
    reason=(
        "LEGACY_ENTITY_UNMERGE is structurally non-replayable: the restore is degraded, never "
        "exact, and gated on a per-invocation operator acknowledgement that recovery cannot give "
        "on an operator's behalf. Quarantined for adjudication (CF-209)."
    ),
)


def build_handlers(
    *,
    merge: Any = None,
    unmerge: Any = None,
    metric_write: Any = None,
    delete: Any = None,
) -> dict[str, Any]:
    """Map operation_kind -> the coordinator that owns it.

    Defined here, as data, so the routing table has exactly one home. Three dispositions exist:

    * **a coordinator owns the kind** -- it is replayed.
    * **a recorded non-replayable decision** -- ``LEGACY_ENTITY_UNMERGE`` is registered
      unconditionally to :data:`LEGACY_UNMERGE_DISPOSITION` and always quarantines. It takes no
      coordinator argument because the disposition is a property of the KIND, not of whether some
      service happens to be wired up.
    * **deliberately unmapped** -- ``METRIC_MIGRATE`` / ``METRIC_REVERSE`` are declared in
      ``OPERATION_KINDS`` but no code in the tree writes or reconciles them, so a row of either
      kind means something unexpected has happened. They report UNKNOWN_KIND and block readiness,
      which is the correct response to a journal row nobody can account for.

    Passing ``None`` for a coordinator leaves its kinds unmapped, which is how a caller can observe
    a subset without pretending the rest are handled.
    """
    handlers: dict[str, Any] = {"LEGACY_ENTITY_UNMERGE": LEGACY_UNMERGE_DISPOSITION}
    if merge is not None:
        handlers["ENTITY_MERGE"] = merge
    if unmerge is not None:
        handlers["ENTITY_UNMERGE"] = unmerge
    if metric_write is not None:
        handlers["METRIC_WRITE"] = metric_write
    if delete is not None:
        handlers["ENTITY_DELETE"] = delete
        handlers["SESSION_TTL_DELETE"] = delete
    return handlers


@dataclass
class ReconcileRun:
    """One reconciliation pass, summarised as a single reportable unit.

    The run_id is what makes a large quarantine event legible as ONE startup incident instead of
    hundreds of unrelated row updates.
    """

    run_id: str
    scanned: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    counts_by_kind: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)
    oldest_prepared_at: str | None = None
    oldest_prepared_age_seconds: float | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    write_ready: bool = True
    blocking_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dry_run": True,
            "scanned": self.scanned,
            "counts": self.counts,
            "counts_by_kind": self.counts_by_kind,
            "examples": self.examples,
            "oldest_prepared_at": self.oldest_prepared_at,
            "oldest_prepared_age_seconds": self.oldest_prepared_age_seconds,
            "write_ready": self.write_ready,
            "blocking_reasons": self.blocking_reasons,
            "outcomes": self.rows,
        }


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

    def run(self, *, dry_run: bool = True, now: datetime | None = None) -> ReconcileRun:
        """Observe-only entry point. ``dry_run=False`` is refused, not silently downgraded.

        Live replay needs the global PREPARE gate and the reconciliation lease from CF-20c. Without
        them a starting reconciler can replay an operation whose original writer is still executing
        it, which is the one failure this whole design exists to prevent. Raising is the safe
        response: quietly observing instead would let a caller believe recovery had run.
        """
        if not dry_run:
            raise NotImplementedError(
                "live saga reconciliation is not enabled: it requires the CF-20c global PREPARE "
                "gate and reconciliation lease. Use observe() / run(dry_run=True)."
            )
        return self.observe(now=now)

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
