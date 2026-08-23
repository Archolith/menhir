"""Domain types for retrieval and scoring results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import menhir.domain.retrieval_trace_models as retrieval_trace_models
from menhir.domain.retrieval_tuning import CandidateSource, RetrievalScoreKind
from menhir.domain.truth.labels import WardenLabel


class InvalidQueryPresetError(ValueError):
    """Raised when a caller supplies an unsupported recall preset."""


class QueryPreset(str, Enum):
    RECENT = "recent"
    KNOWLEDGE = "knowledge"
    EMOTIONAL = "emotional"
    CONNECTED = "connected"
    CONFLICT = "conflict"


VALID_QUERY_PRESET_VALUES: tuple[str, ...] = tuple(preset.value for preset in QueryPreset)


def format_query_preset_values() -> str:
    """Return the canonical preset list for user-facing error messages."""
    return ", ".join(VALID_QUERY_PRESET_VALUES)


def parse_query_preset(value: str) -> QueryPreset:
    """Parse a preset string into QueryPreset with a stable error message."""
    try:
        return QueryPreset(value)
    except ValueError as exc:
        raise InvalidQueryPresetError(
            f"Invalid preset '{value}'. Use: {format_query_preset_values()}."
        ) from exc


# Weights: (alpha=adjacency, beta=recency, gamma=prominence, delta=conflict_boost)
#
# delta is non-zero only for CONFLICT preset — it surfaces nodes that are
# actively involved in an unresolved conflict when the caller wants to review them.
# EMOTIONAL preset retains delta=0.0; emotional arousal signals are deferred post-v1.
#: The relationship types that establish RECALL ADJACENCY (CF-247, owner ruling 2026-08-23).
#:
#: Before this existed, both the adjacency producer and the reinforcement consumer matched
#: relationships UNTYPED -- `MATCH (a)-[r]-(b)` and `MATCH ()-[r]->()`. The effective contract was
#: "every relationship type in the graph establishes adjacency", which nobody decided; it is simply
#: what an untyped pattern gives you. 30+ types are created across the codebase.
#:
#: **Only four of those could ever reach adjacency**, because both endpoints must be recall
#: candidates -- non-structural `:Entity`/`:Episodic` -- and `context_node_ids` is never populated
#: by any caller. `ANCHORED_TO` targets a structural entity the recall filter excludes; the view
#: plumbing (`CURRENT_ANCHOR`, `CONTRIBUTED_TO`, `SUPERSEDED_ANCHOR`, `HISTORY_ENTRY`) targets
#: `:TypedAssertion`; `ADMITTED_ON` targets `:TurnEvidence`; `HAS_MEMBER`/`HAS_EPISODE` target
#: communities. None of them is recallable, so none of them was ever in play.
#:
#: The four that were: `RELATES_TO`, `MENTIONS`, `NEXT_EPISODE`, and `SUPERSEDES`.
#:
#: `SUPERSEDES` is the one that was doing damage. `artifact_repository.supersede_artifact` writes
#: it between two `:Entity` artifacts, so recalling a current artifact gave its SUPERSEDED
#: predecessor an adjacency boost -- the ranker learning that stale content is strongly associated
#: with the thing that replaced it. `NEXT_EPISODE` is dropped by the same ruling for a weaker
#: reason: temporal succession is not semantic relatedness, and two consecutive episodes about
#: unrelated subjects should not rank each other up.
ADJACENCY_EDGE_TYPES: tuple[str, ...] = ("RELATES_TO", "MENTIONS")


def adjacency_edge_pattern() -> str:
    """Emit the type disjunction for an adjacency relationship pattern (CF-247).

    Used by BOTH the producer (`fetch_adjacency_pairs`) and the consumer
    (`increment_edge_weights`). Emitting it from one place is the point of the finding: narrowing
    the consumer alone would produce a silent producer/consumer disagreement, where recall ranks on
    an edge it then declines to reinforce.

    The fragment is a constant built from this module's own tuple, never caller input.
    """
    # Read through the module global rather than closing over it at import: a test that swaps the
    # ruling must see the emitter follow, and that indirection is what makes this a single source
    # of truth rather than two copies that agree today.
    return "|".join(ADJACENCY_EDGE_TYPES)


PRESET_WEIGHTS: dict[QueryPreset, tuple[float, float, float, float]] = {
    QueryPreset.RECENT: (0.1, 0.5, 0.1, 0.0),
    QueryPreset.KNOWLEDGE: (0.2, 0.1, 0.1, 0.0),
    QueryPreset.EMOTIONAL: (0.1, 0.2, 0.1, 0.0),
    QueryPreset.CONNECTED: (0.4, 0.1, 0.1, 0.0),
    QueryPreset.CONFLICT: (0.1, 0.1, 0.1, 0.4),
}


@dataclass(frozen=True)
class TemporalFact:
    """Bi-temporal fact edge state linking an entity to another entity.

    Clock semantics:
    - valid_at / invalid_at = WORLD time (when the fact was/stopped being true)
    - created_at / expired_at = BELIEF time (when menhir learned it / learned it was superseded)
    - is_current_belief = (expired_at is None)
    """

    fact: str | None
    valid_at: str | None
    invalid_at: str | None
    created_at: str | None
    expired_at: str | None
    is_current_belief: bool
    temporal_role: str  # "current_belief" | "superseded_belief"


@dataclass(frozen=True)
class ScoredMemory:
    uuid: str
    name: str
    content: str | None
    scope: str
    memory_type: str
    final_score: float
    breakdown: retrieval_trace_models.RelevanceBreakdown
    retrieval_score: float | None = None
    retrieval_score_kind: RetrievalScoreKind = RetrievalScoreKind.GRAPHITI_RRF
    # Set only by the frontier warden gate (enable_warden_gate): the FLAG label.
    # None on the old path and for ADMITted items. Always a WardenLabel value when set.
    warden_label: WardenLabel | None = None
    temporal_facts: tuple[TemporalFact, ...] = ()
    # True only for a superseded View version (view_current=false) surfaced via
    # include_superseded — the "clearly labeled" flag for historical/provenance/debug recall.
    is_superseded_view: bool = False
    # Claim shape identifier: present only for counter-View surfaces, used in dedup to avoid
    # collapsing distinct view kinds against each other. Rank-inert (display/dedup only).
    view_kind: str | None = None
    # True for the DETERMINISTICALLY-injected current scalar_state View (CandidateSource.SCALAR_AUTHORITY,
    # Phase 4a.4): this result carries the authoritative CURRENT value for a surfaced slot, resolved by
    # slot-keyed lookup rather than embedding rank. The consumer uses it to lead the answer with the
    # current value ("30 coins now") and treat the other observations as its history/provenance. The
    # marker is DATA (Phase 4c) -- menhir states which surface is current; it composes no prose.
    is_scalar_authority: bool = False
    # Stale-anchor metadata set by Hook Center labeling. None when not evaluated.
    # Dict keys: stale_anchor (bool), stale_reason (str|None), dirty_at (str|None),
    # anchored_at (str|None), path (str|None).
    stale_anchor_info: dict[str, Any] | None = field(default=None)


@dataclass(frozen=True)
class ScalarAuthorityContributor:
    """One immutable observation participating in (or excluded from) an authority verdict."""

    assertion_id: str
    relation: str
    operation: str
    value: Any
    stated_span: str
    valid_at: str | None
    evidence_tier: str | None
    episode_uuid: str | None


@dataclass(frozen=True)
class ScalarAuthorityVerdict:
    """Structured current/expired/as-of authority, distinct from ranked memory candidates."""

    kind: str  # current | expired | as_of
    status: str  # leads | advisory
    subject_uuid: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    value: Any
    valid_at: str | None
    view_uuid: str | None
    has_foundation: bool
    contributors: tuple[ScalarAuthorityContributor, ...] = ()
    contributors_total: int = 0
    contributors_truncated: bool = False
    next_offset: int | None = None


@dataclass(frozen=True)
class EventAuthorityVerdict:
    """Structured first-person event authority, separate from scalar authority.

    Immutable advisory/lead verdict for a conservative ``did I`` event recall query, layered onto
    ``RecallResult.event_authority_layer`` and distinct from ``ScalarAuthorityVerdict``. ``status``
    is ``leads`` or ``advisory``. ``gate`` is an explanatory string (``pass`` for a fully grounded
    non-generic unique lead; selection-failure gates mirror ``EventRecallGate``; ``route`` marks a
    unique generic selection; ``foundation`` marks an unverified foundation). Selected grounding
    fields are None when a gate failed (advisory). Never derived from ``learned_at``: authority rests
    on ``valid_at`` plus exact quote/evidence identity.
    """

    predicate: str
    object_key: str | None
    object_display: str | None
    valid_at: str | None
    stated_span: str | None
    assertion_key: str | None
    episode_uuid: str | None
    turn_evidence_uuid: str | None
    domain: str | None
    time_basis: str | None
    status: str  # leads | advisory
    gate: str  # pass | route | foundation | scope | anchor | ambiguity | time | no_candidate | ...
    reason: str
    subject_uuid: str
    has_foundation: bool
    kind: str  # latest | predecessor | generic_advisory | anchor_guard


@dataclass(frozen=True)
class RecallResult:
    query: str
    preset: str
    results: list[ScoredMemory]
    candidates_evaluated: int
    nodes_touched: int
    note: str | None = None
    # Set when candidate generation failed and recall returned a degraded result.
    # This distinguishes a backend failure from a legitimate zero-match search.
    search_error: str | None = None
    # R0 retrieval observability: populated only when recall(trace=True).
    trace: retrieval_trace_models.RetrievalTrace | None = None
    # Phase 4c/7.J: an explicit authority layer, separate from flat ranked candidates. None when the
    # authority feature is disabled or no verdict was produced, preserving flag-off wire output.
    authority_layer: tuple[ScalarAuthorityVerdict, ...] | None = None
    # First-person event authority, separate from scalar authority. None when the authority feature
    # is disabled, no verdict was produced, or the query abstained; preserves flag-off wire output.
    event_authority_layer: tuple[EventAuthorityVerdict, ...] | None = None


@dataclass(frozen=True)
class CandidateData:
    """Internal type holding all pre-fetched metadata for one candidate."""

    uuid: str
    name: str
    content: str | None
    scope: str
    memory_type: str
    similarity: float
    last_accessed_days_ago: float
    edge_count: int
    adjacency_score: float
    freshness: str
    has_conflict: bool = False
    conflict_status: str | None = None
    source: CandidateSource = CandidateSource.VECTOR
    is_superseded_view: bool = False
    # Claim shape identifier: present only for counter-View surfaces, used in dedup to avoid
    # collapsing distinct view kinds against each other. Rank-inert (display/dedup only).
    view_kind: str | None = None
    retrieval_score: float | None = None
    retrieval_score_kind: RetrievalScoreKind = RetrievalScoreKind.GRAPHITI_RRF
    bm25_rank: int | None = None
    cosine_rank: int | None = None
    contributing_sources: frozenset[CandidateSource] = frozenset()
    content_rank: int | None = None
    content_cosine: float | None = None
    # True when a deterministically-injected current scalar_state View has a SOURCE FOUNDATION and may
    # LEAD as the authoritative current value (Phase 4b/G10). Decoupled from `source`: an injected View
    # is always CandidateSource.SCALAR_AUTHORITY (floor-exempt), but it is marked authoritative ONLY
    # when its effective tier rests on a foundation (not agent-only extraction) -- else it stays advisory.
    is_scalar_authority: bool = False
