# Plan: retrieval scale contract + gap remediation

> **ARCHIVED — DONE (2026-08-08, curator audit).** All 5 parts verified shipped: Part 1a/1b
> (`GRAPHITI_RRF_DUAL_METHOD_MAX` scale pin, `similarity_scale` config — `scoring_service.py`),
> Part 2 (edge-weight cap), Part 3 (`view_kind`-gated dedup in
> `context_builder._deduplicate`), Part 4 (`fact_edge_mode` default flip), Part 5
> (`import os` at module level + swallowed wiki-fetch exceptions logged at debug in
> `context_builder.py`). The "Parts 3+5 owned by another agent this wave" line below was stale
> — both landed. Kept for its design rationale (§1 scale-defect analysis).

**Status: IN PROGRESS 2026-07-04. Part 1a DONE, Part 1b DONE (measured; default held at "rrf");
Part 2 DONE (edge-weight cap pinned 5.0/+0.1, decay protection documented, 2026-07-04);
Part 4 DONE (fact_edge_mode default flipped to "pointer", retrieval-profiles.md added, 2026-07-04);
Parts 3+5 owned by another agent this wave.**
Remediation for the now-worthy findings of the 2026-07-03 retrieval-side gap review. Design
authority: `.agent/memory-retrieval-under-uncertainty.md` (§3 scale-coupling law, §4b
self-reinforcing relevance, §4e context collapse, §6 promotion discipline). Independent of the two
perception plans; read-path only.

Part 1a landed 2026-07-04: `GRAPHITI_RRF_DUAL_METHOD_MAX=2.0` contract constant + rank_const=1
regression pin (`test_graphiti_rrf_scale_contract`), `SOURCE_PRIORS` docstring records the
PENDING=1.0 mid-rank accident, `memory-policy.md` floor described as a rank cut. No live-formula
numeric moved -> byte-identical (unit suites green). Part 1b landed
`RetrievalTuningConfig.similarity_scale="rrf"|"normalized"` (default "rrf" byte-for-byte); normalized
mode divides search scores by the pinned max at the recall boundary (search_scored path only; guarded
`not enable_bm25` since weighted_rrf is already [0,1]) and scales the floor to 0.075.

Part 1b A/B (2026-07-04, LongMemEval oracle slice, N=500, recall-only, llm-judge gpt-4o answers /
gpt-4o-mini grader): rrf 143/500 (0.2860) vs normalized 143/500 (0.2860) -- EXACT parity, 18 items
churned symmetrically (9 win / 9 lose), so the flag genuinely reranks with zero net effect on this
corpus. Meets the >= parity flip rule, BUT default held at "rrf": LongMemEval under-exercises the
PENDING/FILE_LINKED priors this change most affects (pure recall, no fresh-ingest or file-context at
query time), so a GLOBAL default flip is not claimed on one anecdotal corpus's parity. Flag + numbers
recorded at the flag site; revisit the flip after a code-workspace-corpus check. Consistent with the
campaign finding that every read-side ranking lever lands neutral-to-negative -- the score is gated by
recall COVERAGE (~24% of answers aren't single entities), a write-side aggregation problem, not
ranking. 1b's real payoff is the honest scale contract + unblocking hybrid_alpha.

## Part 1 — the scale contract (the load-bearing fix)

### The defect cluster (one root cause, three symptoms)
The additive relevance formula and its floor assume one scale; the generators don't provide one.
- `MIN_SIMILARITY_THRESHOLD = 0.15` gates graphiti's RRF **rank** score (dual-method top ≈ 2.0),
  not a cosine — "reasonable only by luck of rank_const=1" (`scoring_service.py:47-63`).
- `SOURCE_PRIORS` declares itself "on the cosine [0,1] scale the scoring floor uses"
  (`retrieval_tuning.py:50`) and pins `PENDING` to 1.0 as "the top" — but the VECTOR scale it
  competes against tops out ~2.0, so the intended top-pin silently became a mid-rank prior.
- `memory-policy.md` still documents a "minimum similarity threshold (0.15)" and relevance tiers
  "derived from raw semantic similarity" — doc drift over the same root cause.
This also blocks planned work: `hybrid_alpha` is "a seam, not a tuned value" pending exactly this.

### The fix — REVISED 2026-07-03 (executor found the original framing self-contradictory)
The original Part 1 demanded both "normalize the similarity lane to [0,1]" and "invisible to
ranking" — impossible under the additive formula: rescaling ONE lane while the bonus weights stay
fixed reorders any pair where higher similarity meets lower bonuses, and restoring `PENDING=1.0`
to a genuine top-pin is itself a ranking change. Floor *membership* can be preserved exactly;
ordering cannot be simultaneously "a real fix" and "invisible". §6 of the retrieval doc governs:
no ranking change ships by argument. So Part 1 splits:

**1a — the contract (truly invisible; land now).** No numeric value that feeds the live formula
changes.
1. **Pin the scale with a regression test**: assert `search_scored`'s observed scale (rank_const=1,
   dual-method top ≈ 2.0) against the graphiti client, so an upstream change fails loudly instead
   of silently rescaling recall.
2. **Document reality, not aspiration**: the floor comment and `memory-policy.md` describe the
   0.15 threshold as a rank cut on the RRF scale; the `SOURCE_PRIORS` docstring is corrected to
   state that `PENDING=1.0` currently sits mid-rank against VECTOR's ~2.0 top (the accident,
   recorded as fact, marked as pending 1b). Tier-label sentence corrected.
3. Verification: byte-identical recall results on a recorded fixture set — achievable because
   nothing numeric moved.

**1b — the semantic normalization (a ranking change; §6 ladder, no exceptions).**
1. `RetrievalTuningConfig.similarity_scale: "rrf" | "normalized"` (default `"rrf"` == today,
   byte-for-byte). `"normalized"` divides VECTOR/BM25/FACT_EDGE search scores by the pinned max
   (2.0), making `PENDING=1.0` a genuine top-pin again; the floor becomes 0.075 under this mode
   (identical membership by construction); lane weights untouched so the A/B measures the honest
   ordering change.
2. Measure on the bench oracle slice. Pre-registered rule: flip the default at **≥ parity**
   overall — this is a correctness/contract fix, not a win-seeking tune, so parity suffices; a
   regression blocks the flip and the numbers get recorded at the flag site either way.
3. `hybrid_alpha` tuning remains sequenced after 1b's default flips.
4. **Fallback-scale note (from the 2026-07-04 graphiti_client read):** `search_scored` silently
   degrades to BM25-only on a vector-dimension mismatch, where the single-method RRF top is 1.0,
   not 2.0 — so normalized-mode scores halve in fallback and the floor bites proportionally
   harder. Acceptable (fallback is an already-degraded mode), but the 1b A/B should exclude or
   flag fallback-mode recalls; also note two RRF conventions coexist by design (`search_scored`
   opaque rank_const=1 vs `hybrid_retrieval` k=60 min-normalized-to-1.0 — the latter is already
   scale-clean).
5. **SemanticOracle clamp artifact (07-04):** the oracle path clamps the injected similarity to
   [0,1] (`retrieval_oracles.SemanticOracle`, `min(1.0, …)`) — on the raw RRF scale every score
   ≥1.0 saturates, erasing top-rank distinctions for oracle ranking. 1b's normalized mode
   dissolves this for free; until then, oracle-ranking A/Bs should know top-2 candidates are
   indistinguishable to the SemanticOracle.

## Part 2 — reinforcement loop: record the truth, pin the cap

Verified 2026-07-03: `increment_edge_weight` is **capped at 5.0** (+0.1/traversal,
`consolidation_queries.py:69-86`) — bounded, not compounding. **No edge-weight decay exists**;
`memory-policy.md`'s "accumulates from use, then decays over time" is aspiration — v1 deliberately
ties edge lifecycle to endpoints. The live loop is *lifecycle*, not ranking: `last_accessed`
touches on every recall protect returned nodes from decay indefinitely, so retrieval chooses what
survives (rich-get-richer via decay protection, by design but undocumented).
1. Unit test pinning the 5.0 cap and the +0.1 step (a silent uncap would reopen §4b).
2. Correct `memory-policy.md` edge-weight wording to v1 reality (capped ratchet, no decay,
   deliberate); name the decay-protection loop explicitly as a designed trade.
3. Note in `.agent/memory-retrieval-under-uncertainty.md` §4b that the cap is verified and decay
   is a v1 non-goal.
4. Backlog (not this plan): traversal-weight decay when edge lifecycle decouples from endpoints.

## Part 3 — Jaccard dedup characterization (the seam protector)

`context_builder._deduplicate` collapses results at >0.8 word-set Jaccard. Counter-View surfaces
are short; short-set Jaccard is noisy; a View can collapse against a lexically-similar episode and
lose to the higher scorer — erasing the write side's payoff at the last step.
1. Characterization test: a counter View surface ("user bike_spend = 185"-shaped, invented domain)
   vs an episode narrating the same numbers; assert both survive packing.
2. If it collapses: **never dedup across claim shapes** — a View (`view_kind` present) may only
   dedup against another View with the same `view_key`. Requires `is_view`/`view_kind` to reach
   `ScoredMemory` (it is already in candidate metadata; thread it through).

## Part 4 — config truth (small, prevents footguns)

1. **Flip `fact_edge_mode` default to `"pointer"`** (`retrieval_tuning.py:155`). The default is
   currently `"standalone"` — the arm measured net-NEGATIVE (0.300→0.033, N=30) — so whoever flips
   `enable_fact_edges` today gets the known-bad mode. The measurement stays in the docstring.
2. **Per-corpus retrieval profiles note** (new `.agent/retrieval-profiles.md` or a section in
   `tasks-mcp.md`): the flag sets for code-workspace vs anecdotal corpora, led by the Guard-5
   footgun (`enable_evidence_anchor=True` refuses entire anecdotal result sets — today documented
   only in a dataclass comment).

## Part 5 — small hardening (do last, or drop if time-boxed)
- `context_builder` wiki section: log swallowed exceptions at debug, cap file reads as today, and
  move the `import os` to module level. No behavior change intended.

## Explicitly NOT in scope (decided, not forgotten)
- A6 View injection and any lens→source routing — separate decision, gated on reachability data.
- Retuning the floor, hybrid_alpha, preset weights, or the 0.8 Jaccard threshold — §8 of the
  write-side doc applies: no tuning to the measurement; Part 1 is behavior-preserving by contract.
- Edge-weight decay implementation (backlogged in Part 2).

## Verification
1. Part 1a: recorded candidate sets (mixed sources, invented content) produce byte-identical
   results before/after; the scale-pin test fails if graphiti's rank_const changes. Part 1b:
   flag-off path byte-identical; flag-on measured on the oracle slice with the pre-registered
   parity rule.
2. New tests green: cap pin (Part 2), dedup characterization (Part 3); full retrieval test suite
   green and untouched tests unmodified.
3. Docs: `memory-policy.md` scoring + edge sections match code; profiles note exists; anchor doc
   §4b annotated.
4. One traced benchmark recall run: identical result ordering pre/post Part 1a on the same graph
   (1a is invisible by construction; 1b is deliberately NOT invisible and is judged by its A/B,
   never by argument).
