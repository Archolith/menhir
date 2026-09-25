"""Shadow-trace assembly and flattening for Stage 1 shadow composition.

Extracted from shadow_context_composition.py (the Stage 1 facade, which re-exports
both functions): combining the frozen prediction with the real extraction outcome,
and flattening the trace for record_lifecycle_event.
"""

from __future__ import annotations

import logging

from menhir.services.shadow_context_composition_models import (
    ExtractionCompositionShadowTrace,
    ShadowCompositionPrediction,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Combine prediction + real extraction outcome into the final frozen trace.
# ---------------------------------------------------------------------------

def build_shadow_trace(
    prediction: ShadowCompositionPrediction,
    graphiti_result: object,
    *,
    shadow_total_ms: int,
) -> ExtractionCompositionShadowTrace:
    """One-shot construction combining the already-frozen prediction with the real
    extraction outcome. Never mutates prediction; never called more than once per
    episode."""
    node_names: tuple[str, ...] = ()
    fact_texts: tuple[str, ...] = ()
    try:
        nodes = getattr(graphiti_result, "nodes", None) or []
        node_names = tuple(str(getattr(n, "name", "") or "") for n in nodes if getattr(n, "name", None))
        edges = getattr(graphiti_result, "edges", None) or []
        fact_texts = tuple(str(getattr(e, "fact", "") or "") for e in edges if getattr(e, "fact", None))
    except Exception:
        logger.debug("Failed to read production extraction result for shadow trace", exc_info=True)

    return ExtractionCompositionShadowTrace(
        prediction=prediction,
        production_extracted_node_names=node_names,
        production_extracted_facts=fact_texts,
        shadow_total_ms=shadow_total_ms,
    )


def shadow_trace_to_details(trace: ExtractionCompositionShadowTrace) -> dict[str, object]:
    """Flatten the trace into a JSON-serializable dict for record_lifecycle_event's
    details_json column (dataclasses.asdict handles nested frozen dataclasses and
    tuples/frozensets need explicit list conversion for JSON)."""
    p = trace.prediction
    return {
        "episode_uuid": p.episode_uuid,
        "namespace": p.namespace,
        "status": p.status,
        "failure_stage": p.failure_stage,
        "error": p.error,
        "candidates": [
            {
                "fact_uuid": c.fact_uuid, "fact_text": c.fact_text,
                "source_uuid": c.source_uuid, "source_name": c.source_name,
                "target_uuid": c.target_uuid, "target_name": c.target_name,
                "valid_at": c.valid_at, "invalid_at": c.invalid_at,
                "created_at": c.created_at, "expired_at": c.expired_at,
                "retrieval_sources": sorted(c.retrieval_sources),
                "semantic_score": c.semantic_score,
            }
            for c in p.candidates
        ],
        "message_hypotheses": [
            {"shadow_facet": h.shadow_facet, "shadow_state_family": h.shadow_state_family, "confidence": h.confidence}
            for h in p.message_hypotheses
        ],
        "candidate_labels": [
            {
                "fact_uuid": lb.fact_uuid, "shadow_facet": lb.shadow_facet,
                "shadow_state_family": lb.shadow_state_family, "shadow_scope": lb.shadow_scope,
            }
            for lb in p.candidate_labels
        ],
        "production_facets": list(p.production_facets),
        "rejected": [{"fact_uuid": r.fact_uuid, "reason": r.reason} for r in p.rejected],
        "selected_fact_uuid": p.selected_fact_uuid,
        "abstention_reason": p.abstention_reason,
        "llm_tie_breaker_fired": p.llm_tie_breaker_fired,
        "temporal_query": p.temporal_query,
        "temporal_pivot": p.temporal_pivot,
        "candidate_retrieval_ms": p.candidate_retrieval_ms,
        "metadata_generation_ms": p.metadata_generation_ms,
        "selection_ms": p.selection_ms,
        "note": p.note,
        "production_extracted_node_names": list(trace.production_extracted_node_names),
        "production_extracted_facts": list(trace.production_extracted_facts),
        "shadow_total_ms": trace.shadow_total_ms,
    }
