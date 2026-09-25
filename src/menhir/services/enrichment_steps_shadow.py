"""Shadow-mode context composition dispatch for the Graphiti extraction step.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
``record_lifecycle_event`` is resolved through the facade module at call time so
monkeypatching ``menhir.services.enrichment_steps.<name>`` keeps affecting this code,
exactly as when everything lived in one module.
"""

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Any

from menhir.services.enrichment_steps_context import EnrichmentContext
from menhir.services.enrichment_steps_helpers import coerce_reference_time, compose_episode_body
from menhir.services.shadow_context_composition import (
    build_shadow_trace,
    run_shadow_composition_with_timeout,
    shadow_trace_to_details,
)

#: Same logger object/name as the facade module: every record this code emits must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


def _dispatch_shadow_composition(
    ctx: EnrichmentContext,
    *,
    candidates: list,
    candidate_error: str | None,
    candidate_retrieval_ms: int,
    graphiti_result: Any,
) -> None:
    """Fire-and-forget: builds and logs the shadow trace without blocking the caller.
    Registered with IngestService's task-tracking set (via ctx.register_background_task)
    when a real IngestService is behind this ctx, so shutdown() can drain it cleanly;
    None (tests constructing EnrichmentContext directly) just means "don't track"."""
    task = asyncio.create_task(
        _run_shadow_composition_and_log(
            ctx, candidates=candidates, candidate_error=candidate_error,
            candidate_retrieval_ms=candidate_retrieval_ms, graphiti_result=graphiti_result,
        ),
        name=f"menhir-shadow-composition-{ctx.episode_uuid}",
    )
    if ctx.register_background_task is not None:
        ctx.register_background_task(task)


async def _run_shadow_composition_and_log(
    ctx: EnrichmentContext,
    *,
    candidates: list,
    candidate_error: str | None,
    candidate_retrieval_ms: int,
    graphiti_result: Any,
) -> None:
    from menhir.services import enrichment_steps as _facade

    shadow_started = perf_counter()
    try:
        prediction = await run_shadow_composition_with_timeout(
            ctx.llm,
            episode_uuid=ctx.episode_uuid,
            namespace=str(ctx.claimed.get("namespace") or "default"),
            episode_body=compose_episode_body(ctx.claimed),
            reference_time=coerce_reference_time(
                ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")
            ).isoformat(),
            candidates=candidates,
            candidate_query_error=candidate_error,
            candidate_retrieval_ms=candidate_retrieval_ms,
            timeout_s=ctx.shadow_composition_timeout_s,
        )
        shadow_total_ms = int((perf_counter() - shadow_started) * 1000)
        trace = build_shadow_trace(prediction, graphiti_result, shadow_total_ms=shadow_total_ms)
        _facade.record_lifecycle_event(
            component="ingest_shadow",
            event="extraction_composition",
            state="logged",
            episode_uuid=ctx.episode_uuid,
            details=shadow_trace_to_details(trace),
        )
    except Exception:
        # This function is never awaited by the real ingest path — a bug here must not
        # become an unretrieved-task-exception warning at GC time, and must never be
        # mistaken for a real extraction failure.
        logger.warning("Shadow composition logging failed episode_id=%s", ctx.episode_uuid, exc_info=True)
