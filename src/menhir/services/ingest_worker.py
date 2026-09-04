"""Deferred enrichment worker execution and heartbeat operations."""

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
    LlmUsageControlSignal,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.infrastructure.scheduler_trace import (
    build_episode_scheduler_task,
)
from menhir.infrastructure.telemetry import (
    record_episode_task_event,
    record_lifecycle_event,
    record_llm_usage_event,
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
    try_reconcile_existing,
)
from menhir.services.ingest_gate import IngestGate

logger = logging.getLogger(__name__)

from menhir.services.ingest_models import (
    _belief_commit_context,
    _parse_occurred_at,
)

class LlmBudgetExceeded(LlmUsageControlSignal, RuntimeError):
    """An episode tried to make more LLM calls than its per-job budget permits (CF-79).

    Raised from the usage callback at the moment a call is reserved, so the call never
    dispatches. Distinct from a provider error: nothing failed, the work was refused.
    """


class IngestWorkerMixin:
    async def _enrichment_worker_loop(self) -> None:
        queue = self._queue()
        while True:
            try:
                episode_uuid = await asyncio.wait_for(
                    queue.get(),
                    timeout=self._lease_recovery_poll_s,
                )
            except asyncio.TimeoutError:
                # A cancellation delivered while wait_for's OWN timeout is firing can be
                # surfaced as TimeoutError rather than CancelledError. Swallowing it here
                # restarts the loop, so the task never dies: it sits in state "cancelling"
                # forever and asyncio's _cancel_all_tasks -- which gathers the cancelled
                # tasks at loop teardown -- never returns. That wedges the interpreter, not
                # just this worker.
                #
                # Task.cancelling() > 0 means a cancel was requested, so honour it rather
                # than treating it as an idle poll. Only reached under that race; ordinary
                # idle timeouts still fall through to lease recovery.
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError from None
                await self._recover_stale_episode_leases(limit=100)
                continue
            try:
                await self._process_pending_episode(episode_uuid)
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ):  # worker loop: log and continue to avoid killing the worker
                logger.exception(
                    "Unexpected pending enrichment worker failure episode_id=%s",
                    episode_uuid,
                )
            finally:
                self._queued_episode_ids.discard(episode_uuid)
                queue.task_done()

    async def _process_pending_episode(self, episode_uuid: str) -> None:
        claimed = await asyncio.to_thread(
            self.graph_adapter.claim_pending_episode,
            episode_uuid,
            max_attempts=self._max_enrichment_attempts,
            context_retry_attempts=self._context_window_retry_attempts,
            worker_id=self._worker_id,
            lease_seconds=self._enrichment_lease_seconds,
        )
        if claimed is None:
            record_lifecycle_event(
                component="ingest_worker",
                event="claim_pending_episode",
                state="skipped",
                episode_uuid=episode_uuid,
            )
            return

        started = perf_counter()
        processing_attempts = int(claimed.get("processing_attempts") or 0)
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._processing_heartbeat_loop(episode_uuid, heartbeat_stop),
            name=f"menhir-enrichment-heartbeat-{episode_uuid}",
        )
        # The session key is captured HERE, not looked up inside the callback: the callback fires
        # from worker threads (graphiti dispatches model calls through asyncio.to_thread), where
        # per-request context is not available. Closing over it is what lets the per-call
        # reservation know which window it is spending from.
        from menhir.services.ingest_queue import session_budget_key

        budget_key = session_budget_key(episode_uuid, claimed.get("session_id"))
        usage_token = set_llm_usage_callback(
            lambda event: self._record_episode_llm_usage(
                episode_uuid, event, budget_key=budget_key
            )
        )

        ctx = EnrichmentContext(
            episode_uuid=episode_uuid,
            claimed=claimed,
            started=started,
            processing_attempts=processing_attempts,
            worker_id=self._worker_id,
            graph_adapter=self.graph_adapter,
            graphiti_client=self.graphiti_client,
            lifecycle_service=self.lifecycle_service,
            llm=self.llm,
            ingest_gate=self._gate(),
            processing_steps_total=self._processing_steps_total,
            settings_record_revisions=self._settings_record_revisions,
            ready_warning_ms=self._ready_warning_ms,
            graphiti_add_episode_timeout_s=self._graphiti_add_episode_timeout_s,
            graphiti_episode_max_estimated_tokens=self._graphiti_episode_max_estimated_tokens,
            canonical_self_binding_mode=self._canonical_self_binding_mode,
            get_queue_depth=self.get_queue_depth,
            shadow_context_composition=self._shadow_context_composition,
            shadow_composition_timeout_s=self._shadow_composition_timeout_s,
            register_background_task=self._register_shadow_task,
        )

        try:
            record_lifecycle_event(
                component="ingest_worker",
                event="claim_pending_episode",
                state="completed",
                episode_uuid=episode_uuid,
                details={
                    "worker_id": self._worker_id,
                    "processing_attempts": processing_attempts,
                },
            )
            logger.info(
                "Episode enrichment begin episode_id=%s worker=%s attempt=%s source=%s",
                episode_uuid,
                self._worker_id,
                processing_attempts,
                claimed.get("source"),
            )
            if await try_reconcile_existing(ctx):
                return
            if await run_preflight_rejection(ctx):
                return

            # Phase 6: LLM budget cap — rolling-window check before extraction
            budget_retry = await self._check_session_budget(
                episode_uuid,
                claimed.get("session_id"),
            )
            if budget_retry is not None:
                logger.warning(
                    "Budget cap hit for episode %s — requeueing with retry_after=%.1fs",
                    episode_uuid,
                    budget_retry,
                )
                await asyncio.to_thread(
                    self.graph_adapter.mark_episode_pending,
                    episode_uuid,
                    retry_after_s=budget_retry,
                    worker_id=self._worker_id,
                )
                record_lifecycle_event(
                    component="ingest_worker",
                    event="budget_cap_requeue",
                    state="completed",
                    episode_uuid=episode_uuid,
                    details={"retry_after_s": budget_retry},
                )
                return

            # PART 4: the per-job LLM budget is enforced at the RESERVATION point, in
            # `_record_episode_llm_usage`, not here (CF-79). A pre-attempt check on
            # `_job_llm_call_counts` could never fire: the `finally` below pops the counter at the
            # end of every attempt, so a fresh attempt always reads 0. It could only have stopped
            # an attempt that was already over budget when it began, which is not a state this
            # code can produce. The amplification the finding proves is WITHIN one attempt --
            # entity count multiplies judge calls, and episode content drives entity count -- so
            # only an in-flight bound stops it.

            await run_graphiti_extraction(ctx, finalize_under_gate=True)
        except CircuitOpenError as exc:
            logger.warning(
                "Circuit open for graphiti — requeueing episode %s: %s",
                episode_uuid,
                exc,
            )
            await asyncio.to_thread(
                self.graph_adapter.mark_episode_pending,
                episode_uuid,
                retry_after_s=30.0,
                worker_id=self._worker_id,
            )
            record_lifecycle_event(
                component="ingest_worker",
                event="circuit_breaker_requeue",
                state="completed",
                episode_uuid=episode_uuid,
                details={"error": str(exc)},
            )
            return
        except Exception as exc:
            self._failed_enrichments += 1
            await handle_enrichment_failure(ctx, exc)
            return
        finally:
            reset_llm_usage_callback(usage_token)
            clear_extraction_receipt()
            self._job_llm_call_counts.pop(episode_uuid, None)
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        return

    def _record_episode_llm_usage(
        self, episode_uuid: str, event: LLMUsageEvent, *, budget_key: str | None = None
    ) -> None:
        """Increment live LLM task counters for one processing episode."""

        if event.phase == "started":
            # CF-79: this is a RESERVATION, not a meter. The counter is incremented before the
            # call is allowed to proceed, and breaching the budget raises rather than logging.
            #
            # It read `budget` and `limit` and behaved as telemetry: it warned and the call went
            # ahead, as did every call after it. That matters because the quantity is influenced
            # by an untrusted party -- one attempt costs roughly
            # `1 + N_entities searches + (surviving pairs x 3)` judge calls, and episode content
            # decides N_entities. A control that counts an attacker-shaped quantity and does
            # nothing is not a control.
            #
            # Raising lands in `_process_episode`'s `except Exception` and goes through
            # `handle_enrichment_failure`, the same path any mid-episode LLM error already takes.
            count = self._job_llm_call_counts.get(episode_uuid, 0) + 1
            self._job_llm_call_counts[episode_uuid] = count
            if count > self._budget_settings_max_per_job and event.report_only:
                # CF-234 measurement mode. The call is counted -- so the overrun is visible and
                # the counter stays truthful for every later call in this job -- but it is not
                # refused. Enforcing here would need a handler that does not exist yet: an
                # unhandled `LlmBudgetExceeded` reaches `_process_episode`'s generic
                # `except Exception` and marks the episode FAILED, which is a worse outcome than
                # the over-spend it prevents.
                logger.warning(
                    "Per-job LLM budget WOULD refuse call %d for episode %s (limit=%d, "
                    "report-only): %s %s",
                    count,
                    episode_uuid,
                    self._budget_settings_max_per_job,
                    event.kind,
                    event.operation or "",
                )
            elif count > self._budget_settings_max_per_job:
                logger.warning(
                    "Per-job LLM budget exceeded for episode %s: %d calls (limit=%d) -- refusing",
                    episode_uuid,
                    count,
                    self._budget_settings_max_per_job,
                )
                raise LlmBudgetExceeded(
                    f"episode {episode_uuid} exceeded its per-job LLM budget "
                    f"({count} calls, limit {self._budget_settings_max_per_job})"
                )

            # CF-79 session half: reserve the SAME call against the session window, so the window
            # meters calls rather than attempts. Ordered after the per-job reservation so a single
            # runaway episode is attributed to itself first -- reporting a per-job overrun as a
            # session exhaustion would point the operator at the wrong cause.
            #
            # `budget_key` is None only for direct invocation in tests; production always passes
            # it. Refusing on absence would break those call sites for no safety gain, since a
            # caller with no session has no window to exhaust.
            if budget_key is not None and not self.reserve_session_llm_call(budget_key):
                if event.report_only:
                    # Reserved (the window is debited, so the measurement is honest) but not
                    # refused, for the same reason as the per-job branch above.
                    logger.warning(
                        "Session LLM budget WOULD refuse a call for key %s while episode %s was "
                        "running (report-only)",
                        budget_key,
                        episode_uuid,
                    )
                    # Deliberately NOT `return`: the raise below skips the usage write, but a
                    # measured call must still be recorded or the measurement loses the very
                    # calls it exists to count.
                else:
                    logger.warning(
                        "Session LLM budget exhausted for key %s while episode %s was running "
                        "-- refusing the call",
                        budget_key,
                        episode_uuid,
                    )
                    raise LlmBudgetExceeded(
                        f"session {budget_key} exhausted its LLM call budget "
                        f"(limit {self._budget_settings_max_calls} calls per "
                        f"{self._budget_settings_window_s}s)"
                    )

        scheduler_task = build_episode_scheduler_task(
            episode_uuid=episode_uuid,
            provider="graphiti",
            action="add-episode",
        )
        logical_task = "memory: graphiti add_episode"
        try:
            self.graph_adapter.increment_episode_llm_usage(
                episode_uuid,
                task_delta=1 if event.phase == "started" else 0,
                phase=event.phase,
                kind=event.kind,
                model=event.model,
                endpoint=event.endpoint,
                task=logical_task,
                error=event.error,
            )
        except (OSError, RuntimeError):
            logger.debug(
                "Failed to record LLM usage for episode=%s kind=%s phase=%s endpoint=%s",
                episode_uuid,
                event.kind,
                event.phase,
                event.endpoint,
                exc_info=True,
            )
        try:
            details = {
                key: value
                for key, value in {
                    "call_id": event.call_id,
                    "operation": event.operation,
                    "duration_ms": event.duration_ms,
                    "input_tokens": event.input_tokens,
                    "output_tokens": event.output_tokens,
                    "total_tokens": event.total_tokens,
                    "cached_input_tokens": event.cached_input_tokens,
                    "reasoning_output_tokens": event.reasoning_output_tokens,
                    "error": event.error,
                }.items()
                if value is not None
            }
            record_episode_task_event(
                episode_uuid=episode_uuid,
                parent_task=logical_task,
                child_task=event.endpoint or event.kind,
                phase=event.phase,
                kind=event.kind,
                model=event.model,
                endpoint=event.endpoint,
                scheduler_task=scheduler_task,
                details=details or None,
            )
        except (OSError, RuntimeError):
            logger.debug(
                "Failed to record episode task event for episode=%s kind=%s phase=%s endpoint=%s",
                episode_uuid,
                event.kind,
                event.phase,
                event.endpoint,
                exc_info=True,
            )
        record_llm_usage_event(event=event, episode_uuid=episode_uuid)

    async def _processing_heartbeat_loop(
        self, episode_uuid: str, stop_event: asyncio.Event
    ) -> None:
        from menhir.infrastructure.llama_endpoint import (
            scheduler_url_from_env,
            should_use_scheduler,
        )

        scheduler_url = scheduler_url_from_env()
        ping_url = f"{scheduler_url}/ping"
        fallback_base_url = str(
            getattr(self.graphiti_client, "scheduler_fallback_base_url", "") or ""
        )
        use_scheduler = should_use_scheduler(fallback_base_url)
        _ping_every_n = max(
            1, int(60.0 / self._processing_heartbeat_interval_s)
        )  # ~60s
        _tick = 0
        while not stop_event.is_set():
            await asyncio.sleep(self._processing_heartbeat_interval_s)
            if stop_event.is_set():
                break
            try:
                await asyncio.to_thread(
                    self.graph_adapter.touch_episode_processing_heartbeat,
                    episode_uuid,
                    worker_id=self._worker_id,
                )
                record_lifecycle_event(
                    component="ingest_worker",
                    event="heartbeat_touch",
                    state="completed",
                    episode_uuid=episode_uuid,
                )
            except (OSError, RuntimeError):
                record_lifecycle_event(
                    component="ingest_worker",
                    event="heartbeat_touch",
                    state="failed",
                    episode_uuid=episode_uuid,
                )
                logger.debug(
                    "Failed to refresh processing heartbeat for %s",
                    episode_uuid,
                    exc_info=True,
                )
            _tick += 1
            if use_scheduler and _tick % _ping_every_n == 0:
                try:
                    if (
                        self._scheduler_http_client is None
                        or self._scheduler_http_client.is_closed
                    ):
                        self._scheduler_http_client = httpx.AsyncClient(timeout=2.0)
                    await self._scheduler_http_client.post(ping_url)
                except (httpx.HTTPError, OSError):
                    logger.debug(
                        "Scheduler keepalive ping failed for episode=%s",
                        episode_uuid,
                        exc_info=True,
                    )
