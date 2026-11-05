"""Retrieval tuning primitives — candidate sources, per-source priors, hybrid config.

This module makes explicit a pattern that already existed ad hoc in the recall
path: different candidate *sources* carry different baseline priors so that
non-vector candidates survive the semantic-similarity floor.

Before this module, two source priors lived as loose constants in
``recall_service`` (``PENDING_ENTITY_SIMILARITY = 1.0``,
``FILE_LINKED_BASELINE_SIMILARITY = 0.3``) and were injected straight into the
similarity map. They now live here as :data:`SOURCE_PRIORS`, keyed by an explicit
:class:`CandidateSource`, so BM25/facet/structure sources can be added the same
way instead of overloading ``similarity`` further.

See ``docs/research/retrieval-tuning-stack.md`` (hybrid alpha) and
``docs/research/oracle-execution-and-performance.md`` §3 (priors / floor).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CandidateSource(str, Enum):
    """Where a recall candidate came from.

    The first three values formalize sources the recall path already produced:
    ``VECTOR`` (graphiti vector/BM25 search), ``PENDING`` (entities linked from a
    just-enriched episode), and ``FILE_LINKED`` (structural file-context
    injection). ``BM25`` is the attributed lexical source that R1 splits out
    of the otherwise-fused vector search. ``FACET`` is an experimental active
    rank-fusion lane; ``STRUCTURE`` remains reserved for a later rung.
    """

    VECTOR = "vector"
    BM25 = "bm25"
    PENDING = "pending"
    FILE_LINKED = "file_linked"
    FACET = "facet"
    STRUCTURE = "structure"
    # Content-vector candidates: a menhir-owned cosine search over n.content_embedding
    # (the memory's summary/content; lane removed 2026-10-27, member kept for stored traces), as opposed to VECTOR which is graphiti's search over
    # n.name_embedding (the 1-3 word entity name). Rescues PARAPHRASE queries the lexical
    # (BM25) lane cannot reach -- the query's words differ from the memory's, but the
    # meaning matches. Behaves like VECTOR downstream: 0.0 source prior, NOT floor-exempt
    # (it is a semantic-similarity lane, subject to the cosine floor). Experimental,
    CONTENT_VECTOR = "content_vector"
    # Fact-EDGE candidates: graphiti EDGE_HYBRID search over RELATES_TO edges,
    # whose relevance is the edge's dated fact text (``EntityEdge.fact``), not an
    # entity-node name. This is the candidate the node-only recall path never
    # produced — the answer to a "what happened" query lives on the edge, so the
    # LongMemEval trace found it at edge-rank 2 while node search buried it. Gated
    # The lane was removed 2026-10-27; the member stays as vocabulary stored traces may contain.
    FACT_EDGE = "fact_edge"
    # Observation candidates: a menhir-owned cosine search over :TypedAssertion.name_embedding (the
    # typed-scalar observation log's stated_span). The recall pipeline is otherwise :Entity-only, so a
    # typed observation ("I own 20 rare coins") is never a candidate. Behaves like CONTENT_VECTOR
    # (lane removed 2026-10-27) downstream: competes on its real cosine (0.0 source prior, NOT floor-exempt). Gated by
    # RecallService.scalar_view_authority_enabled (the scalar-view-authority feature; default on,
    # effective only while the scalar lane is on).
    OBSERVATION = "observation"
    # Scalar-authority candidate: the CURRENT scalar_state View for a slot, injected DETERMINISTICALLY by
    # slot-keyed lookup (not embedding rank) so the authoritative current value surfaces even when its
    # View did not win ranking -- the G5 stale-value failure. Floor-exempt (its relevance is provenance:
    # the slot was surfaced by a matching observation), NOT cosine. Gated by scalar_view_authority_enabled.
    SCALAR_AUTHORITY = "scalar_authority"


class RetrievalScoreKind(str, Enum):
    """Machine-readable scale/provenance of the raw retrieval score."""

    GRAPHITI_RRF = "graphiti_rrf"
    WEIGHTED_RRF_NORMALIZED = "weighted_rrf_normalized"
    SOURCE_PRIOR = "source_prior"
    FACT_EDGE_RRF = "fact_edge_rrf"  # fact-edge lane removed 2026-10-27; kept for stored traces


# Candidates below this score are noise and should be filtered before scoring.
#
# NOTE (verified live 2026-06-28, see .agent/plans/deferred-verification.md): the
# `similarity` this gates is graphiti's RRF node_reranker_score (search_scored),
# NOT a cosine similarity. With graphiti's default rank_const=1 the score is
# sum_methods 1/(rank+1) -- a dual-method top hit = 2.0, single-method top = 1.0 --
# so a 0.15 floor behaves as a RANK CUT (~top 12-13 dual / ~top 6 single), not a
# cosine cutoff. The value is reasonable only by luck of rank_const=1; revisit the
# scale (rescale or re-document as a rank cut) before tuning it.
#
# The floor is SOURCE-AWARE: it only gates candidates whose relevance is a
# vector-similarity score (CandidateSource.VECTOR). Candidates that were injected
# by provenance -- an exact lexical/BM25 hit, a linked pending episode, a
# structural file neighbor -- carry their relevance in their source, not in the
# RRF score, and must not be silently dropped by this gate. Those sources are
# listed in FLOOR_EXEMPT_SOURCES (see FLOOR_EXEMPT_SOURCES below).
#
# SCALE CONTRACT (plan 1a): this floor is a RANK CUT on graphiti's RRF scale, not
# a cosine threshold. graphiti's NodeReranker.rrf scores each hit as
# sum_methods 1/(rank + rank_const) with rank_const=1, so a dual-method (bm25 +
# cosine) top hit tops out at 1/(0+1) + 1/(0+1) = GRAPHITI_RRF_DUAL_METHOD_MAX.
# 0.15 on that scale admits roughly the top ~12-13 dual / ~6 single hits.
# test_scoring_service::test_graphiti_rrf_scale_contract pins the rank_const=1
# assumption against the graphiti package and fails loudly if upstream changes it.
MIN_SIMILARITY_THRESHOLD = 0.15

# The dual-method (bm25 + cosine) top-hit ceiling of graphiti's RRF reranker score
# under rank_const=1. This is the scale MIN_SIMILARITY_THRESHOLD and the [0,1]
# SOURCE_PRIORS are (accidentally) mixed across today.
# Pinned by test_graphiti_rrf_scale_contract so an upstream RRF change is caught.
GRAPHITI_RRF_DUAL_METHOD_MAX = 2.0


# Baseline prior per source, intended on the [0, 1] scale the scoring floor uses.
#
# These are the values that let non-vector candidates clear
# ``MIN_SIMILARITY_THRESHOLD``. ``VECTOR``/``BM25`` carry no baseline prior: their
# similarity is the real search score, gated by the floor (see
# ``scoring_service`` for the source-aware gate). ``PENDING`` is *intended* as a
# top-pin (a just-enriched entity is maximally relevant to the query that
# surfaced it); ``FILE_LINKED`` gets a modest baseline so structural neighbors are
# considered without dominating genuine semantic matches.
#
# KNOWN SCALE ACCIDENT (plan 1a): these priors live on [0, 1], but the
# VECTOR similarity they compete against in the additive formula is graphiti's RRF
# score on a ~[0, 2] scale (``GRAPHITI_RRF_DUAL_METHOD_MAX``), NOT a cosine. So
# ``PENDING = 1.0`` does not actually sit at the top today -- it lands mid-rank,
# around half of a dual-method top VECTOR hit. This is recorded as fact, not fixed
# here: correcting it rescales one additive lane and therefore changes ranking.
SOURCE_PRIORS: dict[CandidateSource, float] = {
    CandidateSource.VECTOR: 0.0,
    CandidateSource.BM25: 0.0,
    # CONTENT_VECTOR (lane removed 2026-10-27) competed on its real cosine like VECTOR: no baseline prior, and
    # NOT floor-exempt (absent from FLOOR_EXEMPT_SOURCES below).
    CandidateSource.CONTENT_VECTOR: 0.0,
    CandidateSource.PENDING: 1.0,
    CandidateSource.FILE_LINKED: 0.3,
    # FACT_EDGE carries no baseline prior: its similarity is the real graphiti
    # edge RRF score (same scale as the node reranker score), so it competes
    # on merit. It is floor-exempt only so the node-tuned cosine floor — which
    # was calibrated against node scores — never silently drops a well-ranked
    # fact edge before it reaches the combiner.
    CandidateSource.FACT_EDGE: 0.0,
    # OBSERVATION competes on its real cosine like CONTENT_VECTOR (lane removed 2026-10-27): no baseline prior, and NOT
    # floor-exempt (a low-relevance observation should be dropped by the floor, not forced in).
    CandidateSource.OBSERVATION: 0.0,
    # SCALAR_AUTHORITY is deterministically injected (the current value for a surfaced slot), so it
    # carries a top-ish baseline prior and is floor-exempt (below) -- it must not be dropped by the
    # cosine floor calibrated for ranked node scores.
    CandidateSource.SCALAR_AUTHORITY: 1.0,
}

# Sources whose candidates are intentionally injected and must NOT be dropped by
# the vector-similarity floor. Their relevance comes from provenance (a linked
# pending episode, a structural file neighbor, an exact lexical hit), not from
# cosine similarity, so the cosine noise floor does not apply to them.
FLOOR_EXEMPT_SOURCES: frozenset[CandidateSource] = frozenset(
    {
        CandidateSource.BM25,
        CandidateSource.PENDING,
        CandidateSource.FILE_LINKED,
        CandidateSource.FACET,
        CandidateSource.STRUCTURE,
        CandidateSource.FACT_EDGE,
        CandidateSource.SCALAR_AUTHORITY,
    }
)


@dataclass(frozen=True)
class RetrievalTuningConfig:
    """Knobs for candidate generation and semantic tuning.

    Defaults reproduce today's behavior exactly: candidate generation is the single
    fused ``search_scored`` path (which itself already runs BM25 + cosine via
    graphiti's RRF reranker).
    """

    enable_cross_encoder_rerank: bool = False
    rerank_top_n: int = 50
    embedding_dimensions: int | None = None
    # Observe-only: when a trace is requested, also run the AssertionPipeline (oracle +
    # warden) over the candidate set and attach the outcome to the trace. Never changes
    # recall results; only adds work on the opt-in trace path, so normal recall is
    # byte-for-byte unchanged regardless of this flag.
    enable_assertion_shadow: bool = True
    # Observe-only: when a trace is requested, also run the FACET candidate source (facet
    # overlap generation + meet-point convergence) over the candidate pool and attach the
    # outcome to the trace. Default OFF (it adds one bulk graph query per traced recall);
    # opt in to measure FACET's contribution on the live graph before wiring it active.
    enable_facet_shadow: bool = False
    # Experimental active FACET lane: derive facet-overlap/convergence order over
    # the retrieved pool and fuse that rank with the existing candidate order.
    # Unlike enable_facet_shadow, this changes similarity, admission provenance,
    # and final ranking. Default OFF preserves production behavior.
    enable_facet_candidates: bool = False
    facet_weight: float = 0.5
    # --- Frontier portions (active; each OFF == plain ScoringService order) ---
    # Oracle ranking: run oracles+combiner over the post-floor survivors and blend its
    # order into the upstream ScoringService/facet order. The upstream rank remains the
    # baseline so a secondary trust/temporal signal cannot erase an exact semantic hit.
    # On by default since #172 (retrieval-neutral on LME, within latency limits); kept as a
    # per-request knob for Recall Lab comparisons. There is no deployment flag.
    enable_oracle_ranking: bool = True
    # Reciprocal-rank residual weight for the Oracle order. 0 preserves the upstream
    # order exactly; 0.25 lets Oracle adjust close/lower-ranked candidates without
    # becoming a second replacement ranker.
    oracle_rank_weight: float = 0.25
    # Intent auto-lens: derive the temporal lens (current/historical/any) from the query
    # text for the oracle/warden path. No effect unless oracle_ranking or warden_gate is on.
    # On by default since #172, like oracle ranking.
    enable_intent_lens: bool = True
    # Warden gate: drop REFUSED candidates from results and label FLAGGED ones. On by default since #168 freeze; kept as a per-request knob.
    enable_warden_gate: bool = True
    # Contradiction interrupt: append ContradictionWarden to the default warden chain. On by default since #168 freeze; kept as a per-request knob.
    enable_contradiction_interrupt: bool = True
    # Minimum query similarity (best per slot) for a scalar verdict to be emitted; 0 = off.
    # Neo4j vector.similarity.cosine scale, (1+cos)/2. MENHIR_PERSONAL_MEMORY_SCALAR_VERDICT_MIN_SIMILARITY.
    scalar_verdict_min_similarity: float = 0.0
    # Which raw scalar observations enter the ranked list (MENHIR_PERSONAL_MEMORY_SCALAR_OBSERVATION_RECALL):
    # "all" (every hit), "gated" (hits at or above scalar_verdict_min_similarity) or "off" (none).
    # Slots are collected for the authority and history lanes in every mode.
    scalar_observation_recall: str = "all"
    # A current or expired verdict stops leading when the recall's observation hits hold newer
    # evidence its fold ignores (see domain/scalar_view_freshness.py). The verdict is still shown,
    # as advisory. On by default since the #168 freeze; kept as a per-request knob.
    scalar_view_freshness_gate: bool = True
    # Source-memory lane (plan menhir-source-memory-lane). When on, recall builds an ADDITIVE
    # source_memories section (raw episode contents by cosine over n.content_embedding, oldest
    # first) next to the ranked results. Never fused: results/candidates_evaluated are identical
    # with the lane on or off. On by default since #168 freeze; kept as a per-request knob.
    enable_source_memories: bool = True
    # Section size (effective only when enable_source_memories is on, or when a caller
    # passes an explicit per-call source_memory_limit > 0).
    source_memory_limit: int = 10
    # Per-memory content cap in the section; cut content ends with a trailing ellipsis.
    source_memory_max_chars: int = 2000
    # Pick the section from a larger pool: drop exact content repeats, then each session's best
    # memory before more from the same session (domain/source_memory_spread.py). On by default
    # since #168 freeze; kept as a per-request knob.
    source_memory_spread: bool = True
    # Put each fact edge's statement time (its reference_time) on recall temporal facts as
    # said_at, rendered as "said <time>". Presentation only. On by default since #168 freeze;
    # kept as a per-request knob.
    enable_fact_said_time: bool = True
    # Label unexpired facts whose own end (invalid_at) has passed as "ended" instead of
    # "current_belief" (follows MENHIR_ANCHORED_TIME_RESOLVER; settings pass the switch value).
    # Presentation only, like the render flag.
    enable_anchored_time_expiry: bool = True
    # One observation per identical stated text in the observation lane (MENHIR_PERSONAL_MEMORY_OBSERVATION_DEDUP_ENABLED).
    enable_observation_dedup: bool = False

    def __post_init__(self) -> None:
        # NaN fails this too: it would otherwise silently disable the gate.
        if not 0.0 <= self.scalar_verdict_min_similarity <= 1.0:
            raise ValueError(
                "scalar_verdict_min_similarity must be in [0.0, 1.0], "
                f"got {self.scalar_verdict_min_similarity!r}"
            )
        if self.scalar_observation_recall not in ("all", "gated", "off"):
            raise ValueError(
                'scalar_observation_recall must be "all", "gated" or "off", '
                f"got {self.scalar_observation_recall!r}"
            )
        if self.facet_weight < 0.0:
            raise ValueError(
                f"facet_weight must be >= 0.0, got {self.facet_weight!r}"
            )
        if not 1 <= self.source_memory_limit <= 50:
            raise ValueError(
                f"source_memory_limit must be in [1, 50], got {self.source_memory_limit!r}"
            )
        if not 100 <= self.source_memory_max_chars <= 4000:
            raise ValueError(
                "source_memory_max_chars must be in [100, 4000], "
                f"got {self.source_memory_max_chars!r}"
            )
