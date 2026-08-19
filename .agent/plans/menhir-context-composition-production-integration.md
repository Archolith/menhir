# Menhir Context-Composition Production Integration — Handoff Plan

Status: STAGE 1 IMPLEMENTED AND SMOKE-TESTED (2026-07-16); Stages 2-4 not started. Written
**Last verified:** 2026-08-18 — CONSISTENT with STAGE 1 IMPLEMENTED. `MemoryFacetSet` 12 hits, `abstained_no_eligible_candidates` 1.

2026-07-16, immediately following the close of the Extraction Lab Phase 1-5 investigation. Stage 1
was built the same day (`.agent/plans/menhir-context-composition-production-integration.md`'s own
Stage 1 section below), committed in `c7d39b0`/`25c2140`/`488bfcc`, and run against real ingest
traffic — see "Stage 1 execution result" below for what that run actually found.

## Evidence base

This plan exists because the Recall-Labs-only investigation is finished and its conclusion is
directional, not hypothetical:

- `.agent/archive/plans/../archive/plans/menhir-extraction-context-ablation-handoff.md` — full methodology, all phases,
  exact trial numbers (Phase 1 through Phase 5 items 1-4).
- `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md` — compact RCA, cross-referenced.
- Frozen selector: `select_structured_then_llm` in
  `src/menhir/explorer/extraction_lab_eligibility_selection.py` — hard subject/facet/state_family/
  scope/bitemporal filter before any LLM ranking, LLM consulted only for genuine residual ties.
  420 Phase 4b trials: 100% injection precision / 100% coverage.
- Winning metadata-production approach: `predict_candidate_aware_ranked` in
  `src/menhir/explorer/extraction_lab_metadata_production.py` — grounds classification in real,
  existing `(subject, facet, state_family)` triples instead of an abstract ontology. 30 Phase 5
  item-3 trials: 100% field accuracy, 100% downstream precision/coverage, matching the oracle
  upper bound.
- Corruption matrix (`src/menhir/explorer/extraction_lab_corruption_matrix.py`): the frozen
  selector fails safe on 7/8 realistic metadata corruptions; the one confirmed dangerous case is a
  decoy mistagged with the correct subject — a structural limit of exact-match filtering, not a
  bug, now locked into a regression test.
- Genuine-tie suite (`src/menhir/explorer/extraction_lab_genuine_tie_suite.py`): the LLM
  tie-breaker, exercised for the first time (30/30 calls fired), reliably uses real content
  signals when present and reliably abstains under genuine irreducible ambiguity.

## Final assessment (restated as the production thesis)

The architectural uncertainty is resolved. The fix is not "give Graphiti more memory" — raising
`RELEVANT_SCHEMA_LIMIT` was tested directly (Phase 3) and is empirically unreliable, chaotic
across window sizes. The fix is:

> Construct a small, structurally eligible, source-grounded piece of prior context and present it
> in the native conversational position the extractor already understands.

That is a context-*composition* problem, solved by structured candidate filtering plus grounded
metadata production, not an extraction-*window* problem. Everything below is the integration path
from "proven in Recall Labs on 3 hand-authored fixtures" to "safe in production on real traffic."

This is an integration rung, not another open-ended research phase. Each stage below gates the
next — no stage begins until the prior stage's exit criteria are met on real data.

---

## Stage 1 — Shadow-mode context composition

**Goal:** wire the pipeline into production ingest without changing the extraction input. Zero
risk to the graph; pure observability.

**Mechanism:** production episode ingest already has two natural hook points:
- `IngestService._process_pending_episode` (`src/menhir/services/ingest_service.py:617`) — the
  real per-episode processing entrypoint, where the episode is about to be handed to graphiti-core
  for extraction.
- `src/menhir/infrastructure/graphiti_patches.py` — already the place menhir monkey-patches
  `graphiti_core.prompts.extract_nodes` / `extract_edges` and the entity-extraction flow
  (`_patch_graphiti_entity_extraction`, line 388). A shadow-mode hook belongs alongside these
  existing patches, not as a separate side-channel, so it observes the exact same extraction call
  production makes.

**For every episode, log (not act on):**
- ranked metadata hypotheses (`predict_candidate_aware_ranked`'s output shape: up to 2 ranked
  `(facet, state_family, confidence)` guesses per candidate query)
- candidate facts retrieved (the real graph query results that would feed `eligible_candidates`)
- facts rejected and rejection reasons (`is_eligible` returns False — which of
  subject/facet/state_family/scope/bitemporal failed)
- selected fact or abstention (`select_structured_then_llm`'s `selected_id` / `None`)
- whether the LLM tie-breaker fired (`llm_calls` count — expected near-zero given Phase 4b, worth
  confirming that holds on real traffic and isn't an artifact of hand-authored fixtures)

**Comparison:** log the shadow system's proposed context next to what the existing pipeline
actually extracted for the same episode. No divergence is acted on in this stage.

**Exit criteria to advance to Stage 2:** shadow logs run clean across a meaningful slice of real
ingest traffic (no crashes, no unbounded latency/cost growth, candidate retrieval actually returns
sane pools) and a first look at the comparison data confirms the shadow system's proposals are
directionally sane (not obviously nonsensical) before spending Stage 2's counterfactual-extraction
budget on them.

### Stage 1 execution result (2026-07-16) — exit criteria only partially met

Implementation used real production concepts, not the lab's hand-authored ones: fact-edge-
granularity candidates (`MemoryQueries.fetch_candidate_fact_edges`, new — the existing
`fetch_temporal_facts` collapses multiple competing claims on one entity into indistinguishable
rows), `domain/temporal.py`'s `was_known_at`/`TemporalQuery.AS_KNOWN_AT` reused directly rather
than reimplemented, and explicitly-named `shadow_facet`/`shadow_state_family` labels (never
`MemoryFacetSet` fields — production's real facet system has no domain-topic vocabulary, confirmed
during planning). Full design rationale in `src/menhir/services/shadow_context_composition.py`'s
module docstring. Shadow work runs as a detached background task dispatched only after the
per-namespace ingest gate releases, so it adds zero latency to real episode completion.

**No-crash / bounded-cost half of the exit criteria: met.** 10 real shadow-trace rows recorded
against `menhir-lme-neo4j` (single-episode smoke + a 7-episode/6-namespace broad smoke,
`scripts/smoke/shadow_context_composition_smoke.py` /
`shadow_context_composition_broad_smoke.py`), zero crashes, real extraction confirmed byte-for-byte
unaffected every run. Two real gaps only surfaced under real graph density (not by the unit tests
or an 8-angle code review pass) and were fixed same-day: LLM response truncation past ~10
candidates (max_tokens now scales with candidate count), and failure-path traces losing their
`candidates` list on early exit (fixed — every already-past-retrieval failure path now carries the
real candidates through, since an empty-candidates malformed-response trace is undiagnosable).

**"Directionally sane proposals" half: not yet met — a real, informative negative result.** Across
9 post-fix real runs spanning 6 different LongMemEval namespaces, **9/9 abstained
(`abstained_no_eligible_candidates`), zero selections, zero tie-breaker fires** — including on
topically relevant content (Rachel/housing, 18 real candidates retrieved) and correctly on the one
deliberate negative control. Root cause visible in the raw traces: message-side and candidate-side
`shadow_facet` values are both free-text and mutually inconsistent (e.g. message hypothesis
`shadow_facet="Rachel"`/`"suburbs"` vs. candidate labels `shadow_facet="user"`/`"Nisha's dad"`/
`"Chicago"` for the same episode) — exact-string-match eligibility can't agree with itself. This
directly answers the plan's own "revised Stage 1 question" (can menhir generate *stable* shadow
semantic labels from real fact text): **not yet, not with exact-string matching.** One run at the
`_MAX_CANDIDATE_FACTS` cap (30 candidates) also landed at 29.75s against the 30s timeout budget —
worth watching before raising the cap.

**Practical read:** Stage 1 proved the mechanism is safe to run broadly (the actual bar for
advancing infrastructure), but the eligibility filter as specified has not yet selected a single
real candidate. Stage 2 (counterfactual extraction) should not start on the current exact-match
filter — it would have nothing to counterfactually test. The needed fix is either (a) normalize/
canonicalize shadow labels before comparison, or (b) replace exact-match with embedding or
LLM-judged similarity for the eligibility check, and re-run the broad smoke before reconsidering
Stage 2 readiness.

### Semantic-similarity validation experiment (2026-07-16) — option (b) tested offline, promising but not sufficient alone

Full results: `.agent/reviews/menhir-shadow-semantic-similarity-lab-2026-07-16.md`. Tested
`similarity(embed(message), embed(candidate_fact_text))` as a direct replacement for the
label-match join, offline against 69 (message, candidate) rows built from the 21 already-validated
Phase 4b eligibility fixtures (real `correct_candidate_id` + `DecoyType` ground truth) plus 36
cross-family "unrelated control" pairs. **No production code changed — this is the "produce a
useful precision/recall curve before Stage 2" validation step, not an implementation.**

Result: cross-family separation is large and clean (unrelated mean 0.148 vs. on-topic mean ~0.44),
confirming the core premise. Best-F1 threshold (0.425) reaches P=0.706/R=1.000/F1=0.828 — but **no
single threshold achieves both perfect precision and recall**: one real `wrong_state_family` decoy
(same entity, different specific topic, lexically overlapping with the query message) scores
**0.5794 — higher than every true positive (max 0.4537)**. `stale` candidates are statistically
indistinguishable from `positive` by content alone (expected — that's the separate bitemporal
filter's job, and the two are confirmed to compose correctly in principle). Conclusion: similarity
is a real, working primary signal, not a full replacement — the ambiguous band around the one hard
negative needs a second-stage LLM judge, exactly as originally proposed. Proposed (not yet built)
reason codes: `semantic_score_below_threshold`, `semantic_ambiguity`, `semantic_match`,
`llm_judge_selected`, `llm_judge_rejected`.

**Still not done before Stage 2:** validate threshold bands on a larger/more varied fixture set,
prototype the LLM-judge second stage against the concrete "boundaries" hard case, wire a two-tier
(similarity + judge-for-ambiguous-band) signal into the real
`shadow_context_composition.py` (still Stage 1 / shadow-only), and re-run the broad smoke to
confirm real selections occur without admitting the deliberate control.

### LLM-judge validation experiment (2026-07-16) — the judge did not yet solve the boundaries case either

Full results: `.agent/reviews/menhir-shadow-llm-judge-lab-2026-07-16.md`. Tested a single binary
LLM call (`gpt-4o-mini`, "does this candidate match this message") against all 69 rows from the
same fixture set, per direct instruction to measure whether the judge preserves true positives
while rejecting the boundaries hard negative. **It has not, across 3 real attempts:**

- Attempt 1 ("is this the same fact"): TP=0/12, FN=12/12 — rejected every true positive. Root
  cause: these are knowledge-*update* fixtures where the correct candidate is deliberately the
  PRIOR value being updated (message: "moved to the suburbs" / correct candidate: "previously
  moved to Chicago") — "same fact" is the wrong question. Despite being maximally strict, it
  *still* wrongly accepted the boundaries decoy twice.
- Attempt 2 ("same slot, value may differ," with the exact Rachel/Chicago/suburbs case as a
  worked example in the prompt): **bit-identical result to attempt 1** — confirmed via an isolated
  debug call (not a caching bug) that `gpt-4o-mini` reasons about differing values as differing
  states regardless of explicit counter-instruction.
- Attempt 3 (spot-check only, 3 rows, not run at full scale): a "field/slot, git-blame" framing
  fixed recall on both spot-checked positives, but the boundaries decoy *still* passed.

Every other negative category (cross-family, stale, wrong_subject, missing_metadata, wrong_scope)
was rejected cleanly and consistently in every attempt — the difficulty is narrow and specific to
same-subject, lexically-overlapping, different-specific-topic decoys, exactly the category
predicted to be hardest. Neither similarity nor the judge (in any of 3 framings) has yet
separated this one case. Proposed next step: a decomposed judge (extract the specific state/slot
for message and candidate as two short extractions inside one call, then compare) rather than one
holistic binary judgment — not yet built or tested.

### Contrastive judge validation experiment (2026-07-16) — boundaries solved, two new problems found

Full results: `.agent/reviews/menhir-shadow-contrastive-judge-lab-2026-07-16.md`. Per direct
instruction, tested a CONTRASTIVE design: one call per entity, message + ALL real candidates (7:
true positive + one of each of the 5 real `DecoyType` categories + one cross-family control) shown
together, forced structured extraction (subject/slot/value/evidence) for the message and every
candidate, then a comparative selection — not an isolated yes/no per candidate. Scoring reads
**only** `selected_candidate_ids`, never the extracted slot strings, per explicit instruction not
to recreate the original exact-match failure.

**Mixed result. The boundaries decoy (and its category generally) was correctly rejected in all 3
test cases — the first approach in this whole chain (similarity, 3 pairwise judge framings, now
this) to cleanly separate that specific case.** But true-positive recall dropped to 1/3, with two
new failure modes neither similarity nor the pairwise judge showed: (a) `lme-830ce83f` selected a
deliberately vague `missing_metadata` decoy ("Rachel mentioned something about her living
situation changing") over the concrete correct answer; (b) `lme-852ce960` correctly identified TWO
real same-slot candidates (the true positive and a `stale` one) but selected neither rather than
picking the more recent one — likely partly a test-design artifact, since the recommended pipeline
order (temporal filter before this stage) would filter the stale candidate out before the judge
ever saw it. N=3 is too small to generalize the 830 failure from one data point.

**Not yet done:** re-run with `stale` candidates pre-filtered per family (matching real pipeline
order) to isolate whether 852's non-selection was a test artifact, and expand the fixture set
before drawing conclusions about the 830-style failure. Only after both, consider prototyping this
shape inside `shadow_context_composition.py` — still Stage 1 / shadow-only, no production wiring
yet.

---

## Stage 2 — Counterfactual extraction

**Goal:** measure whether the composed context actually improves extraction outcomes, without
risking production data.

**Mechanism:** run both paths per episode:
- **A: normal production extraction** (current behavior) — **persist only this.**
- **B: extraction with the selected compact native-style context** substituted in — run,
  score, discard. Never written to the graph.

**Measure:** whether B improves update-capture on real traffic and on LongMemEval benchmark
replay, without increasing unsupported propositions (B must not become a hallucination path just
because it's more permissive about giving the extractor something to work with).

**Exit criteria to advance to Stage 3:** B beats A on update-capture with unsupported-extraction
rate held flat or improved, across both real-traffic replay and the benchmark set (not just one).

---

## Stage 3 — Guarded canary

**Goal:** let the composed-context path actually influence what gets written, but only under
conditions the Recall Labs evidence has already validated.

**Allow the composed-context path only when all of:**
- subject resolved confidently
- metadata hypothesis maps to existing labels (grounded, per the item-3 finding — not an abstract
  ontology guess)
- exactly one temporally eligible fact remains (i.e. `eligible_candidates` returns exactly 1 — the
  deterministic-filter case Phase 4b proved at 100%/100%)
- source provenance is intact

**Continue abstaining** for novel or ambiguous cases — anything that would have required the LLM
tie-breaker or hit a corruption-matrix-style dangerous pattern stays on the current production
path in this stage. This is deliberately the *narrowest* slice: the deterministic lane only.

**Exit criteria to advance to Stage 4:** the guarded canary runs on real traffic for a defined
window with no regressions on the integration-gate metrics below, specifically zero increase in
wrong-subject / wrong-state-family context rate.

---

## Stage 4 — Broaden through evidence

**Goal:** extend beyond the single-eligible-candidate deterministic lane.

**Mechanism:** expand to cases requiring the LLM tie-breaker (`select_structured_then_llm`'s
fallback path) only after the deterministic lane (Stage 3) has proven stable in production. This
is the genuine-tie suite's finding graduating from "validated in Recall Labs on 3 fixtures" to
"trusted in production" — it does not get trusted by default just because Stage 3 passed.

---

## Metrics for the integration gate

Primary decision metrics:
- update proposition capture rate
- injection precision
- candidate recall
- abstention correctness
- unsupported extraction rate
- wrong-subject context rate
- wrong-state-family context rate
- latency and token cost
- end-to-end LongMemEval knowledge-update accuracy

**Failure-stage breakdown — recorded separately, never collapsed into one accuracy number:**
1. right context unavailable (candidate retrieval never found it)
2. right context retrieved but rejected (structured filter or metadata production discarded it)
3. wrong context selected (the dangerous case — corruption-matrix-style mistagged-decoy failure)
4. right context selected but extraction still failed (the extractor itself didn't use good
   context correctly — a different bug class than everything upstream of it)

These four are different failure stages requiring different fixes (retrieval tuning, metadata
grounding, selector safety, prompt/extractor behavior respectively). Any dashboard or gate report
for this integration must report all four, not a blended pass rate.

## What this plan does not cover

- No schema changes to the production graph.
- No change to `RELEVANT_SCHEMA_LIMIT` or graphiti-core's own extraction window — this plan
  supersedes that approach (Phase 3 already showed it doesn't work), it does not combine with it.
- No implementation start. Stage 1 is pure logging and is the lowest-risk entry point, but
  beginning it is a separate decision from writing this plan.
