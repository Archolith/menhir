# Retrieval Tuning Profiles

Per-corpus flag profiles for `RetrievalTuningConfig`. Use these when configuring recall for a specific corpus type to avoid common footguns.

---

## Code-Workspace Profile

**Use for**: codebases, code reviews, file-context queries, structural memory.

```python
RetrievalTuningConfig(
    enable_evidence_anchor=True,       # Guard 5: enforce external anchors
    enable_fact_edges=False,           # Facts on edges are rare in code contexts
)
```

**Why these settings:**

- `enable_evidence_anchor=True` (default): Code contexts produce well-anchored memories (git, files, tests, logs). Keep Guard 5 enabled to refuse synthetic-only candidates.
- `enable_fact_edges=False` (default): Fact edges are under-utilized in code contexts. When enabled, use `fact_edge_mode="pointer"` (default) to hydrate endpoint nodes rather than terse facts.
- `similarity_scale="rrf"` (default): Safe default; "normalized" has parity but is better validated on diverse corpora before a global default flip.

---

## Anecdotal/Personal-Memory Profile

**Use for**: conversation logs, personal episodic memory, LongMemEval-like corpora where memories are extracted from chat.

```python
RetrievalTuningConfig(
    enable_evidence_anchor=False,      # REQUIRED: no external anchors in anecdotal data
    enable_fact_edges=True,            # "What happened" queries need fact edges
    fact_edge_mode="pointer",          # Hydrate endpoint nodes, not terse facts
)
```

**Why these settings:**

- `enable_evidence_anchor=False` (FOOTGUN if omitted): Personal conversation has no code/test/git anchors — every recalled fact is LLM-extracted from chat. Guard 5 refuses entire result sets unless this is `False`. **Do not omit.**
- `enable_fact_edges=True`: Episodic queries ("what did we talk about?", "when did X happen?") retrieve answers from `EntityEdge.fact` fields. This is the measured win for episodic corpora (LongMemEval 2026-07-04, N=500).
- `fact_edge_mode="pointer"` (default): Measured net-NEGATIVE for "standalone" mode (0.300→0.033, N=30); "pointer" preserves node richness and fixes ranking.

---

## Flag Status: `similarity_scale`

The `similarity_scale` flag (default `"rrf"`) governs the scale contract for the additive relevance formula:

- `"rrf"` (default today): Graphiti's RRF reranker score (~[0, 2.0]) feeds directly into the additive formula, mixing with [0, 1] SOURCE_PRIORS. Measured on LongMemEval (N=500) as byte-identical to `"normalized"` but holds default pending code-workspace validation.
- `"normalized"` (the planned correction): Divides VECTOR/BM25/FACT_EDGE scores by the pinned RRF max so similarity becomes [0, 1] and `PENDING=1.0` regains its intended top-pin. Measured parity on LongMemEval (143/500, 0.2860 for both modes). Flip rule: ≥parity and multi-corpus check (code-workspace pending as of 2026-07-04).

See `memory-retrieval-under-uncertainty.md` §3 (scale-coupling law) and `.agent/plans/retrieval-scale-contract-and-gap-remediation.md` (Part 1b).

## Score metadata contract

Recall outputs identify the raw retrieval lane without changing ranking: `graphiti_rrf`, `weighted_rrf_normalized`, `source_prior`, or `fact_edge_rrf`. `retrieval_score` is that raw lane; the old `breakdown.semantic_similarity`, `relevance`, and the 0.15 floor remain for compatibility and carry `relevance_basis=legacy_rrf_threshold_unvalidated`. With `trace=true`, default fused retrieval also shadows per-method BM25/cosine ranks observe-only.

---

## Read Next

- **Tuning detail**: `src/menhir/domain/retrieval_tuning.py` — full flag documentation and defaults.
- **Scoring mechanics**: `services/scoring_service.py` — additive relevance formula, floor gates.
- **Scale contract**: `.agent/plans/retrieval-scale-contract-and-gap-remediation.md` — Part 1a/1b design + measurement.
