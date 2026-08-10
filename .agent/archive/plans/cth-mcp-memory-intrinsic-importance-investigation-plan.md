# Investigation Plan: Intrinsic Importance Scoring for Recall

**Date:** 2026-06-15
**Status:** INVESTIGATION (not an implementation plan — produces a go/no-go + a follow-up build plan)
**Scope:** `cth.mcp.memory` recall scoring. Read-only analysis + one offline spike + one measurement
harness. No production scoring change lands from this plan.
**Origin:** Cross-pollination from archolith Phase 4, which adopted generative-agents scoring
(`recency x importance x relevance`). cth.mcp.memory grades on
`similarity + α·adjacency + β·recency + γ·prominence + δ·conflict + type_boost`
(`services/scoring_service.py`) — it has recency and relevance but **no intrinsic, per-memory
importance**. It approximates importance via `prominence` (edge count) and `type_boost` (currently
0.0 for every type). Hypothesis: that approximation has a cold-start ranking failure worth fixing.

---

## Hypothesis

**H1 (the problem is real).** Brand-new high-value memories rank too low at recall time because the
only importance-like signals are emergent: `prominence` needs edges that don't exist yet, and
`type_boost` is uniformly 0.0. A freshly-stored production blocker can be out-ranked by stale,
well-connected trivia.

**H2 (the fix helps).** Adding an intrinsic `importance` score (generative-agents "poignancy",
rated by the LLM that already runs during enrichment) and folding it into the formula as a weighted
additive term materially improves ranking for the queries that matter, without regressing existing
good rankings.

The investigation either substantiates both with evidence and emits a build plan, or kills the idea
cheaply.

---

## Open questions (this plan answers these — it does not assume them)

1. **Is the cold-start failure material, or theoretical?** How many resident memories sit at
   near-zero prominence? On real recall queries, do recent/low-edge important memories actually land
   below the top-K? Quantify before building anything.
2. **Can the enrichment LLM produce a *stable, cheap* importance rating?** Variance across re-runs,
   added tokens/latency, and whether a 1–10 poignancy prompt discriminates (not everything clustering
   at 5). Failure here kills H2 regardless of H1.
3. **Where does importance live and does it survive the round-trip?** Graphiti → Neo4j node schema,
   the `CandidateData` fetch (`domain/recall.py`), and the `fetch_candidate_metadata` Cypher. Is there
   a clean field, or does it need a migration?
4. **How do we even measure ranking quality?** There is *no* recall-quality benchmark today —
   `scripts/profile_recall.py` measures *latency only*. We need a small labeled probe set before we
   can claim "better."
5. **What ε weight, per preset, and does it regress?** Re-scoring with importance must not push good
   structural/recent results out of the top-K for the presets that shouldn't care
   (`recent`, `connected`).
6. **Backfill story.** Existing memories have no importance. Neutral default (0.5, nothing re-ranks)
   vs. a one-time batch rating pass — cost and value of each.

---

## Investigation lanes

### Lane A — Quantify the problem (read-only, against live graph)
- Extend a throwaway query (pattern from `scripts/profile_recall.py`) to histogram `edge_count`
  (prominence input) across resident `Entity` nodes, split by `memory_type` and `last_accessed_days`.
- Pull 10–15 real recall queries (from memory of actual sessions / the bootstrap queries) and, using
  the existing `ScoringService`, record where known-important-but-new memories rank. Capture concrete
  examples of inversion (stale trivia above fresh blocker) or prove they don't occur.
- **Exit evidence:** a short table — "% of resident memories with edge_count ≤ 1", and ≥3 concrete
  ranked-query examples showing (or refuting) the inversion. If no inversion exists, **stop — H1 fails.**

### Lane B — LLM importance feasibility spike (offline, no writes)
- Draft a poignancy prompt (1–10, with anchored examples). Run it over ~30 existing memory contents
  via the same LLM client enrichment uses (`ctx.llm`).
- Measure: score distribution (does it discriminate?), test-retest variance on a 10-memory subset,
  added tokens/latency per memory.
- **Exit evidence:** distribution + variance numbers + per-memory cost. **Stop if** scores don't
  discriminate or variance is high (unstable signal) or cost is non-trivial on the enrichment path.

### Lane C — Offline re-scoring spike (the cheap proof of H2)
- **No schema change.** In a scratch script, load the Lane-A candidates, inject a *mock* importance
  (use Lane-B ratings where available, else a hand-labeled value), and re-run `ScoringService` math
  with an added `+ ε·importance` term across a sweep of ε and preset weights.
- Compare top-K before/after against the Lane-A "should-rank-high" labels.
- **Exit evidence:** for which ε/preset does importance fix the inversions *without* displacing
  already-good results? A single plot/table. This is the decision-maker for H2.

### Lane D — Data-model & measurement feasibility (read-only design)
- Trace the write path (`core/backend_impl.py` add_memory → Graphiti episode → Entity node) and the
  read path (`fetch_candidate_metadata` Cypher → `CandidateData` → `ScoringService`) and document the
  *exact* minimal touch points an importance field would need. No code — just the anchored map.
- Specify a **reusable recall-quality probe**: a small fixture of (query, expected-top memories) that
  any future change can be scored against. This is the missing benchmark; defining it is a
  deliverable even if importance is rejected.

---

## Evidence to collect (the investigation's output artifact)

A findings note (`.agent/reviews/cth-mcp-memory-intrinsic-importance-findings.md`) with:
- Lane A: prominence histogram + ≥3 ranked-query inversion examples (or proof of none).
- Lane B: poignancy distribution, retest variance, per-memory token/latency cost.
- Lane C: ε/preset sweep table — does `+ ε·importance` fix inversions without regression?
- Lane D: the anchored write/read touch-point map + a defined recall-quality probe fixture.

---

## Decision gate (go / no-go)

**GO (write the build plan)** only if ALL hold:
- Lane A shows a real, recurring inversion (not a one-off).
- Lane B shows the LLM rates poignancy stably (low retest variance), discriminately, and cheaply.
- Lane C shows an ε/preset setting that fixes inversions with no meaningful regression on the
  unaffected presets.

**NO-GO (kill it)** if importance doesn't discriminate, is unstable, the inversion is rare, or
`prominence` already handles it once the graph is warm and the cold-start window is short enough not
to matter.

**If GO**, the follow-up build plan (separate artifact) is the already-scoped change:
`importance: float` (0–1, default 0.5) on the node + `CandidateData`; poignancy rating wired into the
existing enrichment LLM pass; `+ ε·importance` in `ScoringService`; `ε` added to `PRESET_WEIGHTS`
(heavy on `knowledge`, light on `recent`/`connected`); `RelevanceBreakdown` field for explainability;
optional decay tie-in (high importance resists compression like `user_flagged`); backfill = neutral
default, optional batch rating. All behind a config-gated ε so it can ship dark and A/B on the
Lane-D probe.

---

## Risks / notes
- **Measurement gap is the real blocker.** With no quality benchmark, "better" is unfalsifiable —
  Lane D's probe fixture is the load-bearing deliverable; do it even on a NO-GO.
- **Double-counting.** importance + prominence + type_boost may overlap; the sweep (Lane C) must check
  importance adds signal rather than re-weighting what prominence already captures.
- **Enrichment cost creep.** Importance rating rides the existing LLM pass — confirm it adds no extra
  round-trip (one combined prompt), or H2's cost case weakens.
- **Scope fence:** this plan writes only throwaway scripts + a findings note. No change to
  `scoring_service.py`, `recall.py`, `memory_types.py`, schema, or enrichment lands here.
