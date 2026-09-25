"""Scheduler retry-family task functions.

Extracted verbatim from ``scheduler_tasks.py``; the facade module re-exports everything
here so existing ``menhir.services.scheduler_tasks`` import sites keep working unchanged.
``record_failure_event`` is resolved through the facade module at call time so
monkeypatching ``menhir.services.scheduler_tasks.record_failure_event`` keeps affecting
these tasks, exactly as when everything lived in one module.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.episode_lifecycle import TRANSIENT_RETRY_CAP
from menhir.infrastructure.episode_repository import is_recoverable_context_window_error
from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_session_window_refusal,
)
from menhir.services.enrichment_steps import record_retention_sources
from menhir.services.scheduler_protocols import (
    SchedulerGraphAdapter,
    SchedulerIngestService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(value: object | None) -> datetime | None:
    if value is None:
        return None
    rendered = str(value).strip()
    if not rendered:
        return None
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_failed_retry_delay_s(processing_attempts: object | None) -> int:
    attempts = max(1, int(processing_attempts or 1))
    return 30 * (2 ** max(0, attempts - 1))


# ---------------------------------------------------------------------------
# Job: retry failed enrichments
# ---------------------------------------------------------------------------

async def retry_process_candidate(
    graph_adapter: SchedulerGraphAdapter,
    ingest_service: SchedulerIngestService,
    row: dict,
    max_attempts: int,
    now: datetime,
) -> str:
    """Decide what to do with one failed enrichment candidate. Returns the action taken."""
    from menhir.services import scheduler_tasks as _facade

    episode_uuid = str(row.get("uuid") or "")
    anchor_name = str(row.get("name") or "")
    processing_attempts = int(row.get("processing_attempts") or 0)

    if episode_uuid and anchor_name:
        existing_completion = await asyncio.to_thread(
            graph_adapter.find_completed_episode_artifact,
            anchor_uuid=episode_uuid,
            anchor_name=anchor_name,
        )
        if existing_completion is not None:
            resolved_episode_uuid = str(existing_completion.get("resolved_episode_uuid") or "")
            entity_uuids = [str(u) for u in (existing_completion.get("entity_uuids") or []) if str(u)]
            edge_uuids = [str(u) for u in (existing_completion.get("edge_uuids") or []) if str(u)]
            stamp_kwargs: dict[str, object] = {}
            if row.get("bootstrap_scope") is not None:
                stamp_kwargs["bootstrap_scope"] = row.get("bootstrap_scope")
            stamped = await asyncio.to_thread(
                graph_adapter.stamp_ingest_metadata,
                node_uuids=[resolved_episode_uuid] + entity_uuids,
                edge_uuids=edge_uuids,
                session_id=str(row.get("session_id") or ""),
                user_id=str(row.get("user_id") or ""),
                source=str(row.get("source") or "claude-code"),
                source_confidence=source_confidence_for(str(row.get("source") or "claude-code")),
                namespace=str(row.get("namespace") or "default"),
                **stamp_kwargs,
            )
            record_retention_sources(
                graph_adapter,
                entity_uuids,
                source_episode_uuid=episode_uuid,
                namespace=str(row.get("namespace") or "default"),
            )
            if await asyncio.to_thread(
                graph_adapter.mark_episode_ready,
                episode_uuid,
                required_state="FAILED",
                resolved_episode_uuid=resolved_episode_uuid,
                nodes_touched=stamped.nodes_touched,
                edges_touched=stamped.edges_touched,
            ):
                return "reconciled"

    classification = classify_enrichment_failure(row.get("processing_error"))
    # Only an operator-clearable context-window error (local n_ctx too small) bypasses the
    # terminal gate below and earns the extended retry cap. A hosted provider's hard limit is
    # also a context-window error, but re-sending the same payload cannot ever succeed.
    context_window_mismatch = is_recoverable_context_window_error(row.get("processing_error"))
    session_window_refusal = is_session_window_refusal(row.get("processing_error"))
    error_text = str(row.get("processing_error") or "")
    queue_depth = ingest_service.get_queue_depth()
    context_cap = ingest_service.get_context_window_retry_attempts()
    effective_max = max(max_attempts, context_cap) if context_window_mismatch else max_attempts

    if classification in ("terminal", "manual_review") and not context_window_mismatch:
        _facade.record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_classification",
            classification=classification,
            retryable=False,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="terminal_failure" if classification == "terminal" else "manual_review_required",
            error=error_text or classification.replace("_", " "),
            details={"decision": "not_requeued"},
        )
        return "terminal"

    if processing_attempts >= effective_max:
        _facade.record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_attempts_exhausted",
            classification="exhausted",
            retryable=False,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="retry_attempts_exhausted",
            error=error_text or "retry attempts exhausted",
            details={
                "max_attempts": max_attempts,
                "effective_max": effective_max,
                "decision": "not_requeued",
                "context_window_mismatch": context_window_mismatch,
            },
        )
        return "exhausted"

    # Transient requeues (outage, circuit-open, backpressure) refund `processing_attempts`
    # (#79/#70), so the genuine-failure ceiling above cannot see them. Termination for a
    # permanently dead provider rides this separate, much larger counter instead.
    transient_retries = int(row.get("transient_retries") or 0)
    if classification == "retryable" and transient_retries >= TRANSIENT_RETRY_CAP:
        _facade.record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_transient_exhausted",
            classification="exhausted",
            retryable=False,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="transient_retries_exhausted",
            error=error_text or "transient retries exhausted",
            details={
                "transient_retries": transient_retries,
                "transient_cap": TRANSIENT_RETRY_CAP,
                "decision": "not_requeued",
            },
        )
        return "exhausted"

    completed_at = _parse_timestamp(row.get("processing_completed_at"))
    retry_delay_s = compute_failed_retry_delay_s(processing_attempts)
    if session_window_refusal:
        # The ordinary backoff starts at 30s. The session window is 900s by default, so without
        # this floor every attempt would land inside the same exhausted window, fail identically,
        # and burn the attempt ceiling -- parking the episode exactly as before while spending
        # calls to get there. Deferring past the window is what makes the retry meaningful.
        retry_delay_s = max(retry_delay_s, int(ingest_service.get_llm_session_window_seconds()))
    if completed_at is not None and (now - completed_at).total_seconds() < retry_delay_s:
        _facade.record_failure_event(
            operation="scheduler_retry_failed_enrichments",
            episode_uuid=episode_uuid,
            failure_stage="retry_backoff_wait",
            classification="retryable",
            retryable=True,
            processing_attempt=processing_attempts,
            queue_depth=queue_depth,
            error_type="retry_backoff_wait",
            error=error_text or "retry waiting for backoff",
            details={"retry_delay_s": retry_delay_s, "decision": "deferred"},
        )
        return "waiting"

    if await ingest_service.requeue_failed_episode(episode_uuid):
        return "requeued"
    return "skipped"


async def retry_failed_enrichments(
    ingest_service: SchedulerIngestService,
    graph_adapter: SchedulerGraphAdapter,
    *,
    failed_retry_limit: int = 50,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = dict(reconciled=0, requeued=0, terminal=0, waiting=0, exhausted=0)
    candidates = await asyncio.to_thread(
        graph_adapter.fetch_failed_episode_retry_candidates, limit=failed_retry_limit
    )
    max_attempts = ingest_service.get_max_enrichment_attempts()
    for row in candidates:
        action = await retry_process_candidate(graph_adapter, ingest_service, row, max_attempts, now)
        if action in counts:
            counts[action] += 1
    return {
        "candidates": len(candidates),
        **counts,
        "queue_depth": ingest_service.get_queue_depth(),
    }
