"""Lifecycle, lease-ownership, and run-loop methods for :class:`MaintenanceScheduler`.

Split out of ``menhir.services.maintenance_scheduler`` (file-size refactor): the methods are
bound onto ``MaintenanceScheduler`` there via this mixin, so the class surface and behavior
are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from menhir.services.scheduler_lease import _utc_now_iso

logger = logging.getLogger(__name__)


class _MaintenanceLifecycleMixin:
    """Lifecycle and lease-ownership methods for ``MaintenanceScheduler``."""

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
            acquire_started = time.monotonic()
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
                self._block_jobs_until_displaced_owner_expires(previous_owner, acquire_started)
                logger.warning(
                    "Maintenance scheduler lease force takeover; lease=%s previous_owner_pid=%s "
                    "reason=%s jobs_deferred_s=%.1f",
                    self.lease_name,
                    previous_owner.get("owner_pid") if previous_owner is not None else None,
                    self._last_force_takeover_reason,
                    max(0.0, self._jobs_blocked_until - time.monotonic()),
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
            # The acquire is itself a proof of ownership; without stamping it here the first
            # job of the run would be refused for want of a renewal that has not happened yet.
            self._stamp_lease_deadline(acquire_started)
            self._lease_lost_event.clear()
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
        """Stop the run loop and release the lease, atomically with respect to `start()`.

        THE WHOLE SEQUENCE HOLDS `_state_lock` (CF-240). It used to drop the lock right after
        clearing `_task`, then await the task and release the lease outside it. A `start()`
        arriving in that window saw `is_running()` False, re-acquired -- `try_acquire` admits
        `existing_owner_id == owner_id`, and this is the same instance, so the guard was no
        guard -- cleared `_stop_event`, and created a second `_run_loop`. Then this method
        resumed and released the lease the NEW loop believed it held: a live loop running
        maintenance with no lease row, and any other process free to acquire and run
        concurrently.

        `owner_id` cannot distinguish those two acquires, because it is per-instance and stable
        across start/stop cycles. Serializing the sequence is what makes the interleaving
        impossible; detecting it afterwards would be too late, the release having already
        happened.

        Safe to hold across the await: nothing reachable from `_run_loop` takes `_state_lock`,
        and the loop is bounded -- it checks `_stop_event` once per tick and once per job -- so
        this cannot deadlock or hang longer than the previous code's own unguarded `await task`.
        """
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
                logger.debug(
                    "Maintenance scheduler task already cancelled during stop owner_id=%s",
                    self._owner_id,
                )
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

    def _block_jobs_until_displaced_owner_expires(
        self, previous_owner: dict[str, object] | None, acquire_started: float
    ) -> None:
        """Defer THIS owner's jobs until the displaced owner can no longer believe it owns the
        lease (CF-241).

        `force_acquire` overwrites the row unconditionally -- that is what an operator escape hatch
        is for, and it stays. What it cannot do is tell the displaced process anything. That process
        gates its own mutations on `_lease_is_provable()`, a MONOTONIC deadline stamped at its last
        successful renewal, so it keeps running jobs until its next heartbeat renewal fails. Between
        the takeover and that heartbeat, two loops run maintenance against the same graph -- which
        is the one thing the lease exists to prevent.

        The durable fact that bounds the displaced owner is the row we just replaced:
        `lease_expires_at` is the latest moment it could still consider itself provable, because its
        own deadline was stamped from a renewal no later than that. Waiting past it is therefore a
        PROOF, not an estimate. Detecting the displacement faster from the other side would be an
        improvement, not a substitute: nothing the taking process does can make a stalled or
        suspended peer notice sooner.

        The lease is still claimed immediately, so no third process can take it while we wait, and
        the loop still starts -- only job EXECUTION is deferred. Takeover of an already-expired,
        absent, or self-owned lease waits for nothing.
        """
        self._jobs_blocked_until = 0.0
        if not previous_owner:
            return
        if str(previous_owner.get("owner_id") or "") == self._owner_id:
            return
        try:
            expires_at = float(previous_owner.get("lease_expires_at"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # An unreadable expiry is not evidence the peer is finished. Fall back to a full lease
            # duration, which is the longest any owner's provable window can be.
            self._jobs_blocked_until = acquire_started + self.lease_duration_s
            return
        # The row's clock is wall-clock (`time.time`); the gate is monotonic. Convert through the
        # REMAINING interval rather than comparing the two clocks, which are not commensurable.
        remaining = expires_at - time.time()
        if remaining <= 0.0:
            return
        self._jobs_blocked_until = acquire_started + min(remaining, self.lease_duration_s)

    def _jobs_are_deferred(self) -> bool:
        """True while a displaced owner could still be mutating (CF-241)."""
        return time.monotonic() < self._jobs_blocked_until

    def _stamp_lease_deadline(self, acquired_at: float) -> None:
        """Record how long this process can prove it owns the lease.

        `acquired_at` is sampled BEFORE the store call, never after: the store's own expiry
        starts running during that call, so dating the window from the return value would
        claim validity the store does not grant. Erring short can only stop work early.
        """
        self._lease_valid_until = acquired_at + self.lease_duration_s

    def _lease_is_provable(self) -> bool:
        """True only while a successful renewal still covers the present moment (CF-100).

        This is deliberately not "has something told me I lost the lease". A renewal that
        returns False is one way to lose it; the others leave no signal at all:

        - the event loop is blocked long enough that the heartbeat never runs (CF-99), so no
          renewal is attempted and none can fail;
        - the heartbeat task dies on an unexpected store error;
        - the process is suspended.

        In each case the lease expires under a live holder and another scheduler acquires it,
        while every in-memory flag here still says "owner". Only the clock catches that.
        """
        if self._lease_lost:
            return False
        return time.monotonic() < self._lease_valid_until

    def _mark_lease_lost(self) -> None:
        self._lease_lost = True
        self._lease_acquired = False
        self._lease_valid_until = 0.0
        self._lease_blocked_reason = "lease_lost"
        self._lease_lost_event.set()
        # Wake the job loop so it stops before starting more work. A forced takeover
        # (bug FC-02) must not let the displaced owner keep mutating shared state.
        self._stop_event.set()

    def _lease_supervision_active(self) -> bool:
        """Whether lease ownership governs this job run at all.

        A scheduler that has never established ownership is not a displaced owner: there is no
        second owner it could be racing, so there is nothing for the deadline to protect. That
        is the direct-invocation shape -- a job wrapper called without `start()`.

        This is not a fail-open on unknown state, because no production path runs a job without
        a prior successful acquire. Both callers were checked: `_run_due_jobs` is reachable only
        from `_run_loop`, which `start()` creates only after `try_acquire`/`force_acquire`
        succeeds; and `core.runtime._run_initial_structure_scan` -- the one caller of `_run_job`
        outside this class -- is spawned inside an `elif` on `status_snapshot()["running"]`, so
        it too runs only under a held lease and IS supervised.

        `_lease_lost` is included because `_mark_lease_lost` zeroes the deadline: without it, a
        scheduler that had just lost the lease would read as one that never held it.
        """
        return self._lease_lost or self._lease_valid_until > 0.0

    async def _wait_for_lease_loss(self) -> None:
        """Return as soon as this process can no longer prove it owns the lease.

        Two ways in. `_lease_lost_event` is the fast path: a renewal that came back False, or
        a force takeover, is a definite answer and wakes this immediately. The timeout is the
        slow path, and it is the one that matters, because the dangerous losses are silent --
        nothing fails, the deadline simply passes.
        """
        while True:
            if not self._lease_is_provable():
                return
            remaining = self._lease_valid_until - time.monotonic()
            try:
                await asyncio.wait_for(
                    self._lease_lost_event.wait(), timeout=max(0.01, remaining)
                )
            except asyncio.TimeoutError:
                # The heartbeat may have pushed the deadline out while we waited; re-check
                # rather than assuming the timeout means loss.
                continue
            return

    async def _heartbeat_loop(self) -> None:
        """Renew the lease on a fixed cadence independent of job execution.

        Without this, the lease was renewed only once per loop iteration and then a batch of
        unbounded awaited jobs ran; a job longer than the lease let another process acquire
        the "expired" lease and mutate shared state concurrently (AR-02). On renewal failure —
        lease lost, or force-taken-over by another owner — signal the loop to stop (FC-02).
        """
        interval = self._heartbeat_interval_s()
        while not self._stop_event.is_set() and not self._lease_lost:
            try:
                renewed = self._renew_lease()
            except Exception:
                # A store error is not evidence that the lease was taken: it is evidence that
                # ownership is unknown. Declaring loss here would let one locked SQLite write
                # stop maintenance outright. Keep ticking and let the deadline decide -- if the
                # errors persist past _lease_valid_until, _lease_is_provable goes false on its
                # own and every job is refused. What must not happen is this task dying and
                # leaving the job loop running with nothing renewing anything (CF-100).
                logger.warning(
                    "Maintenance scheduler lease renewal errored; ownership unproven until it "
                    "succeeds owner_id=%s",
                    self._owner_id,
                    exc_info=True,
                )
                renewed = True
            if not renewed:
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
        self._lease_lost_event.clear()
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
