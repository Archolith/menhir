# Shadow Context Composition — Semantic-Similarity Validation Experiment Results

**Date:** 2026-07-16
**Scope:** validation experiment run in response to
`.agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md`, which found
Stage 1's exact-string-match label join never selects a real candidate (9/9 real runs abstained).
**Proposal tested:** replace the LLM-label join with `similarity(embed(current_message),
embed(candidate_fact_text))`, scored directly, before touching production code.
**Status:** offline validation only. Nothing in `src/menhir/services/shadow_context_composition.py`
or `src/menhir/infrastructure/llm.py` (the real production shadow pipeline) was changed. This is
the "First, make Stage 1 produce a useful precision/recall curve" step, not Stage 2.

## Method

- **Module:** `src/menhir/explorer/shadow_semantic_similarity_lab.py` (pure logic: fixture
  assembly, cosine similarity, precision/recall sweep) + `scripts/run_shadow_semantic_similarity_lab.py`
  (real embedding calls via `GraphitiClient.embed_query`, the exact same production embedder —
  `text-embedding-3-small`, 1536-d — used elsewhere in the pipeline).
- **Ground truth:** reused, not hand-authored fresh. The 21 already-reviewed eligibility scenarios
  from Phase 4b (`src/menhir/explorer/extraction_lab_eligibility_fixtures.py`) carry a real
  `correct_candidate_id` and `DecoyType` per candidate — this gives confirmed positives and 5
  distinct negative categories for free, already validated in an earlier phase of this
  investigation:
  - `positive` — the fixture's designated correct candidate (12 rows: `c1` recurs across each
    family's scenarios)
  - `wrong_state_family` — same subject, different specific topic/state (the closest real analogue
    to "same-entity-wrong-facet")
  - `wrong_scope`, `wrong_subject`, `missing_metadata`, `stale` — the remaining `DecoyType` values
  - `cross_family_control` — each family's own message cross-paired against every *other* family's
    candidates (36 rows) — three real, topically unrelated (message, graph-content) pairs, not
    synthetic filler
- **69 total (message, candidate) rows, 21 unique texts embedded** (messages repeat 3x per family;
  a few candidates like `c1` recur verbatim across scenarios in the same family — deduped before
  the real API calls).

## Result: category similarity summary

| Category | n | mean | min | max |
|---|---|---|---|---|
| stale | 3 | 0.4457 | 0.4431 | 0.4495 |
| **positive** | 12 | **0.4429** | 0.4342 | 0.4537 |
| wrong_state_family | 6 | 0.4098 | 0.2595 | **0.5794** |
| wrong_subject | 3 | 0.3417 | 0.2848 | 0.3976 |
| missing_metadata | 3 | 0.3348 | 0.2846 | 0.4123 |
| wrong_scope | 6 | 0.2939 | 0.2749 | 0.3240 |
| cross_family_control | 36 | 0.1479 | 0.0650 | 0.3259 |

## Result: precision/recall curve (best-F1 point)

**t=0.425 → P=0.706, R=1.000, F1=0.828** (TP=12, FP=5, FN=0). Full curve near the cliff:

| threshold | precision | recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| 0.350 | 0.571 | 1.000 | 0.727 | 12 | 9 | 0 |
| 0.400 | 0.667 | 1.000 | 0.800 | 12 | 6 | 0 |
| **0.425** | **0.706** | **1.000** | **0.828** | 12 | 5 | 0 |
| 0.450 | 0.667 | 0.333 | 0.444 | 4 | 2 | 8 |
| 0.475+ | — | 0.000 | 0.000 | 0 | 2 | 12 |

Full 41-point curve and per-row scores in `results/shadow_semantic_similarity_lab.json`.

## Findings

**1. Cross-family separation is clean and large — the core premise holds.** Unrelated content
scores a mean of 0.148 (max 0.326) against on-topic content's ~0.44 floor — roughly a 0.3 margin.
Direct message↔fact similarity reliably tells "totally unrelated topic" from "plausibly relevant,"
which is exactly the discriminatory power the exact-match label join never demonstrated on real
data.

**2. `stale` is statistically indistinguishable from `positive` by content alone (0.4457 vs.
0.4429 mean) — expected, and not a flaw in this approach.** A stale fact restates the same real-
world topic with an outdated value ("Rachel used to live in Denver *before* moving to Chicago" vs.
"Rachel previously moved to an apartment in Chicago") — nearly identical surface text, different
world-time validity. This confirms the two-filter composition the earlier facet-instability report
already recommended is the right shape: **semantic similarity finds topical relevance;
`domain/temporal.py`'s bitemporal filter (already working independently, confirmed in the
facet-instability report) is responsible for excluding staleness.** Neither filter should try to do
the other's job.

**3. No single threshold achieves both perfect precision and perfect recall — one hard negative
proves it, concretely.** The `wrong_state_family` decoy `lme-2698e78f`'s `c2` ("The user has been
discussing setting healthy boundaries with Dr. Smith") scores **0.5794 — higher than every single
positive row (max 0.4537)**. Its query message literally contains the word "boundaries" ("speaking
of boundaries, I see Dr. Smith every week"), so the decoy's lexical overlap with the message
inflates its similarity past the entire positive band. This is exactly the near-miss case the
report anticipated needing a second-stage judge for — a same-entity, wrong-specific-topic decoy
that shares surface vocabulary with the query. No threshold choice fixes this; the decoy sits
strictly above the positive ceiling, so raising the threshold to exclude it also excludes every
real positive.

**4. `wrong_scope`, `wrong_subject`, `missing_metadata` separate reasonably well** (0.28–0.41 mean,
mostly below the ~0.43 positive floor), though `missing_metadata`'s max (0.4123, from
`lme-2698e78f`) and `wrong_subject`'s max (0.3976) both encroach on the lower edge of the positive
band closely enough to produce some of the best-F1 point's 5 false positives.

## Practical read

Direct message↔fact embedding similarity is a real, working, substantially better signal than the
exact-label-match approach it's meant to replace — it separates unrelated content cleanly and
achieves perfect recall at a reasonable precision. But it is **not sufficient alone**: at least one
concrete, real (not synthetic) near-miss case beats every true positive on pure similarity, and no
single global threshold resolves it. This matches the report's own proposed shape — similarity as
the primary signal, with a second-stage judge for the ambiguous band — rather than a single-
threshold replacement.

**Proposed threshold bands** (derived from this data, not yet implemented anywhere):
- **below ~0.35** (safely under `cross_family_control`'s max of 0.326 with margin, and under most
  `wrong_scope`/`wrong_subject` scores): confidently reject, no LLM call needed —
  `semantic_score_below_threshold`
- **~0.35 to ~0.46** (spans the gap where `wrong_state_family`'s worst case and the positive band
  both fall): route to an LLM judge rather than decide by threshold alone —
  `semantic_ambiguity` → `llm_judge_selected` / `llm_judge_rejected`
- **above ~0.46**: confident accept on similarity alone — `semantic_match` (though note even this
  band isn't fully safe against the single `lme-2698e78f` outlier at 0.5794 — that specific case
  would still need the LLM judge or a scope/entity-level guard, since it scores *above* this band)

These reason codes are proposed, matching the shape requested, but not implemented in any
production or lab code yet — this doc is the evidence they'd be built from, not the
implementation.

## Caveats

- Small sample: 12 positive rows (only 3 underlying unique positive candidate texts, since `c1`
  repeats verbatim across each family's scenarios), 57 negative rows across 5 categories. The
  threshold estimates are directionally solid (the cross-family margin is large and unambiguous)
  but not precise enough to commit to exact cutoff values without a larger or more diverse fixture
  set.
- All 3 families share a similar register (first-person conversational messages, short factual
  candidate sentences) — real production traffic covers more varied phrasing than these 3 fixture
  families alone.
- This experiment did not test the interaction between the proposed similarity filter and the
  existing `not_known_at_reference_time` bitemporal filter in combination — finding 2 above argues
  they compose correctly in principle, but that hasn't been run end-to-end.

## Recommendation (per direct instruction: do not proceed to Stage 2 yet)

Not yet done, in priority order:
1. Confirm this holds on a larger/more diverse fixture set (more families, more phrasing variety)
   before trusting the threshold bands.
2. Prototype the LLM-judge second stage for the ambiguous band, using the `lme-2698e78f` "boundaries"
   case as the concrete test the judge must get right.
3. Only then wire an embedding-based candidate-side signal into
   `src/menhir/services/shadow_context_composition.py` (still shadow/observe-only — this stays
   inside Stage 1, not a Stage 2 change), replacing or augmenting the exact-match label join with
   the two-tier (similarity threshold + judge-for-ambiguous-band) design, and re-run the real broad
   smoke (`scripts/smoke/shadow_context_composition_broad_smoke.py`) to confirm actual selections
   now occur on real traffic without admitting the deliberate unrelated control.

## References

- `.agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md` — the finding
  this experiment responds to
- `.agent/plans/menhir-context-composition-production-integration.md` — Stage 1 spec and execution
  result
- `src/menhir/explorer/shadow_semantic_similarity_lab.py` — fixture assembly + pure logic
- `scripts/run_shadow_semantic_similarity_lab.py` — runner (real embedding calls)
- `results/shadow_semantic_similarity_lab.json` — full raw per-row scores + 41-point curve
- `src/menhir/explorer/extraction_lab_eligibility_fixtures.py` — reused ground-truth fixtures
