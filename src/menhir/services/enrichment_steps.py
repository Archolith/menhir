"""Extracted enrichment pipeline step functions.

Each function implements one stage of the background enrichment pipeline.
They accept an ``EnrichmentContext`` dataclass (or explicit parameters for
dual-path helpers) so they are independently testable outside of IngestService.

This module is the facade for the ``enrichment_steps_*`` sibling modules: the context
object, pure helpers, and the reconcile/shadow/postprocess/finalization/failure/dispatch
units live beside it and are re-exported below, so every existing import site of
``menhir.services.enrichment_steps`` keeps working unchanged. ``run_graphiti_extraction``
and ``_repair_synthetic_edge_facts`` remain defined here on purpose: the self-identity
producer census (tests/test_self_identity_producer_census.py) and the CF-78 source
assertions (tests/test_high_wave9_budgets_startup.py) pin them to this file.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from menhir.services.scheduler_protocols import LifecycleServiceProtocol

from menhir.domain.models import FreshnessState, ProcessingState
from menhir.domain.self_identity import (
    SelfEvidenceKind,
    SelfIdentityContext,
    SelfSubjectEndpointEnvelope,
    normalize_logical_namespace,
    self_context_for_pending_episode,
    self_subject_endpoint_for_claim,
)
from menhir.infrastructure.self_binding import (
    InvalidSelfSubjectDeclarationError,
    SelfBindMode,
    resolve_bind_mode,
)
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure import GraphitiClient, MemoryGraphAdapter
from menhir.infrastructure.evidence_publication_intents import (
    EvidencePublicationIntentRepository,
    PublicationDispatchSuppressed,
)
from menhir.infrastructure.telemetry import (
    record_failure_event,
    record_lifecycle_event,
    record_mcp_event,
    record_memory_revision,
)
from menhir.infrastructure.graphiti_helpers import SYNTHETIC_FACT_PREFIX, strip_synthetic_prefix
from menhir.infrastructure.graphiti_patches import (
    begin_extraction_receipt,
    is_policy_empty_extraction,
    clear_extraction_receipt,
    get_extraction_receipt,
)
from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_graphiti_output_parse_error,
)
from menhir.services.ingest_gate import IngestGate
from menhir.services.ingest_limits import MAX_DIFF_CHARS
from menhir.services.shadow_context_composition import (
    build_shadow_trace,
    run_shadow_composition_with_timeout,
    shadow_trace_to_details,
    snapshot_candidate_facts,
)

# Facade re-exports: moved units stay importable from this module path.
from menhir.services.enrichment_steps_context import CombinedExtractionCollapsedError, EnrichmentContext
from menhir.services.enrichment_steps_dispatch import add_episode_with_timeout
from menhir.services.enrichment_steps_failure import handle_enrichment_failure
from menhir.services.enrichment_steps_finalization import stamp_and_finalize
from menhir.services.enrichment_steps_helpers import (
    build_episode_preflight_rejection,
    coerce_reference_time,
    compose_episode_body,
    estimate_episode_tokens,
    failure_details_from_exception,
    record_retention_sources,
    still_owns_episode,
)
from menhir.services.enrichment_steps_postprocess import (
    _CONTROL_CHARS_RE,
    _MAX_REPAIRED_FACT_CHARS,
    _anchor_to_structural_entities,
    _is_admissible_repaired_fact,
)
from menhir.services.enrichment_steps_reconcile import run_preflight_rejection, try_reconcile_existing
from menhir.services.enrichment_steps_shadow import _dispatch_shadow_composition, _run_shadow_composition_and_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pipeline step 3 — Graphiti extraction (under ingest lock)
# ---------------------------------------------------------------------------

async def run_graphiti_extraction(
    ctx: EnrichmentContext,
    *,
    finalize_under_gate: bool,
) -> Any:
    """Acquire the ingest gate and run the Graphiti add_episode pipeline.

    The gate bounds total concurrent extractions and serializes per ``group_id``
    (namespace), so episodes in different namespaces extract in parallel while
    same-namespace episodes never race on entity dedup. Production workers set
    ``finalize_under_gate`` so correlation and graph-mutating finalization remain
    in that same namespace-critical section. Extraction-only tests opt out
    explicitly so a production caller cannot accidentally skip the wider gate.
    """

    from menhir.domain.namespace import namespace_to_group_id

    namespace = str(ctx.claimed.get("namespace") or "default")
    group_id = namespace_to_group_id(namespace if namespace != "default" else None)

    self_bind_mode = resolve_bind_mode(
        getattr(ctx, "canonical_self_binding_mode", None)
    )
    # Validate before any shadow candidate search, publication intent, or native dispatch.
    # None means genuinely ineligible. A collision in an eligible projection raises and is
    # parked by the worker's existing failure path; it must not disable self-fork prevention.
    # The endpoint changes behavior, so off/observe still do not construct one.
    self_subject_endpoint = (
        self_subject_endpoint_for_claim(ctx.claimed)
        if self_bind_mode is SelfBindMode.ENFORCE
        else None
    )

    stamped_ok = ctx.graph_adapter.update_episode_processing(
        ctx.episode_uuid,
        worker_id=ctx.worker_id,
        stage="graphiti_extracting",
        substage="awaiting_graphiti_response",
        progress=20.0,
        steps_total=ctx.processing_steps_total,
        steps_completed=1,
        llm_active_task="memory: graphiti add_episode",
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
    record_lifecycle_event(
        component="ingest_worker",
        event="graphiti_extracting",
        state="started",
        episode_uuid=ctx.episode_uuid,
    )
    async with ctx.ingest_gate.acquire(group_id):
        logger.info("Episode enrichment acquired ingest gate episode_id=%s group_id=%s", ctx.episode_uuid, group_id)
        record_lifecycle_event(
            component="ingest_worker",
            event="ingest_lock",
            state="acquired",
            episode_uuid=ctx.episode_uuid,
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="before_add_episode_timeout_wrapper",
            state="started",
            episode_uuid=ctx.episode_uuid,
            details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="dispatch_add_episode_timeout_wrapper",
            state="started",
            episode_uuid=ctx.episode_uuid,
            details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
        )

        # Stage 1 shadow-mode context composition (observe-only): candidate retrieval MUST
        # happen here, before the real extraction call, so this episode's own about-to-be-
        # created facts cannot leak into its own candidate pool. It is cheap and read-only,
        # so it's safe to run inside the gate — the expensive LLM work happens later, after
        # the gate is released (see the dispatch below). shadow_candidates/shadow_candidate_
        # error/shadow_retrieval_ms are all None/empty/0 when the flag is off (zero overhead).
        shadow_candidates: list = []
        shadow_candidate_error: str | None = None
        shadow_retrieval_ms = 0
        if ctx.shadow_context_composition:
            shadow_retrieval_started = perf_counter()
            try:
                shadow_namespace = str(ctx.claimed.get("namespace") or "default")
                shadow_candidates, shadow_candidate_error = await snapshot_candidate_facts(
                    ctx.graphiti_client, ctx.graph_adapter,
                    namespace=shadow_namespace, episode_body=compose_episode_body(ctx.claimed),
                )
            except Exception as exc:  # snapshot_candidate_facts is itself fail-safe; this is
                # a last-resort net (also covers the namespace/body prep above) so a shadow
                # bug can never block real extraction or leave the surrounding lifecycle
                # events (started at line ~528) without a matching outcome.
                logger.debug("Shadow candidate snapshot raised unexpectedly episode_id=%s", ctx.episode_uuid, exc_info=True)
                shadow_candidates, shadow_candidate_error = [], str(exc)
            shadow_retrieval_ms = int((perf_counter() - shadow_retrieval_started) * 1000)

        try:
            turn_evidence_uuid = str(
                ctx.claimed.get("turn_evidence_uuid") or ""
            ).strip()
            relationless_repair_context_loader: Callable[[], tuple[str, ...]] | None = None
            if turn_evidence_uuid:
                repair_namespace = normalize_logical_namespace(ctx.claimed.get("namespace"))

                def _load_relationless_repair_context() -> tuple[str, ...]:
                    # turn_evidence_uuid is caller-supplied. Scoping the read to THIS episode's
                    # namespace is what stops a foreign turn's text entering this extraction
                    # (CF-236); the admission gate governs trust tier, not this path.
                    rows = ctx.graph_adapter.load_preceding_turn_evidence_context(
                        turn_evidence_uuid,
                        namespace=repair_namespace,
                        limit=2,
                    )
                    return tuple(
                        f"{str(row.get('role') or '').strip().lower()}: "
                        f"{str(row.get('text') or '').strip()}"
                        for row in rows
                        if str(row.get("role") or "").strip()
                        and str(row.get("text") or "").strip()
                    )

                relationless_repair_context_loader = _load_relationless_repair_context

            episode_name = str(ctx.claimed.get("name") or ctx.episode_uuid)
            source_description = str(ctx.claimed.get("source") or "claude-code")
            reference_time = coerce_reference_time(
                ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")
            )
            publication_intent = None
            if ctx.evidence_publication_intents is not None:
                publication_intent = ctx.evidence_publication_intents.begin(
                    episode_uuid=ctx.episode_uuid,
                    namespace=namespace,
                    expected_name=episode_name,
                    source_description=source_description,
                    reference_time=reference_time,
                )
                if not publication_intent.dispatch_allowed:
                    raise PublicationDispatchSuppressed(
                        "Graphiti dispatch refused because publication intent "
                        f"{publication_intent.intent_key!r} is already "
                        f"{publication_intent.status}"
                    )

            self_identity = self_context_for_pending_episode(
                source=ctx.claimed.get("source"),
                namespace=namespace,
                episode_uuid=ctx.episode_uuid,
                turn_evidence_uuid=str(
                    ctx.claimed.get("turn_evidence_uuid") or ""
                ).strip() or None,
            )
            graphiti_result = await add_episode_with_timeout(
                ctx.graphiti_client,
                name=episode_name,
                episode_body=compose_episode_body(ctx.claimed),
                source_description=source_description,
                reference_time=reference_time,
                episode_uuid=ctx.episode_uuid,
                attempt=ctx.processing_attempts,
                timeout_s=ctx.graphiti_add_episode_timeout_s,
                group_id=group_id,
                relationless_repair_context_loader=relationless_repair_context_loader,
                self_identity=self_identity,
                self_subject_endpoint=self_subject_endpoint,
                self_bind_mode=self_bind_mode,
            )
            if publication_intent is not None:
                publication_transition = (
                    ctx.evidence_publication_intents.finalize_remote_outcome(
                        publication_intent,
                        remote_episode_uuid=str(graphiti_result.episode.uuid),
                    )
                )
                if not publication_transition.finalized:
                    logger.warning(
                        "Graphiti evidence quarantined episode_id=%s remote_episode_id=%s "
                        "reason=%s candidates=%s tombstones=%s",
                        ctx.episode_uuid,
                        graphiti_result.episode.uuid,
                        publication_transition.reason,
                        publication_transition.candidate_count,
                        publication_transition.tombstone_count,
                    )
        except Exception:  # re-raised; record telemetry for any graphiti failure
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="failed",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
            raise
        else:
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="completed",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
        finally:
            record_lifecycle_event(
                component="ingest_worker",
                event="dispatch_add_episode_timeout_wrapper",
                state="finally",
                episode_uuid=ctx.episode_uuid,
                details={"name": str(ctx.claimed.get("name") or ctx.episode_uuid)},
            )
        logger.info("Episode enrichment graphiti call returned episode_id=%s", ctx.episode_uuid)
        record_lifecycle_event(
            component="ingest_worker",
            event="before_add_episode_timeout_wrapper",
            state="completed",
            episode_uuid=ctx.episode_uuid,
        )
        record_lifecycle_event(
            component="ingest_worker",
            event="graphiti_add_episode",
            state="completed",
            episode_uuid=ctx.episode_uuid,
        )
        if finalize_under_gate:
            await stamp_and_finalize(ctx, graphiti_result)
    # Gate released above (the `async with` block ended). Shadow processing dispatches here,
    # AFTER release, specifically so it never adds to same-namespace queue latency. It's a
    # detached background task, not awaited — real episode completion has already finished
    # when ``finalize_under_gate`` is enabled and never waits on shadow latency.
    if ctx.shadow_context_composition:
        _dispatch_shadow_composition(
            ctx,
            candidates=shadow_candidates,
            candidate_error=shadow_candidate_error,
            candidate_retrieval_ms=shadow_retrieval_ms,
            graphiti_result=graphiti_result,
        )
    return graphiti_result


# ---------------------------------------------------------------------------
# Edge fact repair — best-effort LLM rewrite of synthetic facts
# ---------------------------------------------------------------------------

async def _repair_synthetic_edge_facts(
    ctx: EnrichmentContext,
    extracted_edges: list[Any],
    episode_content: str,
) -> None:
    """Best-effort LLM repair of edges with synthetic facts.

    Identifies edges whose ``fact`` starts with the synthetic prefix,
    calls the LLM to produce a proper fact, and writes the result back
    to Neo4j with provenance tracking.
    """
    if ctx.llm is None or not extracted_edges:
        return

    synthetic_edges: list[tuple[int, Any]] = []
    for i, edge in enumerate(extracted_edges):
        fact = getattr(edge, "fact", "") or ""
        if fact.startswith(SYNTHETIC_FACT_PREFIX):
            synthetic_edges.append((i, edge))

    if not synthetic_edges:
        # All facts are original — stamp provenance for all edges
        updates = []
        for e in extracted_edges:
            uuid = getattr(e, "uuid", None)
            fact = getattr(e, "fact", None)
            if uuid and fact:
                updates.append({"uuid": uuid, "fact": fact, "fact_source": "original"})
        if updates:
            ctx.graph_adapter.update_edge_facts(updates)
        return

    # Build stubs for LLM repair
    stubs: list[dict[str, str]] = []
    for _, edge in synthetic_edges:
        stubs.append({
            "source": getattr(edge, "name", "").split(" -> ")[0] if " -> " in getattr(edge, "name", "") else str(getattr(edge, "source_node_uuid", "")),
            "target": getattr(edge, "name", "").split(" -> ")[-1] if " -> " in getattr(edge, "name", "") else str(getattr(edge, "target_node_uuid", "")),
            "relation": getattr(edge, "name", "") or "related_to",
        })

    try:
        repaired = await ctx.llm.repair_edge_facts(episode_content, stubs)
    except Exception:
        logger.debug("Edge fact repair LLM call failed", exc_info=True)
        repaired = [None] * len(synthetic_edges)

    # Build bulk update list
    updates: list[dict[str, str]] = []
    synthetic_idx_set = {i for i, _ in synthetic_edges}
    for idx, edge in enumerate(extracted_edges):
        uuid = getattr(edge, "uuid", None)
        fact = getattr(edge, "fact", None)
        if uuid and fact and idx not in synthetic_idx_set:
            updates.append({"uuid": uuid, "fact": fact, "fact_source": "original"})

    for j, (_, edge) in enumerate(synthetic_edges):
        uuid = getattr(edge, "uuid", None)
        if not uuid:
            continue
        repaired_fact = repaired[j] if j < len(repaired) else None
        # CF-78: between the model returning a string and that string becoming a stored edge fact
        # there used to be exactly one check -- `if repaired_fact:`. The input to that model is
        # untrusted episode content, and the output is persisted into a graph recall renders
        # verbatim into an operator's agent context (CF-39). Truthiness is not validation.
        #
        # `fact_source` is what decides how hard to look. That field was written at five sites and
        # read at none, which made it a provenance marker that recorded a distinction nothing
        # honoured; branching on it here is the first consumer. An `original` fact came from the
        # extractor and keeps its existing treatment. An `llm_repaired` one is model prose about
        # attacker-influenced text and is held to the stricter bar.
        if repaired_fact and _is_admissible_repaired_fact(repaired_fact):
            updates.append({"uuid": uuid, "fact": repaired_fact, "fact_source": "llm_repaired"})
        else:
            updates.append({
                "uuid": uuid,
                "fact": strip_synthetic_prefix(getattr(edge, "fact", "") or ""),
                "fact_source": "synthetic_fallback",
            })

    if updates:
        ctx.graph_adapter.update_edge_facts(updates)

    repaired_count = sum(1 for j, _ in enumerate(synthetic_edges) if j < len(repaired) and repaired[j])
    logger.info(
        "Edge fact repair episode_id=%s: %d synthetic, %d repaired, %d fallback",
        ctx.episode_uuid,
        len(synthetic_edges),
        repaired_count,
        len(synthetic_edges) - repaired_count,
    )
