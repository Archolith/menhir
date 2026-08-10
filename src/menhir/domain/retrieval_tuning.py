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
    # (the memory's summary/content), as opposed to VECTOR which is graphiti's search over
    # n.name_embedding (the 1-3 word entity name). Rescues PARAPHRASE queries the lexical
    # (BM25) lane cannot reach -- the query's words differ from the memory's, but the
    # meaning matches. Behaves like VECTOR downstream: 0.0 source prior, NOT floor-exempt
    # (it is a semantic-similarity lane, subject to the cosine floor). Experimental,
    # default-off; gated by RetrievalTuningConfig.enable_content_vector.
    CONTENT_VECTOR = "content_vector"
    # Fact-EDGE candidates: graphiti EDGE_HYBRID search over RELATES_TO edges,
    # whose relevance is the edge's dated fact text (``EntityEdge.fact``), not an
    # entity-node name. This is the candidate the node-only recall path never
    # produced — the answer to a "what happened" query lives on the edge, so the
    # LongMemEval trace found it at edge-rank 2 while node search buried it. Gated
    # by RetrievalTuningConfig.enable_fact_edges (MENHIR_FRONTIER_FACT_EDGES).
    FACT_EDGE = "fact_edge"
    # Observation candidates: a menhir-owned cosine search over :TypedAssertion.name_embedding (the
    # typed-scalar observation log's stated_span). The recall pipeline is otherwise :Entity-only, so a
    # typed observation ("I own 20 rare coins") is never a candidate. Behaves like CONTENT_VECTOR
    # downstream: competes on its real cosine (0.0 source prior, NOT floor-exempt). Gated by
    # RecallService.scalar_view_authority_enabled (the scalar-view-authority feature; default off).
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
    FACT_EDGE_RRF = "fact_edge_rrf"


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
# KNOWN SCALE ACCIDENT (plan 1a, pending 1b): these priors live on [0, 1], but the
# VECTOR similarity they compete against in the additive formula is graphiti's RRF
# score on a ~[0, 2] scale (``GRAPHITI_RRF_DUAL_METHOD_MAX``), NOT a cosine. So
# ``PENDING = 1.0`` does not actually sit at the top today -- it lands mid-rank,
# around half of a dual-method top VECTOR hit. This is recorded as fact, not fixed
# here: correcting it rescales one additive lane and therefore changes ranking, so
# it ships behind ``RetrievalTuningConfig.similarity_scale="normalized"`` (plan 1b,
# measured on the oracle slice), never by editing these numbers in place.
SOURCE_PRIORS: dict[CandidateSource, float] = {
    CandidateSource.VECTOR: 0.0,
    CandidateSource.BM25: 0.0,
    # CONTENT_VECTOR competes on its real cosine like VECTOR: no baseline prior, and
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
    # OBSERVATION competes on its real cosine like CONTENT_VECTOR: no baseline prior, and NOT
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

    Defaults reproduce today's behavior exactly: ``enable_bm25=False`` keeps the
    single fused ``search_scored`` path (which itself already runs BM25 +
    cosine via graphiti's RRF reranker). Turning ``enable_bm25`` on switches
    recall to the *attributed* hybrid path that runs vector and BM25 as separate
    labeled passes and blends them with :attr:`hybrid_alpha`.

    ``hybrid_alpha`` is a rank-fusion weight, not a raw-score weight:
    ``1.0`` = vector only, ``0.0`` = BM25 only, ``0.5`` = symmetric (equivalent
    to plain reciprocal-rank fusion). It is left at a neutral default until
    archolith-bench can tune it per query class; it is a seam, not a tuned value.
    """

    hybrid_alpha: float = 0.5
    enable_bm25: bool = False
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
    # --- Frontier portions (active; each OFF == today's ScoringService behavior) ---
    # Oracle ranking: run oracles+combiner over the post-floor survivors and blend its
    # order into the upstream ScoringService/facet order. The upstream rank remains the
    # baseline so a secondary trust/temporal signal cannot erase an exact semantic hit.
    enable_oracle_ranking: bool = False
    # Reciprocal-rank residual weight for the Oracle order. 0 preserves the upstream
    # order exactly; 0.25 lets Oracle adjust close/lower-ranked candidates without
    # becoming a second replacement ranker.
    oracle_rank_weight: float = 0.25
    # Intent auto-lens: derive the temporal lens (current/historical/any) from the query
    # text for the oracle/warden path. No effect unless oracle_ranking or warden_gate is on.
    enable_intent_lens: bool = False
    # Warden gate: drop REFUSED candidates from results and label FLAGGED ones.
    enable_warden_gate: bool = False
    # Diversity gate: reorder results by evidence family to prevent any single source from dominating.
    enable_diversity_gate: bool = False
    # Contradiction interrupt: append ContradictionWarden to the default warden chain.
    enable_contradiction_interrupt: bool = False
    # Belief gate: append CurrentnessWarden + populate belief_score/evidence from
    # candidate metadata temporal markers. Default OFF; only belief-bearing candidates
    # are scored (warden ADMITs the rest permissively).
    enable_belief_gate: bool = False
    # Evidence-anchor gate (Guard 5 / EvidenceAnchorWarden): refuse candidates whose support is
    # synthetic-only / has no external anchor (user/log/test/git/file). ON = today's behavior.
    # Turn OFF for ANECDOTAL corpora (personal conversation) where every recalled fact is
    # LLM-extracted from chat with no code/test/git anchor — otherwise Guard 5 refuses the whole
    # result set. See MENHIR_FRONTIER_EVIDENCE_ANCHOR.
    enable_evidence_anchor: bool = True
    # Fact-edge retrieval: in addition to the entity-node candidate pool, run a
    # graphiti EDGE_HYBRID search and inject the top RELATES_TO fact edges as
    # CandidateSource.FACT_EDGE candidates (content == EntityEdge.fact). Default
    # OFF == today's node-only recall. Turn ON for episodic / "what happened"
    # corpora (LongMemEval) where the answer is a dated fact on an edge, not an
    # entity name. See MENHIR_FRONTIER_FACT_EDGES and services/recall_service.py.
    enable_fact_edges: bool = False
    # How many fact edges to pull when enable_fact_edges is on (best-first by the
    # edge RRF reranker). Kept modest so edges augment rather than swamp the node
    # pool; the combiner/floor still rank the merged set.
    fact_edge_k: int = 20
    # How a retrieved fact edge enters the candidate pool:
    #   "standalone" — inject the terse EntityEdge.fact as its own CandidateData (edge-as-
    #     answer). Measured net-NEGATIVE at N=30 (rung A′, 0.300->0.033): terse floor-exempt
    #     facts crowd out richer nodes and collapse context. Kept for A/B comparison.
    #   "pointer"    — edge-as-POINTER: the edge finds the right memory, then we hydrate its
    #     endpoint entity NODES (rich summaries + surrounding context) into the pool with an
    #     edge-derived similarity prior. Preserves node richness AND fixes ranking. This is
    #     the default (standalone was the footgun default, measured net-NEGATIVE).
    fact_edge_mode: str = "pointer"
    # Similarity scale contract (plan 1a/1b). "rrf" == today: graphiti's RRF
    # reranker score feeds the additive relevance formula on its native ~[0, 2]
    # scale (see scoring_service.GRAPHITI_RRF_DUAL_METHOD_MAX), and the 0.15 floor
    # / [0, 1] SOURCE_PRIORS are (by accident) mixed across it. "normalized"
    # divides the VECTOR/BM25 search scores by that pinned max so `similarity` is
    # [0, 1] and PENDING=1.0 regains its intended top-pin; the floor scales to
    # 0.075 in this mode so floor MEMBERSHIP is identical by construction. This
    # rescales one additive lane and therefore CHANGES ranking, so it ships behind
    # this flag (default "rrf", byte-for-byte today) and is measured on the bench
    # oracle slice with a pre-registered parity rule before any default flip --
    # never by argument (retrieval doc S6).
    #
    # MEASURED 2026-07-04 (LongMemEval oracle slice, N=500, llm-judge, recall-only):
    # rrf 143/500 (0.2860) vs normalized 143/500 (0.2860) -- EXACT parity, with 18
    # items churned symmetrically (9 win / 9 lose). Meets the >= parity flip rule on
    # this corpus. Default held at "rrf" pending a multi-corpus check: LongMemEval
    # under-exercises the PENDING/FILE_LINKED priors this change most affects (pure
    # recall, no fresh-ingest or file-context at query time), so the global default
    # flip is not yet claimed on one anecdotal corpus's parity.
    similarity_scale: str = "rrf"
    # Content-vector lane (plan menhir-content-embedding-recall, Track A). EXPERIMENTAL,
    # default OFF == today's behavior byte-for-byte. When on, recall runs a menhir-owned
    # cosine pass over n.content_embedding (memory summary/content) as a third labeled lane
    # and fuses it via weighted_rrf_multi. Rescues PARAPHRASE queries the lexical (BM25)
    # lane cannot reach. Benchmark-gated; must not be flipped on without the fusion parity
    # test passing. See MENHIR_FRONTIER_CONTENT_VECTOR.
    enable_content_vector: bool = False
    # Benchmark Arm C: suppress name-vector while retaining BM25 + content-vector.
    content_vector_replace_name: bool = False
    # How many content-vector hits to pull (best-first by content cosine) when the lane is
    # on. Kept comparable to candidate_k so the lane competes on equal footing.
    content_vector_k: int = 100
    # This lane's weight in weighted_rrf_multi relative to the name-vector and BM25 lanes.
    content_vector_weight: float = 0.5
    # ``attributed`` preserves the existing experimental BM25 behavior. Benchmark
    # control/treatment arms use ``production_fused`` so BM25 membership alone never
    # grants a floor exemption that today's opaque Graphiti fusion does not grant.
    fusion_admission_policy: str = "attributed"

    def __post_init__(self) -> None:
        if not 0.0 <= self.hybrid_alpha <= 1.0:
            raise ValueError(
                f"hybrid_alpha must be in [0.0, 1.0], got {self.hybrid_alpha!r}"
            )
        if self.similarity_scale not in ("rrf", "normalized"):
            raise ValueError(
                f'similarity_scale must be "rrf" or "normalized", '
                f"got {self.similarity_scale!r}"
            )
        if self.content_vector_weight < 0.0:
            raise ValueError(
                f"content_vector_weight must be >= 0.0, got {self.content_vector_weight!r}"
            )
        if self.facet_weight < 0.0:
            raise ValueError(
                f"facet_weight must be >= 0.0, got {self.facet_weight!r}"
            )
        if self.content_vector_k < 1:
            raise ValueError(
                f"content_vector_k must be >= 1, got {self.content_vector_k!r}"
            )
        if self.fusion_admission_policy not in ("attributed", "production_fused"):
            raise ValueError(
                "fusion_admission_policy must be attributed or production_fused, "
                f"got {self.fusion_admission_policy!r}"
            )
