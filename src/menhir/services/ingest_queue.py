"""Ingest configuration, budget, queue, recovery, and shutdown operations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx

from menhir.domain import IngestResult, IngestStatus, MemorySession
from menhir.domain.models import ProcessingState
from menhir.infrastructure import GraphitiClient, LLMAdapter, MemoryGraphAdapter
from menhir.infrastructure.git_log import current_head
from menhir.infrastructure.paths import repo_root_for_project

if TYPE_CHECKING:
    from menhir.core.bootstrap import (
        UnavailableGraphitiClient,
        UnavailableLLMAdapter,
    )
from menhir.services.scheduler_protocols import LifecycleServiceProtocol
from menhir.infrastructure.observability import (
    LLMUsageEvent,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.infrastructure.scheduler_trace import (
    build_episode_scheduler_task,
)
from menhir.infrastructure.telemetry import (
    record_episode_task_event,
    record_lifecycle_event,
)
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.circuit_breaker import CircuitOpenError
from menhir.infrastructure.graphiti_patches import clear_extraction_receipt
from menhir.services.enrichment_steps import (
    EnrichmentContext,
    add_episode_with_timeout,
    build_episode_preflight_rejection,
    handle_enrichment_failure,
    run_graphiti_extraction,
    run_preflight_rejection,
    stamp_and_finalize,
    try_reconcile_existing,
)
from menhir.services.ingest_gate import IngestGate

logger = logging.getLogger(__name__)

from menhir.services.ingest_models import (
    _belief_commit_context,
    _parse_occurred_at,
)

class IngestQueueMixin:
    def configure(
        self,
        *,
        graphiti_add_episode_timeout_s: float | None = None,
        graphiti_episode_max_estimated_tokens: int | None = None,
        max_llm_calls_per_session_window: int | None = None,
        llm_session_window_seconds: int | None = None,
        max_llm_calls_per_enrichment_job: int | None = None,
        record_detailed_revisions: bool | None = None,
        enrichment_enabled: bool | None = None,
        enrichment_disabled_reason: str | None = None,
        ingest_concurrency: int | None = None,
        shadow_context_composition: bool | None = None,
        shadow_composition_timeout_s: float | None = None,
    ) -> None:
        """Apply runtime configuration values derived from MemorySettings."""
        if graphiti_add_episode_timeout_s is not None:
            self._graphiti_add_episode_timeout_s = max(
                1.0, float(graphiti_add_episode_timeout_s)
            )
        if graphiti_episode_max_estimated_tokens is not None:
            self._graphiti_episode_max_estimated_tokens = max(
                0, int(graphiti_episode_max_estimated_tokens)
            )
        if max_llm_calls_per_session_window is not None:
            self._budget_settings_max_calls = max(
                1, int(max_llm_calls_per_session_window)
            )
        if llm_session_window_seconds is not None:
            self._budget_settings_window_s = max(1, int(llm_session_window_seconds))
        if max_llm_calls_per_enrichment_job is not None:
            self._budget_settings_max_per_job = max(
                1, int(max_llm_calls_per_enrichment_job)
            )
        if record_detailed_revisions is not None:
            self._settings_record_revisions = bool(record_detailed_revisions)
        if enrichment_enabled is not None:
            self._enrichment_enabled = bool(enrichment_enabled)
        if enrichment_disabled_reason is not None:
            self._enrichment_disabled_reason = str(enrichment_disabled_reason)
        if ingest_concurrency is not None:
            new_concurrency = max(1, int(ingest_concurrency))
            if new_concurrency != self._ingest_concurrency:
                self._ingest_concurrency = new_concurrency
                # Rebuild lazily so the new bound takes effect (configure runs at
                # bootstrap, before the worker(s) start).
                self._ingest_gate_obj = None
        if shadow_context_composition is not None:
            self._shadow_context_composition = bool(shadow_context_composition)
        if shadow_composition_timeout_s is not None:
            self._shadow_composition_timeout_s = max(1.0, float(shadow_composition_timeout_s))

    def _register_shadow_task(self, task: asyncio.Task) -> None:
        """Track a detached shadow-composition task so shutdown() can drain it."""
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    def _gate(self) -> IngestGate:
        """The ingestion gate (bounded concurrency + per-namespace serialization)."""
        if self._ingest_gate_obj is None:
            self._ingest_gate_obj = IngestGate(self._ingest_concurrency)
        return self._ingest_gate_obj

    def enrichment_enabled(self) -> bool:
        return self._enrichment_enabled

    def enrichment_disabled_reason(self) -> str:
        return self._enrichment_disabled_reason

    def _queue(self) -> asyncio.Queue[str]:
        if self._pending_queue is None:
            self._pending_queue = asyncio.Queue()
        return self._pending_queue

    def get_queue_depth(self) -> int:
        """Return the current in-memory enrichment queue depth."""

        if self._pending_queue is None:
            return 0
        return self._pending_queue.qsize()

    def get_failed_enrichment_count(self) -> int:
        """Return the number of failed enrichment attempts observed in this runtime."""

        return self._failed_enrichments

    def get_max_enrichment_attempts(self) -> int:
        """Expose the retry ceiling used by the scheduler."""

        return self._max_enrichment_attempts

    def get_context_window_retry_attempts(self) -> int:
        """Expose the extended retry ceiling for context-window errors."""

        return self._context_window_retry_attempts

    async def _ensure_enrichment_worker(self) -> None:
        if not self._enrichment_enabled:
            return
        self._queue()
        # Prune finished loops, then top up to the configured concurrency. Each loop is an
        # independent consumer of the shared queue; per-episode claims are atomic, so N
        # loops never double-process, and the IngestGate bounds real parallelism.
        self._worker_tasks = [t for t in self._worker_tasks if not t.done()]
        while len(self._worker_tasks) < self._ingest_concurrency:
            self._worker_tasks.append(
                asyncio.create_task(
                    self._enrichment_worker_loop(),
                    name=f"menhir-enrichment-{len(self._worker_tasks)}",
                )
            )

    async def _enqueue_pending_episode(self, episode_uuid: str) -> None:
        if episode_uuid in self._queued_episode_ids:
            return
        self._queued_episode_ids.add(episode_uuid)
        queue = self._queue()
        await queue.put(episode_uuid)
        record_lifecycle_event(
            component="ingest_queue",
            event="episode_enqueued",
            state="completed",
            episode_uuid=episode_uuid,
            details={"queue_depth": queue.qsize()},
        )
        queue_depth = queue.qsize()
        if queue_depth > self._queue_warning_depth:
            logger.warning("Pending enrichment queue backlog depth=%s", queue_depth)

    async def _recover_stale_episode_leases(self, limit: int = 100) -> tuple[int, int]:
        """Reset expired ENRICHING leases and enqueue all retryable episodes."""

        exhausted_pending = await asyncio.to_thread(
            self.graph_adapter.fail_exhausted_pending_episodes,
            max_attempts=self._max_enrichment_attempts,
        )
        orphaned_resets = await asyncio.to_thread(
            self.graph_adapter.reset_orphaned_enriching_episodes,
            max_attempts=self._max_enrichment_attempts,
        )
        stale_resets = await asyncio.to_thread(
            self.graph_adapter.reset_stale_enriching_episodes,
            max_attempts=self._max_enrichment_attempts,
        )
        episode_ids = await asyncio.to_thread(
            self.graph_adapter.list_pending_episode_uuids,
            max_attempts=self._max_enrichment_attempts,
            limit=limit,
        )
        for episode_uuid in episode_ids:
            await self._enqueue_pending_episode(episode_uuid)
        if orphaned_resets > 0:
            logger.warning(
                "Recovered %d orphaned ENRICHING rows for worker=%s",
                orphaned_resets,
                self._worker_id,
            )
        if stale_resets > 0:
            logger.warning(
                "Recovered %d stale enrichment leases for worker=%s",
                stale_resets,
                self._worker_id,
            )
        if exhausted_pending > 0:
            logger.warning(
                "Marked %d exhausted pending episodes failed for worker=%s",
                exhausted_pending,
                self._worker_id,
            )
        return stale_resets + orphaned_resets, len(episode_ids)

    async def _check_session_budget(
        self, episode_uuid: str, session_id: str | None
    ) -> float | None:
        """Check rolling-window session budget. Returns retry_after_s if exceeded, else None."""

        now = monotonic()
        window_s = self._budget_settings_window_s
        budget_key = session_id or f"episode:{episode_uuid}"

        async with self._session_llm_budget_lock:
            calls = self._session_llm_call_times.setdefault(budget_key, deque())

            while calls and now - calls[0] >= window_s:
                calls.popleft()

            if len(calls) >= self._budget_settings_max_calls:
                retry_after_s = max(1.0, window_s - (now - calls[0]))
                logger.warning(
                    "LLM session budget exceeded for key %s (%d calls in %ds). "
                    "Episode queued for retry in %.1fs.",
                    budget_key,
                    len(calls),
                    window_s,
                    retry_after_s,
                )
                return retry_after_s

            calls.append(now)
        return None

    async def resume_pending_episodes(self, limit: int = 100) -> int:
        """Requeue any persisted pending episodes on runtime startup."""

        if not self._enrichment_enabled:
            record_lifecycle_event(
                component="ingest_queue",
                event="resume_pending_episodes_service",
                state="skipped",
                details={
                    "limit": limit,
                    "reason": self._enrichment_disabled_reason or "enrichment_disabled",
                },
            )
            return 0
        record_lifecycle_event(
            component="ingest_queue",
            event="resume_pending_episodes_service",
            state="started",
            details={"limit": limit},
        )
        await self._ensure_enrichment_worker()
        _, queued = await self._recover_stale_episode_leases(limit=limit)
        record_lifecycle_event(
            component="ingest_queue",
            event="resume_pending_episodes_service",
            state="completed",
            details={"limit": limit, "queued": queued},
        )
        return queued

    async def recover_stale_enrichment_leases(
        self, limit: int = 100
    ) -> tuple[int, int]:
        """Recover stale leases and re-enqueue retryable pending work."""

        if not self._enrichment_enabled:
            return 0, 0
        await self._ensure_enrichment_worker()
        return await self._recover_stale_episode_leases(limit=limit)

    async def enqueue_pending_episode(self, episode_uuid: str) -> bool:
        """Queue one already-pending episode for worker pickup."""

        if not self._enrichment_enabled:
            return False
        row = self.graph_adapter.fetch_episode_processing(episode_uuid)
        if row is None or row.get("processing_state") != ProcessingState.PENDING:
            return False
        await self._ensure_enrichment_worker()
        if episode_uuid in self._queued_episode_ids:
            return False
        await self._enqueue_pending_episode(episode_uuid)
        return True

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        """Enqueue one already-failed episode for an explicit retry attempt."""

        if not self._enrichment_enabled:
            return False
        row = self.graph_adapter.fetch_episode_processing(episode_uuid)
        if row is None or row.get("processing_state") != ProcessingState.FAILED:
            return False
        await self._ensure_enrichment_worker()
        if episode_uuid in self._queued_episode_ids:
            return False
        await self._enqueue_pending_episode(episode_uuid)
        return True

    async def shutdown(self) -> int:
        """Stop the in-process worker and release leases owned by this runtime."""

        released = self.graph_adapter.release_worker_episode_leases(
            self._worker_id,
            max_attempts=self._max_enrichment_attempts,
        )
        workers = self._worker_tasks
        for worker in workers:
            if not worker.done():
                worker.cancel()
        for worker in workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._worker_tasks = []
        self._pending_queue = None
        self._queued_episode_ids.clear()
        shadow_tasks = list(self._shadow_tasks)
        for shadow_task in shadow_tasks:
            if not shadow_task.done():
                shadow_task.cancel()
        for shadow_task in shadow_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await shadow_task
        # Remove only what was actually drained, not a blind clear() -- worker tasks are
        # cancelled+awaited above before this runs, so a new registration during this drain
        # is a narrow theoretical race rather than a real path, but difference_update costs
        # nothing extra and never silently drops a task this loop didn't actually await.
        self._shadow_tasks.difference_update(shadow_tasks)
        if self._scheduler_http_client is not None:
            await self._scheduler_http_client.aclose()
            self._scheduler_http_client = None
        record_lifecycle_event(
            component="ingest_queue",
            event="shutdown",
            state="completed",
            details={"worker_id": self._worker_id, "released": released},
        )
        return released
