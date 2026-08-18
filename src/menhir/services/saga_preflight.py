"""Per-deployment preflight for live saga recovery (CF-20c).

Activating live replay is a decision about a DEPLOYMENT, not about the code. The same binary is
safe to activate on one host and unsafe on another, because the questions that matter are all
environmental: what is actually sitting in this journal, and can this process trust the evidence it
would use to declare a writer dead.

So this module answers those questions and refuses to answer any others. It performs no mutation of
any kind -- it is the read-only step an operator runs BEFORE flipping the switch, and it is run
again automatically at startup before recovery is allowed to act.

Blockers versus warnings is the load-bearing distinction:

* a **blocker** means live recovery would not be able to resolve the backlog, so activating it
  would leave the deployment in exactly the fenced state it was supposed to clear;
* a **warning** means recovery will work but with a capability switched off, which is a legitimate
  configuration and not a reason to refuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness

logger = logging.getLogger(__name__)


def build_default_dispatcher(adapter: Any) -> Any:
    """Wire the four coordinators over the shared sidecar and return the central dispatcher.

    Lives here, in the services layer, so startup and the operator CLI get IDENTICAL wiring from
    one place. A preflight that inspected a differently-wired dispatcher would be answering a
    question about a system that is not the one about to run -- and a second copy of this function
    is exactly how the two drift apart.

    Imports are deferred to call time because the coordinators pull in Neo4j and SQLite machinery
    that a bare ``import menhir.services.saga_preflight`` has no reason to load.
    """
    from menhir.infrastructure.graph_operations import GraphOperationsJournal
    from menhir.infrastructure.metric_receipts import MetricReceiptStore
    from menhir.services.delete_coordinator import DeleteCoordinator
    from menhir.services.merge_coordinator import MergeCoordinator
    from menhir.services.metric_write_coordinator import MetricWriteCoordinator
    from menhir.services.saga_reconcile_dispatcher import (
        SagaReconcileDispatcher,
        build_handlers,
    )
    from menhir.services.unmerge_coordinator import UnmergeCoordinator

    journal = GraphOperationsJournal()
    handlers = build_handlers(
        merge=MergeCoordinator(graph_adapter=adapter, journal=journal),
        unmerge=UnmergeCoordinator(graph_adapter=adapter, journal=journal),
        metric_write=MetricWriteCoordinator(
            graph_adapter=adapter, journal=journal, receipts=MetricReceiptStore()
        ),
        delete=DeleteCoordinator(graph_adapter=adapter, journal=journal),
    )
    return SagaReconcileDispatcher(journal=journal, handlers=handlers)


@dataclass
class PreflightReport:
    """What this deployment looks like, and whether live recovery may be switched on here."""

    run_id: str
    scanned: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    counts_by_kind: dict[str, int] = field(default_factory=dict)
    oldest_prepared_age_seconds: float | None = None
    examples: dict[str, list[str]] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hostname: str = ""
    pid_namespace_asserted: bool = False

    @property
    def clean(self) -> bool:
        """Whether live recovery may run. Warnings never make a deployment unclean."""
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "clean": self.clean,
            "scanned": self.scanned,
            "counts": self.counts,
            "counts_by_kind": self.counts_by_kind,
            "oldest_prepared_age_seconds": self.oldest_prepared_age_seconds,
            "examples": self.examples,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "hostname": self.hostname,
            "pid_namespace_asserted": self.pid_namespace_asserted,
        }

    def render(self) -> str:
        """A human-readable summary. This is what an operator actually reads before deciding."""
        lines = [
            f"saga recovery preflight (run {self.run_id})",
            f"  host                   : {self.hostname}",
            f"  PREPARED backlog       : {self.scanned}",
            f"  oldest PREPARED age    : {self.oldest_prepared_age_seconds}",
            f"  PID namespace asserted : {self.pid_namespace_asserted}",
            f"  by kind                : {self.counts_by_kind or '{}'}",
            f"  by outcome             : {self.counts or '{}'}",
        ]
        for blocker in self.blockers:
            lines.append(f"  BLOCKER  {blocker}")
        for warning in self.warnings:
            lines.append(f"  warning  {warning}")
        lines.append(f"  VERDICT                : {'CLEAN' if self.clean else 'NOT CLEAN'}")
        return "\n".join(lines)


def preflight_from_run(run: Any) -> PreflightReport:
    """Build a report from a completed observe() pass. Pure; performs no I/O of its own.

    Split out so the environmental checks can be tested without a journal, and so the caller
    controls how the observation is produced -- startup already has a dispatcher wired up and
    should not build a second one just to ask the same question.
    """
    report = PreflightReport(
        run_id=str(getattr(run, "run_id", "?")),
        scanned=int(getattr(run, "scanned", 0) or 0),
        counts=dict(getattr(run, "counts", {}) or {}),
        counts_by_kind=dict(getattr(run, "counts_by_kind", {}) or {}),
        oldest_prepared_age_seconds=getattr(run, "oldest_prepared_age_seconds", None),
        examples=dict(getattr(run, "examples", {}) or {}),
        hostname=process_liveness.hostname(),
        pid_namespace_asserted=oo.host_pid_namespace_is_verifiable(),
    )

    # The observation's own verdict is the primary blocker: unknown kinds, unprovable ownership,
    # or a quarantine storm all mean recovery could not resolve this backlog.
    if not getattr(run, "write_ready", True):
        for reason in getattr(run, "blocking_reasons", []) or []:
            report.blockers.append(str(reason))

    # Not a blocker. An unasserted PID namespace does not break recovery -- it narrows it, and the
    # narrowing is the SAFE direction. Recovery still runs; it simply cannot declare a writer dead
    # by inspecting a local PID, so an expired local row fences instead of being replayed. Refusing
    # to activate over this would punish the conservative configuration.
    if not report.pid_namespace_asserted:
        report.warnings.append(
            f"{oo.HOST_PID_NAMESPACE_ENV} is not set, so automatic PID-based recovery is off on "
            "this deployment: an expired claim from a dead local writer will fence as "
            "OWNER_UNKNOWN rather than being replayed, and only a named operator attestation can "
            "release it. Set it ONLY if this hostname identifies exactly one inspectable PID "
            "namespace -- not on containers, cloned images, or a journal volume shared by more "
            "than one node."
        )

    return report


def run_preflight(dispatcher: Any) -> PreflightReport:
    """Observe the backlog and judge this deployment. Read-only.

    Takes a dispatcher rather than building one so that startup and an operator command exercise
    the SAME handler wiring. A preflight that inspected a differently-wired dispatcher would be
    answering a question about a system that is not the one about to run.
    """
    run = dispatcher.observe()
    report = preflight_from_run(run)
    logger.info("Saga recovery preflight:\n%s", report.render())
    return report


__all__ = [
    "PreflightReport",
    "build_default_dispatcher",
    "preflight_from_run",
    "run_preflight",
]
