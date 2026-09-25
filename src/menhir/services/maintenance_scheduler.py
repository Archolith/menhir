"""Periodic maintenance scheduler for deferred enrichment operations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from menhir.infrastructure.telemetry import record_failure_event, record_mcp_event
from menhir.services.maintenance_scheduler_jobs import _MaintenanceJobWiringMixin
from menhir.services.maintenance_scheduler_lifecycle import _MaintenanceLifecycleMixin
from menhir.services.scheduler_lease import SchedulerLeaseStore, _utc_now_iso
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
    SchedulerLeaseStoreProtocol,
    SchedulerLifecycleService,
)
from menhir.services.scheduler_tasks import consolidate_personal_memory

logger = logging.getLogger(__name__)


class _LeaseLostDuringJob(Exception):
    """Raised when a job is abandoned because lease ownership stopped being provable.

    Distinct from a job failure: nothing went wrong with the work, this process simply lost
    the right to be doing it (CF-100).
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            f"maintenance job `{operation}` abandoned: scheduler lease ownership is no longer provable"
        )
        self.operation = operation


@dataclass
class _JobState:
    interval_s: float
    last_started_at: str | None = None
    last_completed_at: str | None = None
    last_duration_ms: int | None = None
    last_success: bool | None = None
    last_result: dict[str, object] | None = None
    runs: int = 0


@dataclass
class MaintenanceScheduler(_MaintenanceJobWiringMixin, _MaintenanceLifecycleMixin):
    """Run periodic maintenance jobs inside the MCP server process."""

    ingest_service: SchedulerIngestService
    graph_adapter: SchedulerGraphAdapter
    lifecycle_service: SchedulerLifecycleService | None = None
    stale_recovery_interval_s: float = 30.0
    queue_health_interval_s: float = 30.0
    failed_retry_interval_s: float = 30.0
    conflict_auto_resolve_interval_s: float = 86400.0
    conflict_auto_resolve_max_age_days: int = 14
    conflict_auto_resolve_limit: int = 50
    conflict_confirm_interval_s: float = 3600.0
    conflict_confirm_limit: int = 20
    conflict_review_unresolved_interval_s: float = 604800.0  # weekly
    conflict_review_unresolved_limit: int = 50
    structure_watcher_interval_s: float = 1800.0
    structure_watcher_enabled: bool = True
    experience_counter_interval_s: float = 3600.0
    experience_counter_enabled: bool = True
    experience_embed: Callable[[str], "list[float] | None"] | None = None
    experience_embed_version: str | None = None   # 4a.1: model id stamped on write-time observation embeddings
    verifier_sync_interval_s: float = 300.0
    verifier_sync_enabled: bool = False
    verifier_repo: object | None = None
    verifier_context: object | None = None
    personal_memory_interval_s: float = 300.0
    personal_memory_enabled: bool = False
    personal_memory_llm: Callable[[str, str], str] | None = None
    personal_memory_k: int = 3
    event_history_enabled: bool = False
    event_history_perceiver_version: str = "v1"
    event_history_batch_size: int = 500
    personal_memory_call_budget: int | None = None
    personal_memory_verify_retries: int = 0
    personal_memory_sum_grounding: bool = False
    # ScalarStateView typed-scalar shadow path (Piece C.4.3), gated inside the same consolidation job.
    # OFF by default -> the counter path is byte-identical; when on, the job also runs typed-scalar
    # extract -> gate -> bind -> persist -> rebuild over its own scalar watermark cursor.
    scalar_state_enabled: bool = False
    scalar_state_perceiver_version: str = "v1"
    # Drop the free-text attribute name from the k-sample vote and reconcile it modally afterwards.
    # OFF by default; RECALL-affecting when on (more claims clear the gate), not behavior-neutral.
    scalar_reconcile_attribute: bool = False
    # The same identity-smearing defect relocated into scope/subject, plus first-person folding.
    # All OFF by default and all RECALL-affecting when on, exactly as the attribute switch above.
    scalar_reconcile_scope: bool = False
    scalar_reconcile_subject: bool = False
    scalar_canonical_self: bool = False
    # Typed-scalar agreement only. The counter path keeps its existing unanimous threshold.
    scalar_threshold: float = 1.0
    # Build advisory scalar_history Views alongside scalar_state at ingest and repair time.
    scalar_history_enabled: bool = False
    # Observe-only deterministic typed-scalar shadow; default off and behavior-neutral.
    scalar_deterministic_shadow_enabled: bool = False
    # Opt-in deterministic scalar router; default off preserves the existing LLM route.
    scalar_deterministic_router_enabled: bool = False
    scalar_deterministic_router_promoted_classes: tuple[str, ...] = ()
    lifecycle_consolidation_interval_s: float = 86400.0  # daily
    lifecycle_consolidation_enabled: bool = True
    lifecycle_decay_interval_s: float = 86400.0  # daily
    lifecycle_decay_enabled: bool = True
    # Retention for the telemetry sidecar's memory_revisions table. The pruner and the
    # MENHIR_REVISION_RETENTION_DAYS setting both existed; nothing connected them, so the
    # documented window was never enforced. 0 disables the job entirely.
    revision_retention_days: int = 14
    # CF-171: sidecar retention, tiered by role. See scheduler_tasks.prune_telemetry_tables.
    telemetry_observability_retention_days: int = 30
    telemetry_diagnostic_retention_days: int = 90
    revision_prune_interval_s: float = 86400.0  # daily
    recovery_limit: int = 100
    failed_retry_limit: int = 50
    tick_interval_s: float = 1.0
    lease_name: str = "maintenance_scheduler"
    lease_duration_s: float = 90.0
    lease_heartbeat_s: float = 30.0
    lease_store: SchedulerLeaseStoreProtocol = field(default_factory=SchedulerLeaseStore)
    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    # Set ONLY by _mark_lease_lost. Deliberately separate from _stop_event: an orderly stop
    # lets the running job finish, a lost lease must interrupt it (CF-100).
    _lease_lost_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _jobs: dict[str, _JobState] = field(default_factory=dict, init=False, repr=False)
    _owner_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid4()}", init=False, repr=False)
    _owner_pid: int = field(default_factory=os.getpid, init=False, repr=False)
    _lease_acquired: bool = field(default=False, init=False, repr=False)
    _lease_lost: bool = field(default=False, init=False, repr=False)
    # Monotonic deadline past which this process can no longer PROVE it holds the lease.
    # 0.0 means "never established". See _lease_is_provable (CF-100).
    _lease_valid_until: float = field(default=0.0, init=False, repr=False)
    _lease_blocked_reason: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_at: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_reason: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_from: dict[str, object] | None = field(default=None, init=False, repr=False)
    # Monotonic deadline before which THIS owner must not run jobs, because a displaced owner's
    # own provable window has not expired yet (CF-241). 0.0 means "no displaced owner to outlive".
    _jobs_blocked_until: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._jobs = {
            "recover_stale_leases": _JobState(interval_s=self.stale_recovery_interval_s),
            "retry_failed_enrichments": _JobState(interval_s=self.failed_retry_interval_s),
            "observe_queue_health": _JobState(interval_s=self.queue_health_interval_s),
            "auto_resolve_conflicts": _JobState(interval_s=self.conflict_auto_resolve_interval_s),
            "confirm_conflicts": _JobState(interval_s=self.conflict_confirm_interval_s),
            "review_unresolved_conflicts": _JobState(interval_s=self.conflict_review_unresolved_interval_s),
        }
        if self.structure_watcher_enabled:
            self._jobs["refresh_structure_graphs"] = _JobState(interval_s=self.structure_watcher_interval_s)
        if self.experience_counter_enabled:
            self._jobs["sync_experience_counters"] = _JobState(interval_s=self.experience_counter_interval_s)
        if self.verifier_sync_enabled and self.verifier_repo is not None:
            self._jobs["sync_verifiers"] = _JobState(interval_s=self.verifier_sync_interval_s)
        if (
            (self.personal_memory_enabled or self.event_history_enabled)
            and self.personal_memory_llm is not None
        ):
            self._jobs["consolidate_personal_memory"] = _JobState(interval_s=self.personal_memory_interval_s)
        if self.lifecycle_service is not None and self.lifecycle_consolidation_enabled:
            self._jobs["consolidate_lifecycle"] = _JobState(interval_s=self.lifecycle_consolidation_interval_s)
        if self.lifecycle_service is not None and self.lifecycle_decay_enabled:
            self._jobs["decay_lifecycle"] = _JobState(interval_s=self.lifecycle_decay_interval_s)
        if self.revision_retention_days > 0:
            self._jobs["prune_telemetry_revisions"] = _JobState(
                interval_s=self.revision_prune_interval_s
            )
        # CF-171: registered when EITHER tier is enabled, since the two windows are independent
        # and an operator may disable one without disabling the other.
        if (
            self.telemetry_observability_retention_days > 0
            or self.telemetry_diagnostic_retention_days > 0
        ):
            self._jobs["prune_telemetry_tables"] = _JobState(
                interval_s=self.revision_prune_interval_s
            )

    def _renew_lease(self) -> bool:
        """Renew the lease and, on success, extend the provable-ownership deadline.

        Every renewal in this class goes through here, so no path can refresh the lease
        without refreshing the deadline that authorizes mutation.
        """
        started = time.monotonic()
        renewed = self.lease_store.renew(
            lease_name=self.lease_name,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            lease_duration_s=self.lease_duration_s,
        )
        if renewed:
            self._stamp_lease_deadline(started)
        return renewed

    async def _run_due_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        for name, job in self._jobs.items():
            if self._stop_event.is_set() or (
                self._lease_supervision_active() and not self._lease_is_provable()
            ):
                # Lease unprovable or shutdown requested mid-batch: stop before the next job so
                # a displaced owner does not keep running maintenance (FC-02 / AR-02 / CF-100).
                # `_lease_is_provable` rather than `_lease_lost`, because the dangerous case is
                # the one where nothing set that flag: the deadline passed unnoticed.
                return
            if self._jobs_are_deferred():
                # We force-took this lease and the owner we displaced could still believe it holds
                # it (CF-241). Its provable window is bounded by the row we replaced; until that
                # passes, running a job here would put two loops on the same graph.
                logger.warning(
                    "Maintenance jobs deferred for %.1fs after a forced takeover; the displaced "
                    "owner's lease has not expired yet",
                    self._jobs_blocked_until - time.monotonic(),
                )
                return
            if job.last_started_at is not None:
                last_started = datetime.fromisoformat(job.last_started_at)
                if (now - last_started).total_seconds() < job.interval_s:
                    continue
            if name == "recover_stale_leases":
                await self._run_job(job, "scheduler_recover_stale_leases", self._make_recover_stale_leases())
            elif name == "retry_failed_enrichments":
                await self._run_job(job, "scheduler_retry_failed_enrichments", self._make_retry_failed_enrichments())
            elif name == "observe_queue_health":
                await self._run_job(job, "scheduler_queue_health", self._make_observe_queue_health())
            elif name == "auto_resolve_conflicts":
                if self.lifecycle_service is not None:
                    await self._run_job(job, "scheduler_auto_resolve_conflicts", self._make_auto_resolve_conflicts())
            elif name == "confirm_conflicts":
                if self.lifecycle_service is not None:
                    await self._run_job(job, "scheduler_confirm_conflicts", self._make_confirm_conflicts())
            elif name == "review_unresolved_conflicts":
                if self.lifecycle_service is not None:
                    await self._run_job(job, "scheduler_review_unresolved_conflicts", self._make_review_unresolved_conflicts())
            elif name == "refresh_structure_graphs":
                await self._run_job(job, "scheduler_refresh_structure_graphs", self._make_refresh_structure_graphs())
            elif name == "sync_experience_counters":
                await self._run_job(job, "scheduler_sync_experience_counters", self._make_sync_experience_counters())
            elif name == "sync_verifiers":
                await self._run_job(job, "scheduler_sync_verifiers", self._make_sync_verifiers())
            elif name == "consolidate_personal_memory":
                await self._run_job(job, "scheduler_consolidate_personal_memory", self._make_consolidate_personal_memory())
            elif name == "consolidate_lifecycle":
                if self.lifecycle_service is not None:
                    await self._run_job(job, "scheduler_consolidate_lifecycle", self._make_consolidate_lifecycle())
            elif name == "decay_lifecycle":
                if self.lifecycle_service is not None:
                    await self._run_job(job, "scheduler_decay_lifecycle", self._make_decay_lifecycle())
            elif name == "prune_telemetry_tables":
                await self._run_job(
                    job, "scheduler_prune_telemetry_tables", self._make_prune_telemetry_tables()
                )
            elif name == "prune_telemetry_revisions":
                await self._run_job(
                    job, "scheduler_prune_telemetry_revisions", self._make_prune_telemetry_revisions()
                )

    # ------------------------------------------------------------------
    # Job wrapper — shared timing, telemetry, and state bookkeeping
    # ------------------------------------------------------------------

    async def _await_job_under_lease(
        self, coro: Awaitable[dict[str, object]], operation: str
    ) -> dict[str, object]:
        """Run one job, abandoning it the moment lease ownership stops being provable.

        Before this, the between-jobs check at the top of `_run_due_jobs` was the only guard,
        so a lease lost during `await coro` stopped the NEXT job and let the current one run to
        completion -- mutating the same graph a new owner had already started maintaining.

        What this does and does not guarantee is worth being exact about. Cancellation is
        cooperative: it is delivered at the job's next await point. A job blocked inside
        `asyncio.to_thread` keeps that one worker-thread call running to completion, because
        nothing can interrupt a thread from outside. So the guarantee is a bound, not an
        eviction -- at most one already-dispatched call finishes, and no further step of the
        job begins. It also depends on the job actually reaching an await point, which is
        exactly what CF-99 restores by moving the scheduler's blocking graph calls off the
        event loop. Under a synchronously blocked loop this guard cannot run at all.
        """
        if not self._lease_supervision_active():
            return await coro
        job_task = asyncio.ensure_future(coro)
        guard = asyncio.ensure_future(self._wait_for_lease_loss())
        try:
            done, _pending = await asyncio.wait(
                {job_task, guard}, return_when=asyncio.FIRST_COMPLETED
            )
            if job_task in done:
                return job_task.result()
            job_task.cancel()
            with contextlib.suppress(BaseException):
                await job_task
            raise _LeaseLostDuringJob(operation)
        except asyncio.CancelledError:
            # This scheduler task is being torn down. Do not leave the job running detached.
            job_task.cancel()
            with contextlib.suppress(BaseException):
                await job_task
            raise
        finally:
            guard.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await guard

    async def _run_job(
        self,
        job: _JobState,
        operation: str,
        coro: Awaitable[dict[str, object]],
    ) -> None:
        job.last_started_at = _utc_now_iso()
        started = asyncio.get_running_loop().time()
        success = True
        result: dict[str, object]
        try:
            result = await self._await_job_under_lease(coro, operation)
            record_mcp_event(
                kind="background",
                operation=operation,
                payload=result,
                result=result,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                success=True,
            )
        except _LeaseLostDuringJob as exc:
            # Not a job failure: the job was abandoned deliberately because a second owner may
            # already be running it. Recorded as retryable so the new owner's own scheduling is
            # the thing that retries, and kept off logger.exception -- there is no bug here to
            # read a traceback for.
            success = False
            result = {"error": str(exc), "abandoned": "lease_lost"}
            logger.warning(
                "Maintenance scheduler abandoned job %s: lease ownership no longer provable "
                "owner_id=%s",
                operation,
                self._owner_id,
            )
            record_failure_event(
                operation=operation,
                failure_stage="scheduler_lease_lost",
                classification="lease_lost",
                retryable=True,
                queue_depth=self.ingest_service.get_queue_depth(),
                error_type=type(exc).__name__,
                error=str(exc),
                traceback_text="",
            )
            record_mcp_event(
                kind="background",
                operation=operation,
                payload=result,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                success=False,
                error=str(exc),
            )
        except Exception as exc:
            success = False
            result = {"error": str(exc)}
            logger.exception("Maintenance scheduler job %s failed", operation)
            record_failure_event(
                operation=operation,
                failure_stage="scheduler_exception",
                classification="scheduler_error",
                retryable=False,
                queue_depth=self.ingest_service.get_queue_depth(),
                error_type=type(exc).__name__,
                error=str(exc),
                traceback_text="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
            record_mcp_event(
                kind="background",
                operation=operation,
                payload=result,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                success=False,
                error=str(exc),
            )
        job.last_completed_at = _utc_now_iso()
        job.last_duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        job.last_success = success
        job.last_result = result
        job.runs += 1

    # ------------------------------------------------------------------
    # Task coroutine factories
    # ------------------------------------------------------------------

    def _make_consolidate_personal_memory(self) -> Awaitable[dict[str, object]]:
        return consolidate_personal_memory(
            self.graph_adapter,
            llm_complete=self.personal_memory_llm,
            embed=self.experience_embed,
            k=self.personal_memory_k,
            call_budget=self.personal_memory_call_budget,
            verify_retries=self.personal_memory_verify_retries,
            sum_grounding=self.personal_memory_sum_grounding,
            enable_scalar_state=self.scalar_state_enabled,
            scalar_state_perceiver_version=self.scalar_state_perceiver_version,
            scalar_reconcile_attribute=self.scalar_reconcile_attribute,
            scalar_reconcile_scope=self.scalar_reconcile_scope,
            scalar_reconcile_subject=self.scalar_reconcile_subject,
            scalar_canonical_self=self.scalar_canonical_self,
            scalar_threshold=self.scalar_threshold,
            scalar_embed_version=self.experience_embed_version,
            scalar_history_enabled=self.scalar_history_enabled,
            scalar_deterministic_shadow_enabled=self.scalar_deterministic_shadow_enabled,
            scalar_deterministic_router_enabled=self.scalar_deterministic_router_enabled,
            scalar_deterministic_router_promoted_classes=self.scalar_deterministic_router_promoted_classes,
            enable_counter_state=self.personal_memory_enabled,
            enable_event_history=self.event_history_enabled,
            event_history_perceiver_version=self.event_history_perceiver_version,
            event_batch_size=self.event_history_batch_size,
        )
