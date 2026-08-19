"""Periodic maintenance scheduler for deferred enrichment operations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from menhir.infrastructure.telemetry import record_failure_event, record_mcp_event
from menhir.services.scheduler_lease import SchedulerLeaseStore, _utc_now_iso
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
    SchedulerLeaseStoreProtocol,
    SchedulerLifecycleService,
)
from menhir.services.scheduler_tasks import (
    auto_resolve_conflicts,
    compute_failed_retry_delay_s,
    confirm_conflicts,
    observe_queue_health,
    recover_stale_leases,
    refresh_structure_graphs,
    review_unresolved_conflicts,
    prune_telemetry_revisions,
    retry_failed_enrichments,
    consolidate_personal_memory,
    sync_experience_counters,
    sync_verifiers_job,
)

logger = logging.getLogger(__name__)


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
class MaintenanceScheduler:
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
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _jobs: dict[str, _JobState] = field(default_factory=dict, init=False, repr=False)
    _owner_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}:{uuid4()}", init=False, repr=False)
    _owner_pid: int = field(default_factory=os.getpid, init=False, repr=False)
    _lease_acquired: bool = field(default=False, init=False, repr=False)
    _lease_lost: bool = field(default=False, init=False, repr=False)
    _lease_blocked_reason: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_at: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_reason: str | None = field(default=None, init=False, repr=False)
    _last_force_takeover_from: dict[str, object] | None = field(default=None, init=False, repr=False)

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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        force_takeover: bool = False,
        takeover_reason: str | None = None,
    ) -> bool:
        async with self._state_lock:
            if self.is_running():
                return True
            if force_takeover:
                previous_owner = self.lease_store.force_acquire(
                    lease_name=self.lease_name,
                    owner_id=self._owner_id,
                    owner_pid=self._owner_pid,
                    lease_duration_s=self.lease_duration_s,
                )
                acquired = True
                self._last_force_takeover_at = _utc_now_iso()
                self._last_force_takeover_reason = (takeover_reason or "manual").strip() or "manual"
                self._last_force_takeover_from = previous_owner
                logger.warning(
                    "Maintenance scheduler lease force takeover; lease=%s previous_owner_pid=%s reason=%s",
                    self.lease_name,
                    previous_owner.get("owner_pid") if previous_owner is not None else None,
                    self._last_force_takeover_reason,
                )
            else:
                acquired = self.lease_store.try_acquire(
                    lease_name=self.lease_name,
                    owner_id=self._owner_id,
                    owner_pid=self._owner_pid,
                    lease_duration_s=self.lease_duration_s,
                )
            self._lease_acquired = acquired
            if not acquired:
                lease = self.lease_store.fetch(lease_name=self.lease_name)
                self._lease_blocked_reason = (
                    f"lease_held_by_pid={lease.get('owner_pid')}" if lease is not None else "lease_unavailable"
                )
                logger.warning(
                    "Maintenance scheduler start skipped; lease=%s owner_pid=%s owner_id=%s",
                    self.lease_name,
                    lease.get("owner_pid") if lease is not None else None,
                    lease.get("owner_id") if lease is not None else None,
                )
                return False
            self._lease_blocked_reason = None
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._run_loop(),
                name="menhir-maintenance-scheduler",
            )
            return True

    async def force_takeover(self, reason: str = "manual") -> bool:
        """Force this process to take the scheduler lease for troubleshooting."""
        return await self.start(force_takeover=True, takeover_reason=reason)

    async def stop(self) -> None:
        async with self._state_lock:
            task = self._task
            if task is None:
                if self._lease_acquired:
                    self.lease_store.release(lease_name=self.lease_name, owner_id=self._owner_id)
                    self._lease_acquired = False
                return
            self._stop_event.set()
            self._task = None
        if task.cancelled():
            logger.debug("Maintenance scheduler task already cancelled during stop owner_id=%s", self._owner_id)
        else:
            await task
        if self._lease_acquired:
            self.lease_store.release(lease_name=self.lease_name, owner_id=self._owner_id)
            self._lease_acquired = False

    def status_snapshot(self) -> dict[str, object]:
        lease = self.lease_store.fetch(lease_name=self.lease_name)
        return {
            "running": self.is_running(),
            "lease": {
                "name": self.lease_name,
                "owner_id": self._owner_id,
                "owner_pid": self._owner_pid,
                "acquired": self._lease_acquired,
                "blocked_reason": self._lease_blocked_reason,
                "active_owner": lease,
                "last_forced_takeover": {
                    "at": self._last_force_takeover_at,
                    "reason": self._last_force_takeover_reason,
                    "previous_owner": self._last_force_takeover_from,
                },
            },
            "jobs": {
                name: {
                    "interval_s": job.interval_s,
                    "last_started_at": job.last_started_at,
                    "last_completed_at": job.last_completed_at,
                    "last_duration_ms": job.last_duration_ms,
                    "last_success": job.last_success,
                    "last_result": job.last_result,
                    "runs": job.runs,
                }
                for name, job in self._jobs.items()
            },
        }

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    def _heartbeat_interval_s(self) -> float:
        """Lease heartbeat cadence.

        Frequent enough that an in-flight job can never let the lease silently expire under a
        second owner (bug AR-02). Capped at a third of the lease window so at least two
        heartbeats fit inside every lease, and floored so tests with tiny leases still tick.
        """
        return max(0.05, min(self.lease_heartbeat_s, self.lease_duration_s / 3.0))

    def _renew_lease(self) -> bool:
        return self.lease_store.renew(
            lease_name=self.lease_name,
            owner_id=self._owner_id,
            owner_pid=self._owner_pid,
            lease_duration_s=self.lease_duration_s,
        )

    def _mark_lease_lost(self) -> None:
        self._lease_lost = True
        self._lease_acquired = False
        self._lease_blocked_reason = "lease_lost"
        # Wake the job loop so it stops before starting more work. A forced takeover
        # (bug FC-02) must not let the displaced owner keep mutating shared state.
        self._stop_event.set()

    async def _heartbeat_loop(self) -> None:
        """Renew the lease on a fixed cadence independent of job execution.

        Without this, the lease was renewed only once per loop iteration and then a batch of
        unbounded awaited jobs ran; a job longer than the lease let another process acquire
        the "expired" lease and mutate shared state concurrently (AR-02). On renewal failure —
        lease lost, or force-taken-over by another owner — signal the loop to stop (FC-02).
        """
        interval = self._heartbeat_interval_s()
        while not self._stop_event.is_set() and not self._lease_lost:
            if not self._renew_lease():
                logger.warning(
                    "Maintenance scheduler lease lost during heartbeat; stopping owner_id=%s",
                    self._owner_id,
                )
                self._mark_lease_lost()
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _run_loop(self) -> None:
        logger.info("Maintenance scheduler started")
        self._lease_lost = False
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="menhir-maintenance-heartbeat"
        )
        try:
            while not self._stop_event.is_set():
                if self._lease_lost or not self._renew_lease():
                    self._mark_lease_lost()
                    logger.warning("Maintenance scheduler lease lost; stopping loop owner_id=%s", self._owner_id)
                    break
                await self._run_due_jobs()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.tick_interval_s)
                except asyncio.TimeoutError:
                    continue
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            logger.info("Maintenance scheduler stopped")

    async def _run_due_jobs(self) -> None:
        now = datetime.now(timezone.utc)
        for name, job in self._jobs.items():
            if self._lease_lost or self._stop_event.is_set():
                # Lease lost or shutdown requested mid-batch: stop before the next job so a
                # displaced owner does not keep running maintenance (FC-02 / AR-02).
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
            elif name == "prune_telemetry_revisions":
                await self._run_job(
                    job, "scheduler_prune_telemetry_revisions", self._make_prune_telemetry_revisions()
                )

    # ------------------------------------------------------------------
    # Job wrapper — shared timing, telemetry, and state bookkeeping
    # ------------------------------------------------------------------

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
            result = await coro
            record_mcp_event(
                kind="background",
                operation=operation,
                payload=result,
                result=result,
                duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                success=True,
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

    def _make_prune_telemetry_revisions(self) -> Awaitable[dict[str, object]]:
        return prune_telemetry_revisions(retention_days=self.revision_retention_days)

    def _make_recover_stale_leases(self) -> Awaitable[dict[str, object]]:
        return recover_stale_leases(self.ingest_service, recovery_limit=self.recovery_limit)

    def _make_retry_failed_enrichments(self) -> Awaitable[dict[str, object]]:
        return retry_failed_enrichments(
            self.ingest_service, self.graph_adapter, failed_retry_limit=self.failed_retry_limit,
        )

    def _make_observe_queue_health(self) -> Awaitable[dict[str, object]]:
        return observe_queue_health(self.ingest_service, self.graph_adapter)

    def _make_auto_resolve_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return auto_resolve_conflicts(
            self.lifecycle_service,
            max_age_days=self.conflict_auto_resolve_max_age_days,
            limit=self.conflict_auto_resolve_limit,
        )

    def _make_confirm_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return confirm_conflicts(self.lifecycle_service, limit=self.conflict_confirm_limit)

    def _make_review_unresolved_conflicts(self) -> Awaitable[dict[str, object]]:
        assert self.lifecycle_service is not None
        return review_unresolved_conflicts(
            self.lifecycle_service, limit=self.conflict_review_unresolved_limit,
        )

    def _make_refresh_structure_graphs(self) -> Awaitable[dict[str, object]]:
        return refresh_structure_graphs(self.graph_adapter)

    def _make_sync_experience_counters(self) -> Awaitable[dict[str, object]]:
        return sync_experience_counters(self.graph_adapter, embed=self.experience_embed)

    def _make_sync_verifiers(self) -> Awaitable[dict[str, object]]:
        return sync_verifiers_job(
            self.graph_adapter,
            verifier_repo=self.verifier_repo,
            verifier_context=self.verifier_context,
            embed=self.experience_embed,
        )

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

    async def _make_consolidate_lifecycle(self) -> dict[str, object]:
        assert self.lifecycle_service is not None
        result = await self.lifecycle_service.recover_orphans()
        return {
            "promoted": result.promoted,
            "deleted": result.deleted,
            "demoted": result.demoted,
            "conflicts_detected": result.conflicts_detected,
            "skipped_pending": result.skipped_pending,
            "orphan_episodes_cleaned": result.orphan_episodes_cleaned,
        }

    async def _make_decay_lifecycle(self) -> dict[str, object]:
        assert self.lifecycle_service is not None
        result = await self.lifecycle_service.apply_decay()
        return {
            "edge_counts_synced": result.edge_counts_synced,
            "sharpness_recalculated": result.sharpness_recalculated,
            "compressed": result.compressed,
            "deleted": result.deleted,
            "edges_bridged": result.edges_bridged,
            "orphan_subgraphs_cleaned": result.orphan_subgraphs_cleaned,
        }

    # ------------------------------------------------------------------
    # Backward-compat delegators (used by tests)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_timestamp(value: object | None) -> datetime | None:
        from menhir.services.scheduler_tasks import _parse_timestamp
        return _parse_timestamp(value)

    @staticmethod
    def compute_failed_retry_delay_s(processing_attempts: object | None) -> int:
        return compute_failed_retry_delay_s(processing_attempts)

    async def _retry_process_candidate(
        self, row: dict, max_attempts: int, now: datetime,
    ) -> str:
        from menhir.services.scheduler_tasks import retry_process_candidate
        return await retry_process_candidate(
            self.graph_adapter, self.ingest_service, row, max_attempts, now,
        )

    async def _run_recover_stale_leases(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_recover_stale_leases", self._make_recover_stale_leases())

    async def _run_retry_failed_enrichments(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_retry_failed_enrichments", self._make_retry_failed_enrichments())

    async def _run_observe_queue_health(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_queue_health", self._make_observe_queue_health())

    async def _run_auto_resolve_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_auto_resolve_conflicts", self._make_auto_resolve_conflicts())

    async def _run_confirm_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_confirm_conflicts", self._make_confirm_conflicts())

    async def _run_review_unresolved_conflicts(self, job: _JobState) -> None:
        await self._run_job(job, "scheduler_review_unresolved_conflicts", self._make_review_unresolved_conflicts())
