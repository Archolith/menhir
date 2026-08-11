# R1 — Hybrid candidate generation + source-aware priors

**Date:** 2026-06-27
**Status:** IN PROGRESS — first increment landed (attributed hybrid path + source-aware floor,
default-off). Bench tuning of `hybrid_alpha` and config wiring deferred (see Scope).
**Rung:** R1 in [`menhir-research-execution-ladder.md`](../../research/menhir-research-execution-ladder.md) (`depends_on R0`).
**Owners (mechanism):** [`retrieval-tuning-stack.md`](../../../docs/research/retrieval/retrieval-tuning-stack.md),
[`oracle-execution-and-performance.md` §3](../../../docs/research/retrieval/oracle-execution-and-performance.md).

## Why

- Code recall depends heavily on **exact strings** (error messages, symbols, file paths, commit
  hashes). Vector similarity is weak for those; lexical/BM25 is strong.
- BM25 already ran in the recall path — but **fused** with cosine inside graphiti's RRF reranker
  (`graphiti_client.search_scored`), producing one opaque score with **no source attribution** and
  **no tunable blend**. You could not tell a BM25 hit from a vector hit, so you could not protect
  exact-match candidates from the semantic floor.
- The semantic floor (`MIN_SIMILARITY_THRESHOLD = 0.15`) silently drops anything below a cosine
  cutoff. A strong exact-match hit with low semantic similarity could be discarded.
- Source-aware priors already existed **ad hoc**: `PENDING_ENTITY_SIMILARITY = 1.0` and
  `FILE_LINKED_BASELINE_SIMILARITY = 0.3` were injected straight into the similarity map to clear
  the floor. R1 generalizes that hack into an explicit, typed mechanism rather than inventing one.

## Scope

**In (this increment):**
- Explicit `CandidateSource` + per-source `SOURCE_PRIORS` (formalizes the `1.0`/`0.3` constants).
- Attributed hybrid candidate generation: vector and BM25 as **separate labeled passes**, blended
  by **weighted reciprocal-rank fusion** with a tunable `hybrid_alpha`.
- **Source-aware floor**: the cosine floor gates only `VECTOR` candidates; BM25/pending/file-linked
  clear via their source.
- Feature-flagged behind `RetrievalTuningConfig.enable_bm25`, **default off** — today's behavior is
  byte-for-byte unchanged until a caller opts in.

**Out (deferred):**
- Tuning `hybrid_alpha` to a non-neutral value — needs archolith-bench (ladder rule 2). It ships as
  a seam at `0.5`, not a tuned value.
- Query-adaptive alpha (shift toward lexical when the query looks like a symbol/error string).
- Wiring `RetrievalTuningConfig` through settings / MCP / API (currently a `recall()` kwarg only).
- Facet source (R2), cross-encoder rerank (R10), embedding-dimension sweep.

## Proposed Design (as built)

- **`domain/retrieval_tuning.py`** (new): `CandidateSource` enum, `SOURCE_PRIORS`,
  `FLOOR_EXEMPT_SOURCES`, `RetrievalTuningConfig` (validates `hybrid_alpha ∈ [0,1]`).
- **`services/hybrid_retrieval.py`** (new): pure `weighted_rrf(vector_hits, bm25_hits, alpha)` +
  async `hybrid_search(...)`. Fuses on **rank**, not raw score, so `alpha` is well-defined despite
  BM25 and cosine living on incomparable scales. A candidate found by BM25 is attributed
  `BM25` (floor-exempt) even if also a vector hit — an exact match is never floored.
- **`infrastructure/graphiti_client.py`**: new `search_ranked_by_method` runs each method as its own
  pass and returns per-method ranked hits (mirrors `search_scored`'s structure + dimension-mismatch
  handling). `search_scored` is unchanged and remains the default path.
- **`domain/recall.py`**: `CandidateData` gains `source: CandidateSource = VECTOR`.
- **`services/scoring_service.py`**: floor filter becomes source-aware.
- **`services/recall_service.py`**: builds a `source_map`; branches candidate generation on
  `tuning.enable_bm25`; pending/file-linked priors now read from `SOURCE_PRIORS`.

Data-flow change: candidate generation can now emit candidates from two attributed passes instead of
one fused pass; each candidate's origin travels with it to the floor.

## Alternatives Considered

- **A: replace RRF with a linear `alpha*cosine + (1-alpha)*bm25` blend.** Rejected — BM25 and cosine
  scores are on different scales, so a linear blend is ill-defined without per-method normalization.
  Weighted RRF fuses on rank and avoids the problem.
- **B: keep injecting source-specific similarity values (status quo).** Rejected as the destination —
  it is exactly the `similarity` overload R1 exists to remove. Kept only as the ranking prior for
  pending/file-linked (their similarity still feeds the relevance formula), now sourced centrally.
- **C: move filtering downstream of oracle scoring.** Deferred — oracles do not exist until R4–R7.

Chosen: explicit source + prior (option 4) **plus** source-aware floor (option 2). Generalizes the
existing precedent and removes the overload while leaving a clean seam for R4.

## Risks

- **Score-scale (open).** Graphiti is not importable in the dev sandbox, so the absolute scale of the
  normalized fused `similarity` vs the `0.15` floor for **VECTOR-only** candidates in the hybrid path
  is unverified. Mitigated: floor exemption is **source-based**, so BM25/injected correctness does
  not depend on the scale. Must be confirmed against live graphiti / a bench trace before promotion.
- **Latency.** Hybrid mode issues two search passes instead of one. Only on the opt-in path; a pure
  alpha (0 or 1) skips the unused pass.
- **Ranking drift.** In hybrid mode `similarity` is a normalized fused score, not cosine, so the
  relevance-formula leading term differs from the default path. Acceptable for an opt-in, bench-gated
  mode; the default path is untouched.

## Invariants (preserved)

- CANDIDATE exclusion, namespace filtering, GONE/SESSION filtering — unchanged in `recall_service`.
- Vector noise floor still gates VECTOR candidates (regression-tested).
- Determinism: `weighted_rrf` sorts by fused score with stable, first-appearance tie-breaks.
- Default-off ⇒ `search_scored` path and its tests are unchanged.

## Validation

- **Unit (pure, verified in sandbox):** `weighted_rrf` (symmetric / pure-alpha / determinism /
  limit / empty), `RetrievalTuningConfig` validation, source-aware floor (vector floored, BM25
  exempt), end-to-end fusion→scoring exemption.
- **Unit (written, run in CI — sandbox lacks the private `cth-mcp-framework` dep so pytest cannot
  collect here):** `tests/test_hybrid_retrieval.py`, additions to `tests/test_scoring_service.py`
  and `tests/test_recall_service.py` (split-search routing, BM25-only survives floor, default path
  unchanged). Stub gained `search_ranked_by_method`.
- **Bench (next):** archolith-bench ladder A–E on the R1 fixture families; headline =
  `exact_string_recall`/`symbol_recall` up without regressing `stale_hit_rate`/
  `wrong_scope_injection_rate`. Requires R0 traces emitting per-candidate `source`/`prior`.

## Docs To Update

- `.agent/CHANGELOG.md` (this change) — done.
- Ladder: flip R1 status `planned → in-progress` when the bench lands.
- `architecture.md` / `endpoints.md`: update when `RetrievalTuningConfig` is threaded through the
  MCP/API surface (deferred).
