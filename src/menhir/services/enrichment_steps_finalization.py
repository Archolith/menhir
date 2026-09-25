"""Enrichment pipeline step 4: stamp metadata, rehydrate, and finalize.

Extracted verbatim from ``enrichment_steps.py``; the facade module re-exports everything
here so existing ``menhir.services.enrichment_steps`` import sites keep working unchanged.
Telemetry/lease helpers are resolved through the facade module at call time so
monkeypatching ``menhir.services.enrichment_steps.<name>`` keeps affecting this step,
exactly as when everything lived in one module.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from menhir.domain.models import FreshnessState
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.graphiti_patches import (
    clear_extraction_receipt,
    get_extraction_receipt,
    is_policy_empty_extraction,
)
from menhir.infrastructure.telemetry import record_memory_revision

from menhir.services.enrichment_steps_context import CombinedExtractionCollapsedError, EnrichmentContext
from menhir.services.enrichment_steps_helpers import compose_episode_body, record_retention_sources

#: Same logger object/name as the facade module: every record this step emits must keep
#: the ``menhir.services.enrichment_steps`` logger name (caplog filters key off it).
logger = logging.getLogger("menhir.services.enrichment_steps")


# ---------------------------------------------------------------------------
# Pipeline step 4 — stamp metadata, rehydrate, and finalize
# ---------------------------------------------------------------------------

async def stamp_and_finalize(
    ctx: EnrichmentContext,
    graphiti_result: Any,
) -> None:
    """Stamp metadata, rehydrate compressed nodes, and mark episode ready."""

    from menhir.services import enrichment_steps as _facade

    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="stamping",
        substage="graphiti_response_received",
        progress=55.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=2,
        clear_llm_active=True,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )

    resolved_episode_uuid = graphiti_result.episode.uuid
    extracted_nodes = graphiti_result.nodes
    extracted_edges = graphiti_result.edges
    extracted_episodic_edges = graphiti_result.episodic_edges

    # Consume the combined-extraction receipt exactly once (populated inside Graphiti's
    # child task by the sanitation validator). It lets us tell a legitimate empty
    # extraction apart from a collapse where the LLM extracted content that resolution
    # then dropped. Read + clear here regardless of which branch we take below.
    receipt = get_extraction_receipt()
    clear_extraction_receipt()
    raw_extraction_nonempty = (
        receipt is not None
        and receipt.episode_key == ctx.episode_uuid
        and (receipt.raw_entity_count > 0 or receipt.raw_edge_count > 0)
    )

    if not _facade.still_owns_episode(ctx.graph_adapter, ctx.episode_uuid, ctx.worker_id):
        logger.info(
            "Skipping enrichment completion after ownership lost episode_id=%s worker=%s",
            ctx.episode_uuid,
            ctx.worker_id,
        )
        return

    if not extracted_nodes and not extracted_edges:
        if raw_extraction_nonempty and is_policy_empty_extraction(receipt):
            # Policy-empty: the extraction produced entities but no persistable content.
            # Cases: (a) assistant turn with only self-label and no relationship, (b) every
            # usable edge was user->X echo suppressed by design, (c) any-source turn where
            # BOTH the initial and the repair extraction produced only self-labels with zero
            # edges (e.g. evidence projection of "Thanks again for your help!").
            logger.info(
                "Policy-empty enrichment (success) episode_id=%s "
                "(raw_edges=%d suppressed=%d self_only_relationless=%s "
                "initial_self_only=%s repair_self_only=%s repair_attempted=%s "
                "context_unsupported_edges_suppressed=%d)",
                ctx.episode_uuid,
                receipt.raw_edge_count,
                receipt.self_echo_edges_suppressed,
                receipt.assistant_self_only_relationless,
                receipt.initial_self_only_entities,
                receipt.repair_self_only_entities,
                receipt.relationless_repair_attempted,
                receipt.context_unsupported_edges_suppressed,
            )
        elif raw_extraction_nonempty:
            # Collapse: the LLM DID return entities/edges but resolution persisted nothing.
            # This is a linkage/provenance failure, not an empty episode — surface it as an
            # explicit retryable failure instead of masking it as "zero-extraction success"
            # (which would permanently lose the content). Enters the standard failure path.
            #
            # `relationless` names the sub-case where the model returned entities and NO edge at all
            # even after the combined extractor's one bounded corrective pass. The raw shape alone
            # does NOT prove the source text stated no relation: gpt-4o-mini repeatedly returned
            # `new app` with no edge for "I'm actually using a new app I recently downloaded."
            # The wrapper now repairs that under-extraction once with a focused prompt. If it still
            # cannot produce a grounded relationship, the episode remains a visible non-retryable
            # failure rather than burning the scheduler retry budget or silently losing content.
            # A titled list no longer reaches here: sanitation emits the membership its syntax states.
            relationless = receipt.raw_entity_count > 0 and receipt.raw_edge_count == 0
            raise CombinedExtractionCollapsedError(
                ("relationless_extraction " if relationless else "combined_extraction_collapsed ")
                + f"episode_id={ctx.episode_uuid} "
                f"retryable={'false' if relationless else 'true'} "
                f"raw_entities={receipt.raw_entity_count} "
                f"raw_edges={receipt.raw_edge_count} "
                f"list_membership_edges_added={receipt.list_membership_edges_added} "
                f"malformed_entities_dropped={receipt.malformed_entities_dropped} "
                f"malformed_edges_dropped={receipt.malformed_edges_dropped} "
                f"endpoints_synthesized={receipt.endpoints_synthesized} "
                f"orphan_nodes_dropped={receipt.orphan_nodes_dropped} "
                f"relationless_repair_attempted={str(receipt.relationless_repair_attempted).lower()} "
                f"relationless_repair_succeeded={str(receipt.relationless_repair_succeeded).lower()} "
                f"assistant_self_only_relationless="
                f"{str(receipt.assistant_self_only_relationless).lower()} "
                f"initial_self_only_entities="
                f"{str(receipt.initial_self_only_entities).lower()} "
                f"repair_self_only_entities="
                f"{str(receipt.repair_self_only_entities).lower()} "
                f"context_unsupported_edges_suppressed="
                f"{receipt.context_unsupported_edges_suppressed} "
                "resolved_nodes=0 resolved_edges=0"
            )
        # PART 1: Zero-extraction is a successful empty determination, not a failure.
        # The episode may have had no memorable content (e.g., an "ok thanks" response),
        # which is a valid outcome, not a breakage condition.
        logger.info(
            "Zero-extraction enrichment (success) episode_id=%s — Graphiti returned no nodes or edges",
            ctx.episode_uuid,
        )
        marked_ready = ctx.graph_adapter.mark_episode_ready(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            resolved_episode_uuid=resolved_episode_uuid,
            nodes_touched=0,
            edges_touched=0,
        )
        if not marked_ready:
            logger.info(
                "Skipping zero-extraction ready write after ownership lost episode_id=%s worker=%s",
                ctx.episode_uuid,
                ctx.worker_id,
            )
            return
        duration_ms = int((perf_counter() - ctx.started) * 1000)
        # Record as successful empty extraction with a reason receipt
        _facade.record_mcp_event(
            kind="background",
            operation="episode_enrichment",
            payload={
                "episode_uuid": ctx.episode_uuid,
                "processing_attempts": ctx.processing_attempts,
                "queue_depth": ctx.get_queue_depth(),
            },
            result={"empty_extraction": True},
            duration_ms=duration_ms,
            success=True,
        )
        # DO NOT emit record_failure_event — zero-extraction is not a failure
        # The _failed_enrichments counter is untouched (only incremented by caller on exceptions)
        _facade.record_lifecycle_event(
            component="ingest_worker",
            event="episode_empty",
            state="completed",
            episode_uuid=ctx.episode_uuid,
            details={"reason": "empty_extraction"},
        )
        return

    node_uuids = [resolved_episode_uuid] + [node.uuid for node in extracted_nodes]
    edge_uuids = [edge.uuid for edge in extracted_edges] + [
        edge.uuid for edge in extracted_episodic_edges
    ]
    stamp_kwargs: dict[str, object] = {}
    if ctx.claimed.get("bootstrap_scope") is not None:
        stamp_kwargs["bootstrap_scope"] = ctx.claimed.get("bootstrap_scope")
    stamped = ctx.graph_adapter.stamp_ingest_metadata(
        node_uuids=node_uuids,
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
        [node.uuid for node in extracted_nodes],
        source_episode_uuid=ctx.episode_uuid,
        namespace=str(ctx.claimed.get("namespace") or "default"),
    )
    # M6 Phase 5: Record scope assignment for extracted entity nodes
    if ctx.settings_record_revisions:
        for node in extracted_nodes:
            record_memory_revision(
                node_uuid=node.uuid,
                field="scope",
                old_value=None,
                new_value="SESSION",
                changed_by="ingest",
                episode_uuid=ctx.episode_uuid,
            )
    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="post_process",
        substage="metadata_stamped",
        progress=75.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=3,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
        )
    # Best-effort LLM repair of synthetic edge facts
    await _facade._repair_synthetic_edge_facts(
        ctx, extracted_edges, compose_episode_body(ctx.claimed),
    )
    # Best-effort structural anchoring
    _facade._anchor_to_structural_entities(
        ctx, [node.uuid for node in extracted_nodes], compose_episode_body(ctx.claimed),
    )
    # Best-effort semantic correlation check (Step 8)
    # After nodes are committed, check if they correlate with existing entities.
    # Correlations create RELATES_TO edges (0.7–0.85 sim) or trigger merge (>0.95 sim).
    # Conflict-range pairs (0.85–0.95) are returned for the lifecycle service to flag.
    correlation_conflicts = 0
    try:
        from menhir.services.correlation_service import CorrelationService
        correlation_service = CorrelationService(
            correlation_repo=ctx.graph_adapter._correlation,
            graphiti_client=ctx.graphiti_client,
            llm=ctx.llm,  # Part 2: Pass LLM for judge-gated merge
        )
        episode_body = compose_episode_body(ctx.claimed)
        # Part 1: namespace-scoped correlation search (deterministic veto gate)
        namespace = str(ctx.claimed.get("namespace") or "default")
        for node in extracted_nodes:
            # Part 5: kill the episode-body fallback — skip correlation for unnamed nodes
            node_name = str(getattr(node, "name", "") or "").strip()
            node_content = str(getattr(node, "content", "") or "").strip()
            if not node_name and not node_content:
                # Node has no identity claim — skip correlation
                continue
            node_query = node_name or node_content
            corr_result = await correlation_service.check_correlation(
                node.uuid, node_query, namespace=namespace,
            )
            correlation_conflicts += corr_result.conflicts
            if corr_result.related > 0 or corr_result.merged > 0:
                logger.info(
                    "Correlation check: node=%s related=%d merged=%d conflicts=%d",
                    node.uuid, corr_result.related, corr_result.merged, corr_result.conflicts,
                )
    except Exception:
        logger.warning("Correlation check failed (best-effort)", exc_info=True)
    if ctx.lifecycle_service is not None:
        entity_uuids = [node.uuid for node in extracted_nodes]
        freshness_map = ctx.graph_adapter.fetch_node_freshness(entity_uuids)
        episode_content = compose_episode_body(ctx.claimed)
        stamped_ok = ctx.graph_adapter.update_episode_processing(
            ctx.episode_uuid,
            worker_id=ctx.worker_id,
            stage="rehydrating",
            substage="checking_compressed_nodes",
            progress=85.0,
            steps_total=ctx.processing_steps_total,
            steps_completed=4,
        )
        if not stamped_ok:
            # CF-233: the stamp did not apply. The sibling terminal writes
            # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
            # every caller checks it; these five discarded it, so a worker whose lease had
            # already gone kept running the pipeline -- LLM calls included -- until the
            # terminal write finally refused.
            #
            # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
            # it for lost ownership, for a missing node, AND for a call with no fields to
            # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
            logger.warning(
                "Episode progress stamp did not apply episode_id=%s worker=%s; "
                "the episode is no longer owned by this worker or no longer exists",
                ctx.episode_uuid,
                ctx.worker_id,
            )
        for node_uuid, freshness in freshness_map.items():
            if freshness != FreshnessState.COMPRESSED:
                continue
            rehydrate_started = perf_counter()
            rehydrated = await ctx.lifecycle_service.rehydrate_node(
                node_uuid,
                new_context=episode_content,
                source_node_uuid=node_uuid,
                source_episode_uuid=resolved_episode_uuid,
            )
            rehydrate_duration_ms = int((perf_counter() - rehydrate_started) * 1000)
            _facade.record_mcp_event(
                kind="background",
                operation="rehydration",
                payload={
                    "node_uuid": node_uuid,
                    "source_node_uuid": node_uuid,
                    "source_episode_uuid": resolved_episode_uuid,
                    "trigger": "ingestion",
                },
                result={"rehydrated": rehydrated},
                duration_ms=rehydrate_duration_ms,
                success=rehydrated,
            )
    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="finalizing",
        substage="marking_ready",
        progress=95.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=5,
        clear_llm_active=True,
    )
    if not stamped_ok:
        # CF-233: the stamp did not apply. The sibling terminal writes
        # (mark_episode_ready/mark_episode_failed) have the identical bool contract and
        # every caller checks it; these five discarded it, so a worker whose lease had
        # already gone kept running the pipeline -- LLM calls included -- until the
        # terminal write finally refused.
        #
        # Reported, NOT acted on, because False is ambiguous: episode_stamping returns
        # it for lost ownership, for a missing node, AND for a call with no fields to
        # set. Treating it as proof of ownership loss would repeat CF-205 exactly.
        logger.warning(
            "Episode progress stamp did not apply episode_id=%s worker=%s; "
            "the episode is no longer owned by this worker or no longer exists",
            ctx.episode_uuid,
            ctx.worker_id,
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
            "Skipping ready finalization after ownership lost episode_id=%s worker=%s",
            ctx.episode_uuid,
            ctx.worker_id,
        )
        return
    _facade.record_lifecycle_event(
        component="ingest_worker",
        event="episode_ready",
        state="completed",
        episode_uuid=ctx.episode_uuid,
        details={
            "resolved_episode_uuid": resolved_episode_uuid,
            "nodes_touched": stamped.nodes_touched,
            "edges_touched": stamped.edges_touched,
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
        },
        duration_ms=duration_ms,
        success=True,
    )
    if duration_ms > ctx.ready_warning_ms:
        logger.warning(
            "Slow background enrichment episode_id=%s duration_ms=%s queue_depth=%s",
            ctx.episode_uuid,
            duration_ms,
            ctx.get_queue_depth(),
        )
