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
    from menhir.services.erasure_coordinator import ErasureCoordinator
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
        # Registered unconditionally with the rest: an EXPLICIT_ERASURE row left by a crash
        # would otherwise report UNKNOWN_KIND and block write-readiness, which on a
        # deployment running live recovery means refusing to boot.
        erasure=ErasureCoordinator(graph_adapter=adapter, journal=journal),
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
    writers_gate_aware: bool = False
    client_read_timeout_s: float | None = None
    #: Whether the read deadline was actually PROBED. 'Not measured' is not evidence of
    #: absence, and warning on it would fire on every caller that does not probe.
    client_read_timeout_measured: bool = False

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
            "writers_gate_aware": self.writers_gate_aware,
            "client_read_timeout_s": self.client_read_timeout_s,
            "client_read_timeout_measured": self.client_read_timeout_measured,
        }

    def render(self) -> str:
        """A human-readable summary. This is what an operator actually reads before deciding."""
        lines = [
            f"saga recovery preflight (run {self.run_id})",
            f"  host                   : {self.hostname}",
            f"  PREPARED backlog       : {self.scanned}",
            f"  oldest PREPARED age    : {self.oldest_prepared_age_seconds}",
            f"  PID namespace asserted : {self.pid_namespace_asserted}",
            f"  writers gate-aware     : {self.writers_gate_aware}",
            f"  client read timeout    : {self.client_read_timeout_s}",
            f"  by kind                : {self.counts_by_kind or '{}'}",
            f"  by outcome             : {self.counts or '{}'}",
        ]
        for blocker in self.blockers:
            lines.append(f"  BLOCKER  {blocker}")
        for warning in self.warnings:
            lines.append(f"  warning  {warning}")
        lines.append(f"  VERDICT                : {'CLEAN' if self.clean else 'NOT CLEAN'}")
        return "\n".join(lines)


def preflight_from_run(
    run: Any,
    *,
    client_read_timeout_s: float | None = None,
    client_read_timeout_measured: bool = False,
) -> PreflightReport:
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
        writers_gate_aware=oo.all_saga_writers_are_gate_aware(),
        client_read_timeout_s=client_read_timeout_s,
        client_read_timeout_measured=client_read_timeout_measured,
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

    # A BLOCKER, unlike the PID-namespace warning above. That one narrows recovery; this one admits
    # a writer racing it. Without the assertion there is no basis for believing the global PREPARE
    # pause actually pauses every writer, and recovery's write-ready verdict is unsupported.
    if not report.writers_gate_aware:
        report.blockers.append(
            f"{oo.SAGA_WRITERS_GATE_AWARE_ENV} is not set. The PREPARE pause is enforced inside "
            "prepare(), so it binds only writers running a gate-aware build: an older binary can "
            "still insert a PREPARED row while recovery holds the gate, begin mutating, and be "
            "missed by the pass that then reports write-ready. Set it only once no gate-unaware "
            "writer can be running -- every saga writer stopped or upgraded. Current-version peers "
            "do not need to be stopped."
        )

    # CF-211. A warning, not a blocker: recovery is correct either way, because ownership is
    # decided by positive death evidence rather than by elapsed time. What an unbounded client
    # read costs is AVAILABILITY -- a caller hangs with its operation PREPARED and its
    # participants fenced. Worth surfacing because the bound is supplied by the SERVER, so it can
    # disappear by changing database, not by changing this code.
    if report.client_read_timeout_measured and not report.client_read_timeout_s:
        report.warnings.append(
            "the Neo4j connection has NO client-side read deadline, so a stalled or black-holed "
            "read can hang a saga writer indefinitely with its operation PREPARED and its "
            "participants fenced. The driver takes this bound only from the server's "
            "connection.recv_timeout_seconds hint; this server is not sending one."
        )

    return report


def _probe_client_read_timeout(dispatcher: Any) -> float | None:
    """Measure the connection's read deadline via whatever repository the handlers hold."""
    for handler in getattr(dispatcher, "handlers", {}).values():
        adapter = getattr(handler, "graph_adapter", None)
        repo = getattr(adapter, "neo4j", None)
        probe = getattr(repo, "client_read_timeout_seconds", None)
        if callable(probe):
            return probe()
    return None


def run_preflight(dispatcher: Any) -> PreflightReport:
    """Observe the backlog and judge this deployment. Read-only.

    Takes a dispatcher rather than building one so that startup and an operator command exercise
    the SAME handler wiring. A preflight that inspected a differently-wired dispatcher would be
    answering a question about a system that is not the one about to run.
    """
    run = dispatcher.observe()
    report = preflight_from_run(
        run,
        client_read_timeout_s=_probe_client_read_timeout(dispatcher),
        client_read_timeout_measured=True,
    )
    logger.info("Saga recovery preflight:\n%s", report.render())
    return report


__all__ = [
    "PreflightReport",
    "build_default_dispatcher",
    "preflight_from_run",
    "run_preflight",
]
