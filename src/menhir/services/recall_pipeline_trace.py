"""Observe-only trace assembly for recall: shadow passes and view reachability."""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from menhir.domain.recall import QueryPreset, ScoredMemory
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.domain.retrieval_trace_models import (
    FacetShadowTrace,
    RetrievalTrace,
    ScoringTrace,
    ViewReachability,
)
from menhir.infrastructure.telemetry import record_mcp_event

logger = logging.getLogger(__name__)


async def _build_retrieval_trace(
    service: Any,
    query: str,
    preset: QueryPreset,
    tuning: RetrievalTuningConfig,
    namespace: str | None,
    candidate_inputs: list[dict[str, object]],
    metadata_by_uuid: dict[str, dict[str, object]],
    top_results: list[ScoredMemory],
    file_context_project: str | None,
    scoring_trace: ScoringTrace | None,
    _t_phases: dict[str, int],
    facet_active_trace: FacetShadowTrace | None,
    rank_shadow_warning: str | None,
) -> RetrievalTrace | None:
    """Build the RetrievalTrace; returns None when no scoring trace was requested."""
    if scoring_trace is None:
        return None
    # Shadow oracle/warden pass (observe-only): runs only when a trace is
    # requested and the flag is on, never changes `top_results`, and must never
    # break recall — any failure is logged and recorded, not propagated.
    assertion_shadow = None
    if tuning.enable_assertion_shadow and candidate_inputs:
        _t = perf_counter()
        try:
            assertion_shadow = await service._run_assertion_shadow(
                query, namespace, candidate_inputs, metadata_by_uuid,
                query_project=file_context_project,
                tuning=tuning,
            )
            record_mcp_event(
                kind="background",
                operation="assertion_shadow",
                payload={"query": query[:60], "candidates": len(candidate_inputs)},
                result={
                    "admitted": assertion_shadow.admitted,
                    "flagged": assertion_shadow.flagged,
                    "refused": assertion_shadow.refused,
                    "intent": assertion_shadow.intent,
                },
                duration_ms=int((perf_counter() - _t) * 1000),
                success=True,
            )
        except Exception:  # observe-only: never break recall
            logger.exception("Assertion shadow pass failed query=%r", query[:60])
            record_mcp_event(
                kind="background",
                operation="assertion_shadow",
                payload={"query": query[:60], "candidates": len(candidate_inputs)},
                result={"error": "assertion_shadow_failed"},
                duration_ms=int((perf_counter() - _t) * 1000),
                success=False,
            )
    # FACET candidate-generation shadow (observe-only, default-off): what would
    # CandidateSource.FACET contribute over this pool? Never changes top_results;
    # any failure is logged + recorded, not propagated.
    facet_shadow = None
    if tuning.enable_facet_shadow and candidate_inputs:
        _t = perf_counter()
        try:
            facet_shadow = await service._run_facet_pass(
                query, namespace,
                [str(c["uuid"]) for c in candidate_inputs],
                query_project=file_context_project,
            )
            record_mcp_event(
                kind="background",
                operation="facet_shadow",
                payload={"query": query[:60], "pool": facet_shadow.pool},
                result={"candidates": facet_shadow.candidates},
                duration_ms=int((perf_counter() - _t) * 1000),
                success=True,
            )
        except Exception:  # observe-only: never break recall
            logger.exception("Facet shadow pass failed query=%r", query[:60])
            record_mcp_event(
                kind="background",
                operation="facet_shadow",
                payload={"query": query[:60], "candidates": len(candidate_inputs)},
                result={"error": "facet_shadow_failed"},
                duration_ms=int((perf_counter() - _t) * 1000),
                success=False,
            )
    # D0 view-reachability (trace-only, deterministic): where did the first
    # current View land in the shipped results? A View is query-sufficient
    # state, so its rank IS the delivered retrieval entropy for its query
    # class — no labels, no LLM. view_kind/view_current come from the
    # metadata already fetched; a superseded version never counts.
    from menhir.services.view_entropy import estimate_footprint_tokens

    view_reachability = None
    _walk_tokens = 0
    for _rank, _sm in enumerate(top_results, start=1):
        _walk_tokens += estimate_footprint_tokens(_sm.content or _sm.name)
        _meta = metadata_by_uuid.get(_sm.uuid) or {}
        if _meta.get("view_kind") and _meta.get("view_current") is not False:
            view_reachability = ViewReachability(
                uuid=_sm.uuid,
                view_kind=str(_meta["view_kind"]),
                rank=_rank,
                tokens_to_view=_walk_tokens,
            )
            # Emit telemetry for view-reachability outcomes (best-effort)
            try:
                record_mcp_event(
                    kind="background",
                    operation="view_reachability",
                    payload={
                        "query": query[:60],
                        "namespace": namespace,
                        "view_kind": str(_meta["view_kind"]),
                        "rank": _rank,
                        "tokens_to_view": _walk_tokens,
                        "result_count": len(top_results),
                    },
                    success=True,
                )
            except Exception:  # telemetry: never break retrieval
                logger.exception("view_reachability event recording failed")
            break
    # Emit view_absent event when no current View surfaced
    if view_reachability is None:
        try:
            record_mcp_event(
                kind="background",
                operation="view_reachability",
                payload={
                    "query": query[:60],
                    "namespace": namespace,
                    "result_count": len(top_results),
                },
                result={"view_absent": True},
                success=True,
            )
        except Exception:  # telemetry: never break retrieval
            logger.exception("view_absent event recording failed")
    retrieval_trace = RetrievalTrace(
        query=query,
        preset=preset.value,
        total_ms=_t_phases["total"],
        phases=dict(_t_phases),
        candidates=list(scoring_trace.candidates),
        assertion_shadow=assertion_shadow,
        view_reachability=view_reachability,
        facet_shadow=facet_shadow,
        facet_active=facet_active_trace,
        rank_shadow_warning=rank_shadow_warning,
    )
    return retrieval_trace
