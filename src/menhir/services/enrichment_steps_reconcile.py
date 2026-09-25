"""Enrichment pipeline steps 1-2: completion reconciliation and preflight rejection.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
Telemetry helpers are resolved through the facade module at call time so monkeypatching
``menhir.services.enrichment_steps.<name>`` keeps affecting these steps, exactly as when
everything lived in one module.
"""

from __future__ import annotations

import logging
from time import perf_counter

from menhir.domain.utils import source_confidence_for

from menhir.services.enrichment_steps_context import EnrichmentContext
from menhir.services.enrichment_steps_helpers import (
    build_episode_preflight_rejection,
    compose_episode_body,
    record_retention_sources,
)

#: Same logger object/name as the facade module: every record these steps emit must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


# ---------------------------------------------------------------------------
# Pipeline step 1 — try to reconcile an already-completed Graphiti result
# ---------------------------------------------------------------------------

async def try_reconcile_existing(ctx: EnrichmentContext) -> bool:
    """Check for an existing Graphiti completion and mark ready. Returns True if handled."""

    from menhir.services import enrichment_steps as _facade

    existing_completion = ctx.graph_adapter.find_completed_episode_artifact(
        anchor_uuid=ctx.episode_uuid,
        anchor_name=str(ctx.claimed.get("name") or ctx.episode_uuid),
    )
    if existing_completion is not None:
        resolved_episode_uuid = str(existing_completion.get("resolved_episode_uuid") or "")
        entity_uuids = [str(uuid) for uuid in (existing_completion.get("entity_uuids") or []) if str(uuid)]
        edge_uuids = [str(uuid) for uuid in (existing_completion.get("edge_uuids") or []) if str(uuid)]
        stamp_kwargs: dict[str, object] = {}
        if ctx.claimed.get("bootstrap_scope") is not None:
            stamp_kwargs["bootstrap_scope"] = ctx.claimed.get("bootstrap_scope")
        stamped = ctx.graph_adapter.stamp_ingest_metadata(
            node_uuids=[resolved_episode_uuid] + entity_uuids,
            edge_uuids=edge_uuids,
            session_id=str(ctx.claimed.get("session_id") or ""),
            user_id=str(ctx.claimed.get("user_id") or ""),
            source=str(ctx.claimed.get("source") or "claude-code"),
            source_confidence=source_confidence_for(str(ctx.claimed.get("source") or "claude-code")),
            namespace=str(ctx.claimed.get("namespace") or "default"),
            **stamp_kwargs,
        )
        record_retention_sources(
            ctx.graph_adapter,
            entity_uuids,
            source_episode_uuid=ctx.episode_uuid,
            namespace=str(ctx.claimed.get("namespace") or "default"),
        )
        marked_ready = ctx.graph_adapter.mark_episode_ready(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            resolved_episode_uuid=resolved_episode_uuid,
            nodes_touched=stamped.nodes_touched,
            edges_touched=stamped.edges_touched,
        )
        if not marked_ready:
            logger.info(
                "Skipping completion reconciliation after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return True
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="episode_ready",
            state="completed",
            episode_uuid=ctx.episode_uuid,
            details={
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": stamped.nodes_touched,
                "edges_touched": stamped.edges_touched,
                "reconciled_existing_completion": True,
            },
        )
        duration_ms = int((perf_counter() - ctx.started) * 1000)
        _facade.record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={
                "episode_uuid": ctx.episode_uuid,
                "processing_attempts": ctx.processing_attempts,
                "queue_depth": ctx.get_queue_depth(),
            },
            result={
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": stamped.nodes_touched,
                "edges_touched": stamped.edges_touched,
                "reconciled_existing_completion": True,
            },
            duration_ms=duration_ms,
            success=True,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Pipeline step 2 — preflight rejection for oversized episodes
# ---------------------------------------------------------------------------

async def run_preflight_rejection(ctx: EnrichmentContext) -> bool:
    """Reject oversized episodes before Graphiti. Returns True if rejected."""

    from menhir.services import enrichment_steps as _facade

    preflight_rejection = build_episode_preflight_rejection(
        compose_episode_body(ctx.claimed),
        ctx.graphiti_episode_max_estimated_tokens,
    )
    if preflight_rejection is not None:
        # PART 2: Create raw-capture for terminal breakage (preflight oversize rejection).
        # This is best-effort — capture failure must not break the failure handling itself.
        try:
            episode_content = compose_episode_body(ctx.claimed)
            if episode_content.strip():
                # Short name: first ~60 chars of content
                capture_name = episode_content[:60].replace("\n", " ").strip()
                ctx.graph_adapter.create_raw_capture_entity(
                    episode_uuid=ctx.episode_uuid,
                    name=capture_name,
                    content=episode_content,
                    namespace=str(ctx.claimed.get("namespace") or "default"),
                    session_id=str(ctx.claimed.get("session_id") or ""),
                    user_id=str(ctx.claimed.get("user_id") or ""),
                    source=str(ctx.claimed.get("source") or "claude-code"),
                )
        except Exception as e:
            logger.debug(
                "Failed to create raw-capture for oversized episode %s: %s",
                ctx.episode_uuid,
                e,
            )

        failed = ctx.graph_adapter.mark_episode_failed(
            ctx.episode_uuid,
            str(preflight_rejection["error"]),
            worker_id=ctx.worker_id,
        )
        if not failed:
            logger.info(
                "Skipping oversized preflight failure write after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return True
        duration_ms = int((perf_counter() - ctx.started) * 1000)
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
            error=str(preflight_rejection["error"]),
        )
        _facade.record_failure_event(
            operation="episode_enrichment",
            episode_uuid=ctx.episode_uuid,
            failure_stage="graphiti_preflight_rejected",
            classification="terminal",
            retryable=False,
            processing_attempt=ctx.processing_attempts,
            queue_depth=ctx.get_queue_depth(),
            worker_id=ctx.worker_id,
            error_type=str(preflight_rejection["code"]),
            error=str(preflight_rejection["error"]),
            details={
                "source": ctx.claimed.get("source"),
                "session_id": ctx.claimed.get("session_id"),
                "user_id": ctx.claimed.get("user_id"),
                "duration_ms": duration_ms,
                "estimated_tokens": preflight_rejection["estimated_tokens"],
                "limit": preflight_rejection["limit"],
                "char_count": preflight_rejection["char_count"],
            },
        )
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="episode_preflight_rejected",
            state="failed",
            episode_uuid=ctx.episode_uuid,
            details={
                "estimated_tokens": preflight_rejection["estimated_tokens"],
                "limit": preflight_rejection["limit"],
                "char_count": preflight_rejection["char_count"],
            },
        )
        logger.warning(
            "Rejected oversized episode before Graphiti extraction episode_id=%s estimated_tokens=%s limit=%s",
            ctx.episode_uuid,
            preflight_rejection["estimated_tokens"],
            preflight_rejection["limit"],
        )
        return True
    return False
