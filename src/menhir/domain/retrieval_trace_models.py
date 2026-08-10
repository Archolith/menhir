"""Neutral domain models shared by recall results and retrieval tracing.

A ``RetrievalTrace`` is an opt-in, library-level record of one ``recall()`` call:
per-phase timings plus a per-candidate row capturing source attribution, the
similarity the floor saw, whether the candidate survived the source-aware floor,
and (for survivors) the full score breakdown, final score, and rank.

Design contract:
- Producing a trace is OFF by default. When no trace is requested the recall and
  scoring paths are byte-for-byte unchanged (zero overhead).
- These types are pure data with no infrastructure dependency. A persistence or
  log sink can consume ``RetrievalTrace`` later without changing the producer.
- Scope is retrieval scoring plus an OPTIONAL observe-only oracle/warden shadow
  pass (``AssertionShadowTrace``). The shadow pass runs only when a trace is
  requested; it records what the oracle/warden stack would do but never changes
  the recall results, so the shipped recall ranking is unaffected.

The contracts deliberately live outside ``domain.recall``, making the dependency
one-way and giving retrieval observability a single canonical owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from menhir.domain.retrieval_tuning import CandidateSource
from menhir.domain.truth.labels import WardenLabel


@dataclass(frozen=True)
class RelevanceBreakdown:
    semantic_similarity: float
    adjacency_bonus: float
    recency_bonus: float
    prominence_bonus: float
    conflict_bonus: float
    type_boost: float
    preset: str
    alpha: float
    beta: float
    gamma: float
    delta: float


@dataclass(frozen=True)
class CandidateTrace:
    """One candidate's journey through scoring and the source-aware floor."""

    uuid: str
    source: CandidateSource
    similarity: float
    survived_floor: bool
    # score_parts/final_score/rank are populated only for candidates that
    # survived the floor and were scored. Floored candidates carry None — they
    # were never scored, so reporting a breakdown would be fabricated.
    score_parts: RelevanceBreakdown | None = None
    final_score: float | None = None
    rank: int | None = None
    bm25_rank: int | None = None
    cosine_rank: int | None = None
    contributing_sources: frozenset[CandidateSource] = frozenset()
    content_rank: int | None = None
    content_cosine: float | None = None


@dataclass(frozen=True)
class AssertionShadowRow:
    """One candidate's would-be oracle/warden verdict (observe-only)."""

    candidate_id: str
    rank: int                    # the oracle combiner's rank (NOT the shipped score rank)
    decision: str                # admit | flag | attenuate | refuse
    score: float                 # combiner probability x warden attenuation
    label: WardenLabel | None = None  # warden FLAG qualifier


@dataclass(frozen=True)
class AssertionShadowTrace:
    """Observe-only record of the frontier oracle/warden pass over a recall.

    SHADOW means recorded, never applied: it captures what the AssertionPipeline would
    have admitted / flagged / refused and how the oracle combiner would have ranked the
    candidates, so the frontier stack can be measured against the shipped ScoringService
    ranking on the live graph without changing any recall result.
    """

    intent: str                  # the temporal lens the pipeline resolved from the query
    admitted: int
    flagged: int
    refused: int
    rows: list[AssertionShadowRow]
    note: str = ""               # honest limitations of the inputs the shadow pass saw


@dataclass(frozen=True)
class FacetShadowRow:
    """One candidate's would-be FACET contribution (observe-only)."""

    candidate_id: str
    rank: int                          # meet-point rank within the facet-overlap candidates
    score: float                       # convergence meet-point score (discipline deferred to wardens)
    convergence: dict[str, list[str]]  # shared file/symbol/test values with the query
    matched_required: list[str]        # required facets (scope/operation/object/symbol) that overlapped


@dataclass(frozen=True)
class FacetShadowTrace:
    """Observe-only record of what CandidateSource.FACET would contribute to a recall.

    SHADOW means recorded, never applied: which memories facet-overlap candidate
    generation surfaces from the recall pool and how the meet-point reranker would order
    them (convergence only; scope/stale discipline stays with the warden chain), so FACET
    can be measured on the live graph without changing any recall result.
    """

    pool: int                    # candidates the FACET source saw
    candidates: int              # candidates it generated (>=1 facet overlap with the query)
    query_facets: list[str]      # the query's discrete (facet=value) pairs
    rows: list[FacetShadowRow]
    note: str = ""


@dataclass(frozen=True)
class ViewReachability:
    """D0 view-reachability: where the first current View landed in the shipped results.

    A View is materialized query-sufficient state, so for its query class "retrieval
    reached the View" IS the deterministic sufficiency check (no labels, no LLM). Rank
    is 1-based within the shipped results; tokens_to_view is the estimated token
    footprint of the greedy rank walk up to and including the View. Reachability only —
    it does not say the View's value is correct or current (belief-gate's axis).
    """

    uuid: str
    view_kind: str
    rank: int
    tokens_to_view: int


@dataclass(frozen=True)
class RetrievalTrace:
    """Inline observability record for a single recall call."""

    query: str
    preset: str
    total_ms: int
    phases: dict[str, int]
    candidates: list[CandidateTrace]
    # Observe-only frontier oracle/warden outcome; None unless the shadow pass ran.
    assertion_shadow: AssertionShadowTrace | None = None
    # Observe-only FACET candidate-generation outcome; None unless the facet shadow ran.
    facet_shadow: FacetShadowTrace | None = None
    # Active FACET pass used in candidate rank fusion. Reuses the same explanation
    # shape as the shadow, but its note states that the rows affected ranking.
    facet_active: FacetShadowTrace | None = None
    # First current View in the shipped results; None when no View surfaced (or the
    # query class has no maintained View — absence is only meaningful for queries
    # that target one).
    view_reachability: ViewReachability | None = None
    rank_shadow_warning: str | None = None


@dataclass
class ScoringTrace:
    """Mutable collector handed into ScoringService.score_candidates.

    The scorer records every candidate it sees (both floored and scored) here.
    It is intentionally NOT frozen: it is a write-target during scoring. The
    finished ``candidates`` list is copied into the frozen ``RetrievalTrace``.
    """

    candidates: list[CandidateTrace] = field(default_factory=list)


__all__ = [
    "AssertionShadowRow",
    "AssertionShadowTrace",
    "CandidateTrace",
    "FacetShadowRow",
    "FacetShadowTrace",
    "RelevanceBreakdown",
    "RetrievalTrace",
    "ScoringTrace",
    "ViewReachability",
]
