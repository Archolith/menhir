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
  anyone's" identically, as a silent ``continue``. That matters concretely today:
  ``LEGACY_ENTITY_UNMERGE`` rows are written by the legacy coordinator and no reconciler claims them,
  so a crash leaving one PREPARED is currently invisible to every reconciler in the system.

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


def build_handlers(
    *,
    merge: Any = None,
    unmerge: Any = None,
    metric_write: Any = None,
    delete: Any = None,
) -> dict[str, Any]:
    """Map operation_kind -> the coordinator that owns it.

    Defined here, as data, so the routing table has exactly one home. Kinds deliberately left
    unmapped are reported as UNKNOWN_KIND rather than skipped:

    * ``LEGACY_ENTITY_UNMERGE`` is written by the legacy coordinator but has no replay path.
    * ``METRIC_MIGRATE`` / ``METRIC_REVERSE`` are declared in ``OPERATION_KINDS`` but no code in the
      tree writes or reconciles them, so a row of either kind would mean something unexpected has
      happened and must not be waved through.

    Passing ``None`` for a coordinator leaves its kinds unmapped, which is how a caller can observe
    a subset without pretending the rest are handled.
    """
    handlers: dict[str, Any] = {}
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
]
