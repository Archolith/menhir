"""Data model for Stage 1 shadow-mode context composition.

Extracted from shadow_context_composition.py (the Stage 1 facade, which re-exports
everything here): the status vocabulary, the frozen dataclasses shared by the
composition pipeline, and the _empty_prediction() early-exit constructor.

.agent/plans/menhir-context-composition-production-integration.md lays out the 4-stage
rollout path from the Extraction Lab Phase 1-5 investigation
(.agent/plans/menhir-extraction-context-ablation-handoff.md) to production trust. All
dataclasses here are frozen; ShadowCompositionPrediction and
ExtractionCompositionShadowTrace are each constructed exactly once, with final data,
never mutated after the fact (see the plan's point 3 for why: a frozen dataclass that
claims to be filled in later is a contradiction, not a design).
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.temporal import TemporalQuery

_STATUS_SELECTED = "selected"
_STATUS_ABSTAINED_NO_CANDIDATES = "abstained_no_candidates"
_STATUS_ABSTAINED_NO_ELIGIBLE = "abstained_no_eligible_candidates"
_STATUS_ABSTAINED_TIE = "abstained_tie"
_STATUS_CANDIDATE_QUERY_FAILED = "candidate_query_failed"
_STATUS_METADATA_GENERATION_FAILED = "metadata_generation_failed"
_STATUS_MALFORMED_LLM_RESPONSE = "malformed_llm_response"
_STATUS_TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class ShadowCandidateFact:
    """One real fact-edge from the graph, at edge (not entity) granularity."""

    fact_uuid: str
    fact_text: str
    source_uuid: str
    source_name: str
    target_uuid: str
    target_name: str
    valid_at: str | None
    invalid_at: str | None
    created_at: str | None
    expired_at: str | None
    retrieval_sources: frozenset[str]  # {"literal_name", "semantic_search"}
    semantic_score: float | None = None


@dataclass(frozen=True)
class ShadowRankedHypothesis:
    """Message-side: what the episode itself implies, up to 2 ranked guesses."""

    shadow_facet: str
    shadow_state_family: str
    confidence: float


@dataclass(frozen=True)
class ShadowCandidateLabels:
    """Candidate-side: labels grounded in ONE real candidate fact's own text."""

    fact_uuid: str
    shadow_facet: str | None
    shadow_state_family: str | None
    shadow_scope: str | None


@dataclass(frozen=True)
class ShadowRejection:
    fact_uuid: str
    reason: str


@dataclass(frozen=True)
class ShadowCompositionPrediction:
    """Everything knowable BEFORE the real extraction call returns. Complete and
    immutable at construction. status is always one of the _STATUS_* constants above;
    when status != "selected"/"abstained_*", failure_stage/error are populated instead
    of candidates/hypotheses (which may be empty but are never fabricated)."""

    episode_uuid: str
    namespace: str
    status: str
    failure_stage: str | None
    error: str | None
    candidates: tuple[ShadowCandidateFact, ...]
    message_hypotheses: tuple[ShadowRankedHypothesis, ...]
    candidate_labels: tuple[ShadowCandidateLabels, ...]
    production_facets: tuple[str, ...]
    rejected: tuple[ShadowRejection, ...]
    selected_fact_uuid: str | None
    abstention_reason: str | None
    llm_tie_breaker_fired: bool
    temporal_query: str = TemporalQuery.AS_KNOWN_AT.value
    temporal_pivot: str | None = None
    candidate_retrieval_ms: int = 0
    metadata_generation_ms: int = 0
    selection_ms: int = 0
    note: str = ""


@dataclass(frozen=True)
class ExtractionCompositionShadowTrace:
    """Prediction + real outcome, combined once in build_shadow_trace()."""

    prediction: ShadowCompositionPrediction
    production_extracted_node_names: tuple[str, ...]
    production_extracted_facts: tuple[str, ...]
    shadow_total_ms: int


def _empty_prediction(
    *,
    episode_uuid: str,
    namespace: str,
    status: str,
    failure_stage: str | None = None,
    error: str | None = None,
    reference_time: str | None = None,
    production_facets: tuple[str, ...] = (),
    candidate_retrieval_ms: int = 0,
    candidates: tuple[ShadowCandidateFact, ...] = (),
    note: str = "",
) -> ShadowCompositionPrediction:
    """Build a prediction for any early-exit path (failure or trivial abstention).
    Centralizes the "always return a tagged result, never None" contract -- every
    early-exit path in compose_shadow_prediction (including the "no candidates"
    abstention) goes through here rather than hand-rolling the same ~10-field
    construction, so a future field addition only needs updating in one place.

    candidates defaults to () (the true "no candidates" cases), but every failure
    path that occurs AFTER real candidates were already retrieved (metadata
    generation failure, malformed LLM response, timeout) MUST pass the real
    candidates through -- silently blanking them on failure is exactly the kind of
    hidden-failure-mode this whole observability stage exists to prevent. A
    malformed_llm_response trace with an empty candidate list is undiagnosable:
    you can't tell whether the LLM saw reasonable input and produced garbage
    output, or never got meaningful input in the first place."""
    return ShadowCompositionPrediction(
        episode_uuid=episode_uuid,
        namespace=namespace,
        status=status,
        failure_stage=failure_stage,
        error=error,
        candidates=candidates,
        message_hypotheses=(),
        candidate_labels=(),
        production_facets=production_facets,
        rejected=(),
        selected_fact_uuid=None,
        abstention_reason=(status if status.startswith("abstained_") else None),
        llm_tie_breaker_fired=False,
        temporal_pivot=reference_time,
        candidate_retrieval_ms=candidate_retrieval_ms,
        note=note,
    )
