"""Enrichment pipeline step 5: classify and emit events for enrichment failures.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
Telemetry helpers are resolved through the facade module at call time so monkeypatching
``menhir.services.enrichment_steps.<name>`` keeps affecting this step, exactly as when
everything lived in one module.
"""

from __future__ import annotations

import logging
import traceback
from time import perf_counter

from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_graphiti_output_parse_error,
)

from menhir.services.enrichment_steps_context import EnrichmentContext
from menhir.services.enrichment_steps_helpers import failure_details_from_exception

#: Same logger object/name as the facade module: every record this step emits must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


# ---------------------------------------------------------------------------
# Pipeline step 5 — handle enrichment failure
# ---------------------------------------------------------------------------

async def handle_enrichment_failure(
    ctx: EnrichmentContext,
    exc: BaseException,
) -> None:
    """Classify and emit events for enrichment failures.

    Note: the caller is responsible for incrementing ``_failed_enrichments``
    *before* calling this function.
    """

    from menhir.services import enrichment_steps as _facade

    duration_ms = int((perf_counter() - ctx.started) * 1000)
    error_type = type(exc).__name__
    classification = classify_enrichment_failure(exc, error_type=error_type)
    failed = ctx.graph_adapter.mark_episode_failed(
        ctx.episode_uuid,
        str(exc),
        worker_id=ctx.worker_id,
        transient_requeue=classification == "retryable",
        claim_started_at=ctx.claimed.get("processing_started_at"),
    )
    if not failed:
        logger.info(
            "Skipping failed finalization after ownership lost episode_id=%s worker=%s error=%s",
            ctx.episode_uuid,
            ctx.worker_id,
            exc,
        )
        return
    failure_stage = (
        "graphiti_invalid_output"
        if is_graphiti_output_parse_error(exc, error_type=error_type)
        else "graphiti_exception"
    )
    failure_details = {
        "source": ctx.claimed.get("source"),
        "session_id": ctx.claimed.get("session_id"),
        "user_id": ctx.claimed.get("user_id"),
        "duration_ms": duration_ms,
    }
    failure_details.update(failure_details_from_exception(exc))
    _facade.record_lifecycle_event(
        component="ingest_worker",
        event="episode_failed",
        state="failed",
        episode_uuid=ctx.episode_uuid,
        details={"error_type": type(exc).__name__, "error": str(exc)},
    )
    _facade.record_mcp_event(
        kind="background",
        operation="episode_enrichment",
        payload={
            "episode_uuid": ctx.episode_uuid,
            "processing_attempts": ctx.processing_attempts,
            "queue_depth": ctx.get_queue_depth(),
        },
        duration_ms=duration_ms,
        success=False,
        error=str(exc),
    )
    _facade.record_failure_event(
        operation="episode_enrichment",
        episode_uuid=ctx.episode_uuid,
        failure_stage=failure_stage,
        classification=classification,
        retryable=classification == "retryable",
        processing_attempt=ctx.processing_attempts,
        queue_depth=ctx.get_queue_depth(),
        worker_id=ctx.worker_id,
        error_type=error_type,
        error=str(exc),
        traceback_text="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        details=failure_details,
    )
    logger.exception("Background enrichment failed episode_id=%s", ctx.episode_uuid)
