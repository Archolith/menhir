# Handoff: Menhir Ingest-Quality Investigation — Extraction Context

> **Archived 2026-08-11.** Phases 1–5 and the four requested follow-ups were executed; this document
> is now an experiment/results record rather than an active handoff.

**Status: ACTIVE — supersedes the ad-hoc Phase 1/Phase 2 exploration in
`menhir-belief-supersession-code-mapped-plan.md` for everything extraction-context-related.**
That plan's own Phase 0 (Extraction Lab harness) and Phase 1 (prompt ablation) results feed
directly into this document as the starting evidence base; this document is the more rigorous
continuation plan, explicitly designed to control for the API sampling-variance problem that
undermined both `update_aware`'s and the candidate-lookup's first results.

**Provenance:** authored by the user working with an external model (Codex-style, 2026-07-15),
built directly from this session's own reported findings and numbers (quotes the exact figures
from the "Report: Ingest-Quality Investigation" delivered earlier this session). Pasted into this
Claude Code session as an operational handoff, not a speculative research doc — unlike the two
earlier Codex documents saved this same day (`../../reference/menhir-belief-supersession-temporal-chains-research.md`,
`menhir-extraction-prompt-recency-recall-research.md`), this one is being actioned immediately
per its own "Required Next Work" phase ordering, starting with Phase 1 (repeated-trial variance
quantification).

**Explicit exclusions (per the doc's own "Do not work on" list):** retrieval ranking, belief
supersession, CurrentnessWarden, evidence oracles, production graph schema changes.

---

## Results (2026-07-16) — Phase 1 and Phase 2 executed, durable findings below

**Read this section first if you are picking up this investigation — it is the actual answer to
the questions the "Original document" below poses, not just a status update.** Cross-referenced
into `.agent/reviews/rca-lme-stale-fact-retention-2026-07-15.md`'s "2026-07-16 update" section;
that section is a compressed version of these results for readers who only need the RCA.

### Phase 1: Quantify Model Variance — DONE

Ran 120 trials (10 per cell) across baseline / `update_aware` / baseline+lookup / `update_aware`+
lookup, on the 3 real RCA fixtures, interleaved order, `context_episode_count=3` fixed. Raw data:
`results/extraction_lab_phase1_variance.json`. Runner: `scripts/run_extraction_lab_phase1_variance.py`.

Per-fixture proposition-success frequency (95% Wilson CI):

```
830ce83f:  baseline 8/10 (80%)   update_aware 9/10 (90%)   +lookup 6/10 (60%)   both+lookup 6/10 (60%)
852ce960:  10/10 (100%) on all four -- ceiling, uninformative
2698e78f:  baseline 1/10 (10%)  update_aware 0/10 (0%)     +lookup 3/10 (30%)  both+lookup 6/10 (60%)

Aggregate (n=30, all 3 fixtures):
  baseline               19/30 (63%)  CI [46%,78%]
  update_aware            19/30 (63%)  CI [46%,78%]
  baseline_lookup         19/30 (63%)  CI [46%,78%]
  update_aware_lookup     22/30 (73%)  CI [56%,86%]
```

**Verdict: none of the 4 configs clear the bar this document itself set** ("improvement beyond API
sampling variance," "repeated improvement across real RCA fixtures") — every pairwise CI overlaps.
`update_aware` alone was actively *worse* than baseline on `2698e78f` (0% vs 10%), the first
evidence in this investigation that a prompt variant can hurt, not just plateau. This result is
what motivated building Phase 2 rather than declaring victory on `update_aware`.

### Phase 2: Context-Form Ablation — DONE, this is the load-bearing result

240 trials (10 per cell) across 8 conditions (A/B/C/D/E/F/G/J — see "Note on collapsed H/I" below)
x 3 real RCA fixtures, interleaved order. Raw data:
`results/extraction_lab_phase2_context_ablation.json`. Runner:
`scripts/run_extraction_lab_phase2_context_ablation.py`. Condition definitions and per-fixture
authored content: `src/menhir/explorer/extraction_lab_context_ablation.py`.

**`852ce960` — uninformative.** 100% success on all 8 conditions; ceiling effect at this context
depth. Excluded from the analysis below.

**`830ce83f` — clean two-cluster separation, the strongest single result in this investigation:**

```
                          success   95% CI
A  no context              1/10 (10%)   [2%,40%]
B  entity-name only        0/10 (0%)    [0%,28%]
E  entity description      1/10 (10%)   [2%,40%]
F  unrelated episode       0/10 (0%)    [0%,28%]
J  retrieved-context block 1/10 (10%)   [2%,40%]
--------------------------------------------------  <- clean gap
C  full real episode       7/10 (70%)   [40%,89%]
D  compact fact            9/10 (90%)   [60%,98%]
G  lexically similar       10/10 (100%) [72%,100%]
```

**`2698e78f` — does NOT replicate the same pattern; a different failure shape:**

```
A  no context              8/10 (80%)
C  full real episode       5/10 (50%)   <- WORSE than no context
D  compact fact            9/10 (90%)
G  lexically similar       9/10 (90%)
```

### The six durable findings (verified against non-overlapping or near-non-overlapping CIs)

1. **Native previous-episode delivery is not equivalent to a bolted-on "retrieved context" block,
   even with byte-identical text.** `830ce83f`: C (real episode, ordinary delivery) = 70%; J (same
   text, `<RETRIEVED CONTEXT>` block) = 10%. Any future context-injection mechanism must render
   through the same channel/format as an ordinary prior episode.
2. **Entity-name awareness alone does not help.** B = 0%, statistically indistinguishable from A
   (10%) and F (0%). Directly refutes the original Phase 2 candidate-lookup design (name-only
   signal) as built earlier in this investigation.
3. **A compact restated fact matched or beat the full episode on both non-ceiling fixtures.**
   `830ce83f`: D 90% vs C 70%. `2698e78f`: D 90% vs C 50% (C below that fixture's own 80%
   baseline). The most cross-fixture-consistent finding here — full episode retrieval is not
   required.
4. **Topically-similar-but-unrelated context can unlock extraction for the wrong entity.** G
   (different person, same topic) = 100% on `830ce83f`, beating both correct-content conditions
   (C, D). F (genuinely unrelated) = 0%, at the floor with A/B/E/J. The effect is topical/lexical
   priming, not truth-recovery — a real precision risk (confident extraction unlocked by adjacent
   noise, not by correct grounding), not just a recall opportunity.
5. **The three RCA fixtures are not one failure class.** `852ce960` = ceiling (uninformative).
   `830ce83f` = context-gated under-extraction (clean 0-10% vs 70-100% split). `2698e78f` =
   context-interference (the correct real episode makes things *worse* than no context at all) —
   the opposite pattern. Aggregating across all three (as earlier single-config runs in this
   investigation did) hides this split. Any future fix must be evaluated per-fixture.
6. **Conditions H/I (native Graphiti window vs. reconstructed raw-turn window) were not
   implemented as distinct conditions — disclosed, not faked.** The RCA fixtures' `previous_episodes`
   came from real `:Episodic` nodes in the live LME graph, ~1:1 with raw turns (23-48 nodes per
   fixture) — not the "~3 sub-episodes per raw turn / 70+" expansion the original RCA's Step 3/4
   described for a different ingest pass of the same conversations. This discrepancy is unresolved
   and matters for Phase 3's episode-limit sizing below — see "Open question" there.

### Phase 3: `RELEVANT_SCHEMA_LIMIT` as a causal control — DONE (2026-07-16)

Ran 130 trials (10 per cell) across 13 (fixture, limit) cells, using the REAL, full
`previous_episodes` lists per RCA fixture and the harness's real production-style slicing
(`previous_episodes[-context_episode_count:]`) — not the hand-authored content Phase 2 used.
`852ce960` skipped per instruction (confirmed ceiling case). Raw data:
`results/extraction_lab_phase3_schema_limit.json`. Runner:
`scripts/run_extraction_lab_phase3_schema_limit.py`.

**Requested limits 10/20/40/80 were mostly degenerate given available fixture length**
(`830ce83f`/`852ce960` only have 12 real prior episodes; `2698e78f` has 24) — any limit >= the
fixture's total episode count collapses to the same full-list slice. The runner deduplicated these
automatically and substituted finer-grained values that actually bracket the real
inclusion/exclusion boundary; the exact values tested are recorded per-fixture in the raw output.

**Per-limit results — the headline finding is that this is NOT a clean monotonic curve:**

```
830ce83f (12 episodes total):
  limit= 1 (absent)  20%   limit= 3 (absent)  90%   limit= 5 (absent)   0%
  limit= 8 (present) 80%   limit= 9 (present)  0%   limit=10 (present) 90%   limit=12 (present) 70%

2698e78f (24 episodes total):
  limit= 1 (absent)  80%   limit= 3 (present) 40%   limit=10 (present) 60%
  limit=20 (present)100%   limit=22 (present)100%   limit=24 (present) 60%
```

Both fixtures swing by 60-90 percentage points between adjacent limit values, including drops
*within* the "present" group (830ce83f: 80%→0%→90% across limits 8/9/10) and a high score in the
"absent" group (830ce83f limit=3: 90%, higher than most "present" limits). These are not small
n=10 wobbles — several of these gaps have non-overlapping 95% CIs (e.g. limit=3's [60%,98%] vs.
limit=5's [0%,28%]).

**Primary analysis — P(success | establishing episode present) vs. P(success | absent):**

```
830ce83f:    present 24/40 (60%, CI [45%,74%])   absent 11/30 (37%, CI [22%,54%])
2698e78f:    present 36/50 (72%, CI [58%,83%])   absent  8/10 (80%, CI [49%,94%])  <- reversed
AGGREGATE:   present 60/90 (67%, CI [56%,76%])   absent 19/40 (48%, CI [33%,63%])
```

The aggregate direction matches the hypothesis (present > absent) but the CIs are close to
touching (absent's upper bound 63% vs. present's lower bound 56%), and `2698e78f` alone runs
*backwards* (presence very slightly, non-significantly, associated with lower success). This is
much weaker and noisier than Phase 2's clean 0-10%-vs-70-100% split.

**Root cause of the noise, identified during analysis, not swept under the rug: the
"establishing episode present" flag used here was too narrow.** It only tagged the single clearest
introduction of each fact (e.g. `830ce83f` index 4, "visiting Rachel in Chicago"). But the real
conversations mention the same entities repeatedly — `830ce83f`'s indices 6-10 all reference Rachel
and/or Chicago again in passing (asking about neighborhoods, coffee shops, etc.) without being
tagged as "the" establishing episode. So several windows counted as "absent" by this measure
actually contained real, if secondary, Rachel/Chicago content — which plausibly explains both the
high scores in nominally-"absent" windows (limit=3) and the muddied present-vs-absent signal
overall. The *true* effect of "is any relevant content in the window" is likely stronger than what
this measurement shows; the measurement undercounts it. Flagging this as a real methodological
limitation of this phase rather than re-running it (the qualitative conclusion below doesn't depend
on resolving it) — a future pass wanting a cleaner causal estimate should tag ALL episodes
mentioning the target entity, not just the first/clearest one.

**What this changes for the fix direction: raising `RELEVANT_SCHEMA_LIMIT` is now confirmed not
just structurally non-scaling (the original concern) but empirically unreliable even within the
range where it's cheap to test.** Picking a specific limit value does not predictably restore
extraction — the exact number matters in ways this phase couldn't fully explain (window
composition, not just window size or target-fact presence, appears to matter, e.g. an intervening
assistant question at `830ce83f` index 3 coincides with a 90%→0% collapse between limits 8 and 9).
This is a stronger argument against "just widen the window" as a viable fix, even a stopgap one,
than what was known before this phase. It sharpens the case for Phase 4's targeted, single-fact
composer over any window-size-based approach.

**Exploratory position effect (within "present" trials):** no clear pattern emerged —
`830ce83f`'s present trials were all "near-start" of their respective windows by construction (60%
success); `2698e78f` showed near-start 67% vs. near-end 80%, a small and not clearly meaningful
difference at this sample size. Not pursued further.

### Phase 4 (design finalized 2026-07-16, selector prototype tested same day) — context selection, not extraction, is now the open problem

**Precise statement of what is and is not confirmed, per direct correction (2026-07-16):** Phase 2
confirmed that a hand-selected, compact, natively-delivered prior fact is a *viable mechanism* —
when the RIGHT fact is chosen and delivered the RIGHT way, extraction reliably succeeds (~90%).
That is not the same as confirming an *automated end-to-end system* — nothing built so far has
tested whether Menhir can correctly CHOOSE that fact on its own. Phase 3's chaotic per-limit swings
sharpen why this distinction matters: **episode count was never the real control variable; context
composition is.** Each additional episode in a window can add useful grounding, irrelevant topical
priming, a competing entity, a conflicting fact, or content that otherwise alters extraction
behavior — Phase 3 showed all of these effects firing unpredictably as the window grew. A system
that reliably composes the RIGHT single fact sidesteps that unpredictability entirely; a system
that just widens the window does not. **The open problem is now context selection, not
extraction — extraction already works fine once fed the right compact fact.**

**Architecture, stated precisely:**

```
BAD (Phase 3's finding):        last N episodes -> extractor  (composition uncontrolled, chaotic)

BETTER (Phase 4's target):      current message
                                     -> identify subject + topic/facet
                                     -> retrieve one compact prior fact
                                     -> insert as an ordinary previous episode (finding 1: native
                                        delivery, not a labeled block)
                                     -> extractor (unchanged)
```

**Two separate quality gates, tested independently — this is the core methodological change from
the earlier draft of this phase.** Finding 4 (topically-similar-but-wrong-entity context scored
100%, higher than the correct fact) proved the extractor can succeed for the wrong reason. A
selector that leans on that same effect would look like it works while actually being unreliable
in less controlled cases — so selection quality must be measured on its own terms, not inferred
from downstream extraction success alone:

- **Context-selection recall** — was the useful prior fact included among the retrieval
  candidates at all?
- **Context-selection precision** — did the FINAL injected context avoid irrelevant or misleading
  facts (i.e., did the selector correctly reject the decoys, not just happen to have the right one
  in the candidate pool)?

**Candidate hierarchy — narrowest available match first:**

```
1. Same subject + same state family   (Rachel / current residence)        -- strongest
2. Same subject + same facet          (Rachel / housing)
3. Same subject + related fact        (Rachel / relocation language)
4. Same facet, different subject      (housing, not Rachel)               -- experimental
                                                                              control only, avoid
                                                                              in the real selector
```

This refines the earlier "subject x facet" 2x2 into a proper hierarchy with a `subject/facet/state
family` decomposition, e.g. for the Rachel case: `subject=Rachel -> facet=Housing ->
state_family=Current residence -> event=Relocation`. Facet finds the relevant topic area; state
family finds the specific fact that's actually current; event type describes what changed.

**Provenance must be preserved but need not be model-visible.** The rendered prompt content stays
exactly as tested in Phase 2 ("Rachel previously moved to an apartment in Chicago.") — the model
never needs to see where it came from. Internally, Menhir should carry `source_episode_id`,
`fact_id`, `subject_id`, `facet`, `state_family`, `confidence` alongside the composed fact, so the
composer's choices remain auditable without changing what the extractor is shown (consistent with
finding 1 — the delivery channel is exactly the ordinary-episode format, nothing added to it).

**Required negative-control test matrix — the most important gate before calling this working.**
Before treating the composed-context system as validated, it must be tested against graph states
where the correct answer to "what should the selector inject" varies, including the case where the
right move is to inject nothing:

```
1. One correct related fact available               -- selector should find and use it
2. Several facts about the same subject, different facets  -- selector should pick the matching facet
3. Several housing facts about DIFFERENT people      -- selector should reject the wrong-subject ones
   (this is finding 4's false-grounding trap, made explicit as a test case rather than an
   after-the-fact observation)
4. An outdated fact AND a newer fact for the same subject/facet -- selector should prefer current
5. No relevant prior fact exists at all              -- selector must inject NOTHING, not fall back
   to loosely-adjacent context (the false-grounding trap again, at the "nothing good exists" edge)
```

Case 5 is a hard requirement, not a nice-to-have: given finding 4 (topical priming alone can
unlock extraction for the wrong reason), a selector that injects "something plausible-looking" when
nothing correct exists would be actively reproducing the precision risk this investigation flagged,
not avoiding it.

**Implementation scope note (self-imposed, not yet confirmed with the requester):** this session's
handoff explicitly excludes "production graph schema changes." Building Phase 4 for real would
require the graph to actually carry retrievable, facet-tagged facts — which does not exist today.
The scope-safe way to build and test the SELECTOR's recall/precision in isolation without touching
production schema is a Recall-Labs-only simulation: hand-author small candidate pools per fixture
(mirroring the negative-control matrix above — a correct fact, same-subject decoys, wrong-subject
decoys, stale-vs-current pairs, and an empty-pool case) and test whether a selection mechanism
(rule-based or a narrow LLM call) picks correctly from each pool, entirely inside the harness. This
tests the SELECTION logic end-to-end without requiring real facet infrastructure in the graph. Real
graph-backed retrieval (an actual `facet`/`state_family` schema, real fact storage) would be a
separate, later, explicitly-scoped decision — not implied by this phase.

**Production gate (restated once more, now precise):** do not wire anything into production
ingestion until the composer, tested against the full negative-control matrix above, beats
per-fixture: no context, entity-name-only, full native episode, and same-facet-wrong-entity
context — AND demonstrates acceptable context-selection precision (not just downstream extraction
success, which finding 4 already showed can be misleadingly high for the wrong reason).

#### Phase 4 selector prototype — results (150 trials, 2026-07-16)

Built an LLM-based selector (`src/menhir/explorer/extraction_lab_context_selection.py`,
`scripts/run_extraction_lab_phase4_selector.py`) and ran it against all 15 negative-control cells
(5 scenarios x 3 fixtures), 10 interleaved trials each. Raw data:
`results/extraction_lab_phase4_selector.json`. 20 unit tests
(`src/menhir/explorer/test_extraction_lab_context_selection.py`), all passing, covering scenario
construction and scoring logic (mocked, no live calls).

**One real bug caught and fixed before trusting any results:** the selector's first-draft system
prompt instructed it to reject a candidate if it didn't "match" or was "contradicted by" the
CURRENT MESSAGE. That is backwards — the entire reason to retrieve a prior fact is usually that the
current message is UPDATING it (the "Rachel moved to Chicago" candidate exists specifically because
the current message says she moved to the suburbs). The unfixed prompt scored 0/3 or near-0 on the
single-candidate "obviously correct" scenario across all 3 fixtures at the smoke-test stage — a
genuine logic defect, not a hard case. Fixed by explicitly instructing the selector that apparent
disagreement between a candidate and the current message is not a rejection signal. Post-fix
numbers below are all against the corrected prompt.

```
Aggregate by scenario type (30 trials each, across all 3 fixtures):
  1 one_correct                  30/30 (100%)  CI [89%,100%]   decoy_selections=0
  2 same_subject_multi_facet     29/30  (97%)  CI [83%, 99%]   decoy_selections=1
  3 same_facet_multi_subject     30/30 (100%)  CI [89%,100%]   decoy_selections=0
  4 stale_vs_current             21/30  (70%)  CI [52%, 83%]   decoy_selections=9
  5 no_relevant_fact             10/30  (33%)  CI [19%, 51%]   decoy_selections=20
```

**Subject discrimination is solid: 100% on scenario 3** (rejecting wrong-subject decoys even when
they share the target facet — this is finding 4's false-grounding trap from Phase 2, tested
explicitly, and the selector handles it cleanly every time in this sample).

**Fine-grained facet/recency discrimination is the real, precise, fixture-specific weak point** —
not a uniform "sometimes fails," a clean split:

```
Scenario 4 (stale vs current) by fixture:  830ce83f 10/10   852ce960  1/10   2698e78f 10/10
Scenario 5 (no relevant fact) by fixture:  830ce83f 10/10   852ce960  0/10   2698e78f  0/10
```

`830ce83f` gets every case right. `852ce960` and `2698e78f` fail almost every time on exactly these
two scenario types. Inspecting *why* (not just noting the split) shows a coherent, mechanistic
pattern: `830ce83f`'s decoys are COARSE distinctions — "Employment" vs. "Housing" (different life
domains), "Denver" vs. "Chicago" (different named places). `852ce960` and `2698e78f`'s decoys are
FINE-GRAINED distinctions WITHIN the same narrow topic — "mortgage pre-approval amount" vs. "home
inspection findings" (both home-buying-adjacent), "therapy schedule" vs. "therapy discussion topic"
(both about the same therapy relationship), and two dollar amounts ($325,000 vs. $350,000) or two
frequencies (monthly vs. every two weeks) differing only by value, not by category. **The selector
reliably distinguishes different life domains but struggles to distinguish closely-related
sub-facets of the same domain, or to infer recency from wording alone when two candidate values
look similar.**

**This directly motivates using menhir's own real structured metadata rather than natural-language
inference, if this is ever built against a real graph:** menhir already has a bitemporal fact model
(`domain/temporal.py`: `valid_at`/`invalid_at`/`created_at`/`expired_at`) that would resolve the
stale-vs-current failures structurally (compare real timestamps, don't infer "initially" vs.
"previously" from wording) — and an explicit `facet`/`state_family` tag on stored facts (not yet
built; see the candidate hierarchy above) would resolve the fine-grained-facet failures the same
way (compare tags, don't ask an LLM to judge semantic distance between "mortgage" and "home
inspection" from prose alone). This is a concrete, evidence-backed argument for *why* the
Phase 4 design's provenance metadata (`facet`, `state_family`) needs to be real structured data on
real stored facts eventually, not just documentation — text-only candidates hit a real, repeatable
ceiling on exactly the cases where getting it wrong matters most (false-grounding and stale-fact
injection).

**Verdict against the stated production gate:** not met yet, correctly — scenario 5 (33%
aggregate, 0% on 2 of 3 fixtures) is the single most safety-critical gate (correctly declining when
nothing good exists), and it is the worst-performing scenario. Selection is not ready to gate
production wiring. The finding is precise and actionable rather than a flat "not ready," though:
coarse discrimination works now; fine-grained discrimination needs either a better prompt
(unexplored — no second fix attempt was made here, deliberately, to avoid overfitting the prompt to
these exact 15 test cases) or, more durably, structured comparison instead of natural-language
inference.

#### Phase 4b: structured eligibility filtering, four-selector comparison — results (420 trials, 2026-07-16)

Direct redesign instruction (2026-07-16): the Phase 4 prototype's failure is an **open-set
selection problem, not a reranking problem** — a forced-choice LLM ranker tends to pick the
least-bad candidate instead of abstaining when nothing is actually eligible. Fix: hard-filter on
structured metadata (subject, facet, state_family, scope, bitemporal validity) *before* any
ranking, falling back to an LLM only for genuine residual ambiguity. Built and compared four
selectors against 7 negative-control scenario types (expanded from Phase 4's 5, adding "wrong
state_family," "wrong scope," and "missing metadata" as distinct cases) x 3 real RCA fixtures = 21
cells, with real `valid_at`/`expired_at` timestamps replacing word-cue recency ("initially" vs.
"previously") this time. Code:
`src/menhir/explorer/extraction_lab_eligibility_selection.py` (the four selectors + `is_eligible()`
hard filter), `extraction_lab_eligibility_fixtures.py` (21 scenario pools), 24 unit tests in
`test_extraction_lab_eligibility_selection.py`. Runner:
`scripts/run_extraction_lab_phase4b_eligibility.py`. Raw data (462 records):
`results/extraction_lab_phase4b_eligibility.json`.

```
selector               precision (of injected, % correct)   coverage (of correct-cases, % found)   decoys   llm_calls
llm_only                72/112 = 64%                          72/120 = 60%                           40       210
structured_only         12/12  = 100%                         12/12  = 100%                           0         0
structured_then_llm    120/120 = 100%                        120/120 = 100%                           0         0
oracle                  12/12  = 100%                         12/12  = 100%                           0         0
```

**Denominator note (why `structured_only` shows 12/12 and `structured_then_llm` shows 120/120 —
same underlying result, not a different one):** of the 21 scenarios, 12 have a real correct
candidate (scenario types 1-4, one per fixture x 4 = 12) and 9 are negative controls expecting
"select nothing" (types 5-7, one per fixture x 3 = 9). `structured_only` is fully deterministic
(rule-based, no LLM), so it was run exactly **once** per scenario per the runner's design (repeating
a deterministic function adds no information) — giving 12 positive-scenario runs, all correct:
12/12. `structured_then_llm` was run through the same **10-trial repeated-sampling loop as the
LLM-involving selectors**, for direct comparability with `llm_only`'s numbers in the per-scenario-
type table below — since it turned out to be deterministic too (0 LLM calls, see the caveat below),
each of the same 12 positive scenarios simply repeated its correct answer 10 times: 12 x 10 = 120,
all correct. Both rows describe the identical underlying decision (100% correct on all 12
positive-answer scenarios, 100% correct abstention on all 9 negative ones) — the raw counts differ
only because of how many times each selector was re-run, not because of a different eligibility
denominator or a different set of scenarios being scored.

**By scenario type (30 trials each, `llm_only` vs. `structured_then_llm`), 95% Wilson CI:**

```
1 one_correct              llm_only  67% [49,81]   structured_then_llm 100% [89,100]
2 wrong_state_family       llm_only  67% [49,81]   structured_then_llm 100% [89,100]
3 wrong_scope               llm_only  73% [56,86]   structured_then_llm 100% [89,100]
4 stale_vs_current          llm_only  33% [19,51]   structured_then_llm 100% [89,100]
5 near_neighbor_decoys_only llm_only  67% [49,81]   structured_then_llm 100% [89,100]
6 no_subject_match          llm_only 100% [89,100]  structured_then_llm 100% [89,100]
7 missing_metadata          llm_only   0%  [0,11]   structured_then_llm 100% [89,100]
```

**The structured approach didn't just outperform — it was perfect on every cell, at zero LLM cost.**
The two scenario types matching this document's stated diagnosis show the largest gaps: missing
metadata (0%→100%) and stale-vs-current with real-but-similar-looking values (33%→100%). Subject
discrimination (scenario 6) is the one case where `llm_only` already matched structured (100%
either way) — consistent with Phase 4's original finding that subject discrimination was never the
weak point.

**Two honest caveats, not swept under a "100%!" headline:**

1. **`structured_then_llm`'s LLM-fallback path was never actually exercised.** Across all 210
   trials, `llm_calls=0` — every one of the 21 authored scenarios resolved to 0-or-1 eligible
   candidates after the hard filter alone, so the LLM disambiguation step this selector's name
   promises never ran even once. What was tested at scale is really `structured_only`'s
   correctness; the *hybrid* design's actual differentiator (does the LLM correctly break a
   genuine tie between 2+ structurally-eligible candidates) remains unvalidated. A future pass
   wanting to test that specifically needs a scenario engineered to leave real ambiguity after
   filtering — not fabricated here to avoid a contrived, low-value test case.
2. **This entire phase assumed complete, correctly-typed metadata already exists on every
   candidate** (`subject`/`facet`/`state_family`/`scope`/`valid_at` all populated, hand-authored to
   be correct). That infrastructure does not exist in the real graph today — no facet-tagging, no
   scope-typing. Phase 4b proves that **if** structured metadata is available, filtering on it is
   dramatically more reliable than asking an LLM to reason it out from prose. It does **not** prove
   that metadata can be reliably *produced* from raw conversation in the first place — extracting
   subject/facet/state_family/scope/valid_at from a message is itself an unsolved, unbuilt,
   untested step, and is now the honest next open problem, upstream of everything tested here.

**Revised production gate:** the composer's eligibility-filter stage should be built and driven by
real structured metadata (menhir's existing `valid_at`/`expired_at` bitemporal fields, plus new
`facet`/`state_family`/`scope` tags — see the candidate hierarchy above) rather than an LLM
reasoning over prose. The LLM's role should shrink to true residual ties, exactly as designed here
— but that specific code path still needs its own test before being trusted, and the metadata
*production* step (how facet/state_family/scope get attached to a stored fact in the first place)
is unbuilt and is the actual next-highest-value question, not further selector tuning.

### Phase 5 (design captured 2026-07-16, built and run same day) — metadata production, not selector tuning

**Status: planned, deliberately not started.** This session's Phase 4b work ends at a clean
stopping point (confirmed by direct instruction): fine-grained context selection is solved *given*
correct structured metadata; metadata production is now the bottleneck. Freeze the Phase 4b
selector — do not keep tuning it while testing metadata generation.

**Precise restatement of what Phase 4b actually validated:** not the hybrid's LLM tie-breaking (it
was never exercised — 0 real LLM calls across 210 `structured_then_llm` trials), but a
deterministic eligibility function: `subject match + compatible facet/state_family + compatible
scope + actual temporal applicability = eligible context`. Once those gates ran on hand-authored,
correct metadata, there was nothing left for an LLM to decide.

**Two distinct metadata problems, not one — this separation matters:**

1. **Candidate-side metadata** — existing stored facts need reliable `subject_id`, `facet`,
   `state_family`, `scope_id`/`event_key`, `valid_at`, `expired_at`, `learned_at`,
   `source_episode_id`. E.g. `subject=Rachel, facet=housing, state_family=residence_location,
   scope=Rachel's current residence, valid_at=...`.
2. **Query-side routing metadata** — the new, currently-under-extracted raw message needs a
   lightweight pre-pass producing `subject mention`, `likely facet`, `likely state_family`,
   `event type` (e.g. relocation) *before* full extraction runs, since this pre-pass is what locates
   the compact prior fact needed to help the main extractor. **This pre-pass cannot depend on the
   full extractor succeeding — that's the exact failure being repaired**, so it must be a separate,
   higher-recall mechanism, not a byproduct of the extractor itself.

**Proposed production-shaped pipeline** (the context composer stays separate from the extractor —
a pre-extraction continuity provider, not a change to extraction itself):

```
New raw message
    -> High-recall routing pre-pass (explicit subject mentions, likely facet, likely state_family)
    -> Structured candidate filter (subject, facet/state_family, scope, bitemporal applicability)
    -> 0 candidates  -> inject nothing
       1 candidate   -> inject compact native-style fact
       2+ candidates -> optional LLM tie-breaker
    -> Normal Graphiti extraction (unchanged)
```

**Phase 5 test design — a Metadata Lab, testing production independently of selection:**

- Build a harness that takes raw source messages or existing graph facts and outputs proposed
  metadata. Score each field independently: subject resolution accuracy, facet accuracy,
  state-family accuracy, scope accuracy, temporal-field accuracy, and abstention rate when a field
  is genuinely unknowable (abstaining on an unknowable field is correct, not a miss).
- **The downstream test matters most and must not be skipped:** `produced metadata -> structured
  selector -> selected compact context -> extraction result`. Perfect metadata-field classification
  that still selects harmful context in the end would not be a useful result — field-level accuracy
  is a diagnostic, not the success criterion.
- Compare metadata-production approaches head to head: (a) existing graph/entity/edge information
  mapped deterministically, (b) ontology-constrained LLM classification, (c) two-stage subject
  detection followed by facet/state-family classification, (d) hybrid rules plus LLM only for
  missing fields, (e) oracle metadata (Phase 4b's already-established upper bound). Use menhir's
  actual `valid_at`/`learned_at`/`expired_at` wherever they exist — never ask an LLM to infer
  recency when a real timestamp already answers it.

**Metadata corruption tests — required, not optional.** Phase 4b assumed perfect metadata;
production will not have it. Deliberately test: missing facet on the correct candidate; incorrect
state-family tag; unresolved subject; a stale candidate missing its `expired_at`; two candidates
sharing subject and state_family but different scopes; contradictory timestamps; a decoy
*incorrectly tagged as eligible*; and correct context present only under a broader facet than
expected. **The dangerous failure mode is not missing metadata (which naturally causes safe
abstention per Phase 4b's own filter design) — it is incorrect metadata that makes a decoy appear
structurally eligible.** That's the case most worth stress-testing, since it defeats the entire
point of the hard-filter design by feeding it false-positive structure instead of no structure.

**The untested hybrid path still needs its own dedicated test, separate from metadata production:**
a genuine-tie suite engineered so hard filtering intentionally leaves 2+ candidates — e.g. "Rachel
lived in Chicago" and "Rachel lived in Austin," same subject/facet/state_family, both with
plausible or incomplete temporal data. Only this isolates whether the LLM tie-breaker adds real
value, or whether better temporal normalization / finer scope identifiers eliminate real ties
almost entirely, making the LLM step unnecessary in practice.

**Metrics, restated precisely for Phase 5:** injection precision (of injected, % correct) and
injection coverage (of correct-cases-existing, % found) remain the final downstream metrics:
precision should be prioritized over coverage throughout, since incorrect injected context actively
primes extraction (Phase 2's finding 4) — a system that injects less often but is almost always
right is safer than one that eagerly injects a nearby fact.

**Not started. Explicitly gated the same way every phase in this investigation has been: no
production wiring, no further selector tuning, until Phase 5's metadata-production comparison and
corruption tests are run and reported.**

#### Phase 5 results (90 LLM cells + 2 deterministic baselines, 2026-07-16)

Built `src/menhir/explorer/extraction_lab_metadata_production.py` (5 routing approaches),
`test_extraction_lab_metadata_production.py` (27 unit tests, all passing), and
`scripts/run_extraction_lab_phase5_metadata.py`. The Phase 4b selector was frozen and reused
unchanged — each approach's routing prediction was fed directly into `is_eligible`/
`select_structured_only` against that fixture's real Phase 4b candidate pools (target_scope left as
each scenario originally defined it; routing does not predict scope in this phase). Raw data (93
records including the full downstream chain per trial): `results/extraction_lab_phase5_metadata.json`.

**Field-level accuracy (aggregate across 3 fixtures):**

```
approach                subject   facet    state_family   all_3_correct
graph_lookup_only       0%        0%       0%             0%    (n=3, structurally incapable)
oracle                  100%      100%     100%           100%  (n=3, sanity check)
llm_ontology            67%       67%      67%             33%  (n=30)
two_stage               37%       67%      37%              3%  (n=30)
hybrid_rules_then_llm   100%      67%      33%             33%  (n=30)
```

**Downstream chain — precision/coverage, using each approach's PREDICTED metadata against Phase
4b's real candidate pools (210 chain cells each for the 3 LLM approaches, 21 for the 2
deterministic ones):**

```
approach                precision            coverage           decoy_selections
graph_lookup_only       100% (0/0, vacuous)   0%   (0/12)         0     -- can't do this job at all
oracle                  100% (12/12)          100% (12/12)        0
llm_ontology            100% (40/40)          33%  (40/120)       0     -- never wrong, just cautious
two_stage               100% (4/4)            3%   (4/120)        0     -- even more cautious, barely fires
hybrid_rules_then_llm    80% (40/50)          33%  (40/120)       10    -- the only approach that got it wrong
```

**Four findings:**

1. **`graph_lookup_only` (the only signal that exists in production today) is confirmed
   structurally incapable** — 0% coverage, not because it's inaccurate but because it cannot
   produce facet/state_family at all. This isn't a new finding so much as a direct, now-quantified
   confirmation of why this phase exists.
2. **Both pure-LLM approaches have perfect downstream precision (100%, zero decoy selections
   across 214 combined chain cells) — they fail by abstaining, not by injecting wrong content.**
   This is exactly the safety property this investigation has been optimizing for since Phase 2
   ("precision over coverage"). Their weakness is coverage (33% and 3%), not correctness when they
   do commit.
3. **`two_stage` underperforms the simpler single-call `llm_ontology` on every metric**, including
   the one thing it was specifically designed to improve (subject accuracy: 37% vs. 67%). A
   genuine, disclosable negative result against the design intuition that splitting subject
   detection into its own dedicated step would help — splitting into two independent LLM calls
   compounded error here rather than reducing it, at least as implemented.
4. **`hybrid_rules_then_llm` is the only approach that produced real decoy selections (10 across
   210 chain cells, ~4.8%) — the exact "dangerous case" flagged before this run.** Root cause
   confirmed, not hypothetical: the deterministic keyword rule confidently (and wrongly)
   classifies `852ce960`'s message as facet=Housing instead of Mortgage (it mentions moving into a
   new home in the same breath as the mortgage figure), and because the rule *did* produce a
   non-null facet, the design's full-LLM correction path never triggers — only a narrower
   state_family sub-call runs, scoped to the wrong facet's candidate list. A confident-but-wrong
   deterministic stage with no downstream correction opportunity is worse than an LLM stage that
   stays uncertain — precisely the risk this phase was built to surface, and it surfaced
   organically rather than needing to be engineered in.

**Practical read:** on this test set, the simplest approach (`llm_ontology`, one LLM call, no
rules) has the best safety profile of any non-oracle approach — equal-or-better precision than the
more complex alternatives, without the hybrid's decoy-selection risk. Coverage (33% for the best
real approach) remains the real gap versus oracle's 100%, and is now the honest ceiling to push on
— through better ontology-classification prompting, few-shot grounding, or accepting a
conservative operating point (often abstains, essentially never wrong) as the right tradeoff for a
production composer, consistent with this investigation's precision-first stance throughout.

**Not yet done (at time of writing above):** the full metadata-corruption test matrix and the
dedicated genuine-tie suite. Both were run the same day, immediately following — see below.

#### Item 1: Abstention error taxonomy (2026-07-16) — a real reframing, not just more detail

Post-hoc analysis of the 90 already-collected Phase 5 trials (`scripts/analyze_phase5_abstentions.py`,
no new LLM calls). **Zero missing-field abstentions occurred, across all three approaches, across
all 90 trials.** Not one `subject_unresolved`, `facet_missing`, or `state_family_missing` case. Every
single coverage miss was a confident, well-formed, WRONG classification:

```
llm_ontology (67% miss rate):        50% wrong subject only | 50% wrong facet+state_family only
two_stage (97% miss rate):           roughly even 3-way split: wrong subject / wrong facet+SF / wrong subject+SF
hybrid_rules_then_llm (67% miss):    50% wrong facet+state_family (the Housing/Mortgage rule bug) | 50% wrong state_family only
```

This reframes the coverage problem: it isn't "encourage the model to commit instead of abstaining" —
it never declines to answer. The 100% precision reported earlier isn't the LLM being well-calibrated;
it's the downstream exact-match filter catching confident wrong guesses and converting them into safe
null results. The fix needed is better grounding (so the guess is right more often), not confidence
thresholding (there was no low-confidence signal to threshold — the model was never hesitant).

#### Item 2: Metadata corruption matrix (2026-07-16) — 5 safe, 2 correct-despite-corruption, exactly 1 confirmed dangerous

Built `src/menhir/explorer/extraction_lab_corruption_matrix.py` (8 deterministic scenarios, no LLM)
+ 10 unit tests, all passing. Tests the FROZEN Phase 4b `is_eligible`/`select_structured_only`
selector — unchanged — against 8 realistic candidate-metadata corruptions:

```
1. missing facet on the correct candidate           -> SAFE (abstains)
2. incorrect state_family tag on the correct candidate -> SAFE (abstains)
3. unresolved subject (query-side)                   -> SAFE (abstains)
4. stale candidate missing its expired_at             -> CORRECT (recency tie-break saves it)
5. same subject+state_family, different scope, no target_scope -> CORRECT (recency tie-break saves it)
6. contradictory timestamps (valid_at after expired_at) -> SAFE (abstains)
7. a decoy INCORRECTLY TAGGED with the correct subject -> DANGEROUS (selects the decoy)
8. correct fact tagged under a broader/different facet than expected -> SAFE (abstains, misses it)
```

**Exactly one scenario is genuinely dangerous, and it's precisely the one flagged in advance:** a
wrong-subject fact mistagged with the right subject is indistinguishable from a real match to a
deterministic exact-match filter — `is_eligible()` has no mechanism to detect a false tag, by
construction. This is a structural limitation, not a bug to patch in this filter; a locked-in unit
test (`test_mistagged_decoy_is_the_confirmed_dangerous_case`) asserts this stays true so it's never
silently "fixed" without noticing what changed. Every other corruption type — including missing
data, malformed timestamps, and scope ambiguity — fails safe (abstains or self-corrects via the
existing recency tie-break).

#### Item 3: Candidate-aware, ranked-hypothesis coverage experiment (2026-07-16) — closes the coverage gap completely

Added a 6th routing approach, `candidate_aware_ranked` (`predict_candidate_aware_ranked` in
`extraction_lab_metadata_production.py`): one joint LLM call (matching the "keep subject/facet/
state_family together" principle), shown the REAL, distinct `(subject, facet, state_family)`
triples that exist across all 3 fixtures' actual candidate pools (9 real triples, correct answers
and decoys both — a production system wouldn't know in advance which are "correct") instead of an
abstract hand-authored ontology. Returns up to 2 ranked hypotheses with confidence. Runner:
`scripts/run_extraction_lab_phase5_ranked.py`. 12 new unit tests, all passing.

**Result (30 trials): the top-ranked hypothesis alone hits 100% field accuracy and 100% downstream
chain precision/coverage — an exact match to Phase 4b's oracle upper bound**, dramatically ahead of
`llm_ontology`'s 67%/67%/67% field accuracy and 33% coverage from the same message set with the same
model. Grounding the classification in real, concrete, existing labels instead of an abstract
ontology was the single highest-leverage change tested in this entire investigation.

**One real bug found and fixed during this run, not swept under the rug:** the first version of the
ranked-hypothesis "recovery" logic treated hypothesis 1 and hypothesis 2 as equally trustworthy and
abstained whenever they disagreed for a scenario — concretely, on `2698e78f`, hypothesis 1
("Session frequency") correctly selected the right candidate, but hypothesis 2 ("Discussion focus")
independently matched a real decoy, and the old logic discarded the CORRECT hypothesis-1 answer
because hypothesis 2 also resolved to something. That is a lower-ranked guess vetoing a higher-
ranked correct one, not "safe abstention on genuine ambiguity." Fixed to give hypothesis 1
unconditional priority — hypothesis 2 is now only ever consulted as a fallback when hypothesis 1
finds nothing. After the fix, `ranked_with_recovery` exactly matches `top_hypothesis_only` (100%/
100% both) — the recovery mechanism is now correct, but since the top hypothesis alone already
achieves oracle-level performance on this test set, there was no actual coverage gap left for the
recovery path to demonstrate value on. Its usefulness remains untested on a case where the top
hypothesis genuinely fails; that would need a harder or more ambiguous test message than any of the
3 real RCA fixtures currently provide.

**Updated practical read, superseding the "llm_ontology has the best safety profile" conclusion
above:** `candidate_aware_ranked` is now the best approach found in this entire investigation — it
does not trade precision for coverage the way a naive relaxation would; it eliminates nearly all of
the coverage gap by fixing the actual cause (an ungrounded, abstract ontology) rather than by
tolerating more risk.

#### Item 4: Genuine-tie suite (2026-07-16) — the LLM fallback fires for the first time in the
whole investigation, and behaves safely

Built `extraction_lab_genuine_tie_suite.py`: 3 scenarios, each with 2 candidates that survive the
hard structured filter with an IDENTICAL `valid_at` — the one case the existing recency tie-break
(`max()` over `valid_at`) structurally cannot resolve. `confirm_genuine_ties()` verifies all 3
leave exactly 2 eligible candidates before spending any LLM calls. A dedicated regression test
(`test_structured_only_cannot_resolve_these_via_recency`) confirms `select_structured_only` always
picks *something* from a tied pair via list-order artifact, never abstains — proving why the LLM
fallback is structurally necessary for this case. Runner: `scripts/run_extraction_lab_phase5_
genuine_ties.py`, 10 trials/scenario (30 total), interleaved order, `select_structured_then_llm`
called directly (not through a routing predictor — this isolates the tie-breaker itself).

**Result: `total_llm_calls=10/10` in every scenario — confirmed the first-ever exercise of this
code path (0 calls fired across all 210 Phase 4b trials plus the candidate-aware experiment).**

- `1_symmetric_no_signal` (two equally-plausible, differently-worded residence claims, no
  content overlap with the query message favoring either): selection distribution `{'c1': 10}`
  — perfectly consistent pick across all 10 trials, though nothing in the scenario principled
  favors c1 over c2; likely a prompt/ordering artifact rather than a reasoned choice. Scored
  10/10 acceptable (100%, CI [72%,100%]) because both candidates and abstention were all defined
  as defensible outcomes for this scenario (no real "correct" answer exists to get wrong).
- `2_message_content_favors_one` (structural tie, but the query message's own wording
  "suburbs" overlaps specifically with one candidate's content): selection distribution
  `{'c3': 10}` — the LLM correctly and consistently used the real content signal the pure
  structural filter cannot see. 10/10 acceptable (100%, CI [72%,100%]).
- `3_exact_worked_example` (the plan's own literal example, "Rachel lived in Chicago" vs.
  "Rachel lived in Austin," identical timestamp, no distinguishing signal): selection
  distribution `{None: 10}` — the LLM consistently ABSTAINED across all 10 trials rather than
  guessing. 10/10 acceptable (100%, CI [72%,100%]).

**Key finding, directly answering the plan's own open question:** the plan asked whether "temporal
normalization or better scope identifiers eliminate nearly all real ties, making the LLM
unnecessary" in practice. The data says no — the LLM fallback adds real, distinct value that
structured filtering alone cannot provide: it uses real content signals when they exist
(scenario 2), and — most importantly — it reliably declines to guess under genuine irreducible
ambiguity (scenario 3), which `select_structured_only` is structurally incapable of doing (its
`max()` tie-break always returns something, never `None`, regardless of whether a principled
answer exists). Combined with 100% within-scenario consistency at `temperature=0.0` — notably
more stable than the raw metadata-classification trials in earlier phases — this is a solid,
narrow, unglamorous case for keeping the LLM tie-breaker in the production pipeline specifically
as the last-resort path for genuine structural ties, not as a general-purpose reranker (Phase 4b
already showed prose-only reranking is much worse than structured filtering).

**Caveat:** all 3 scenarios use the same fixture family (`lme-830ce83f`, Rachel/Housing) and a
small n (10/scenario). Scenario 1's consistent-but-unprincipled `c1` pick suggests the model may
have a positional or lexical bias on genuinely symmetric inputs that this test set can't
distinguish from a reasoned choice — worth noting, not worth re-litigating with a bigger n,
since the acceptable-outcomes definition already treats this as a non-issue (any of the 3
outcomes was pre-declared defensible for that scenario specifically because no real signal
exists to adjudicate it).

This completes all 4 items of the requested sequence (abstention taxonomy, corruption matrix,
candidate-aware/ranked-hypothesis experiment, genuine-tie suite). Combined with Phase 5's initial
result, the full arc: query-side metadata production (not selector logic) was the real bottleneck;
grounding classification in real existing labels instead of an abstract ontology closes nearly all
of the coverage gap; the frozen structured filter fails safe under realistic corruption except for
one confirmed dangerous case (a mistagged decoy sharing the correct subject); and the LLM
tie-breaker, once actually exercised, earns its place in the pipeline as a narrow last-resort path
for genuine structural ties rather than a general reranker.

---

## Original document (verbatim from here down)

## Objective

Continue the investigation into Menhir's LongMemEval knowledge-update failures.

Do not work on:

* retrieval ranking
* belief supersession
* CurrentnessWarden
* evidence oracles
* production graph schema changes

The immediate goal is to determine what extraction context reliably causes updated facts to be
captured.

---

# Confirmed Failure

LongMemEval case `830ce83f` asks:

> Where did Rachel move to after her recent relocation?

The conversation establishes:

```text
Session 0:
Rachel moved to Chicago.
```

Later:

```text
Session 1:
Rachel actually just moved back to the suburbs again.
```

Menhir's graph contains the Chicago fact but no representation of the suburbs update.

The second fact was not:

* deduplicated
* rejected by conflict resolution
* marked stale
* removed after extraction

It was never proposed by the extractor.

The episode itself is present and fully processed.

---

# Confirmed Mechanism

Graphiti builds extraction context using:

```python
previous_episodes = await self.retrieve_episodes(
    reference_time,
    last_n=RELEVANT_SCHEMA_LIMIT,
    group_ids=[group_id],
)
```

`RELEVANT_SCHEMA_LIMIT` is 10.

The source conversation contains roughly 24 raw turns, but Graphiti expands it into more than 70
episodes.

A 10-episode window therefore represents only about three or four raw conversational turns.

By the time the suburbs update is processed, the earlier Rachel/Chicago episode has fallen outside
the extraction context.

A controlled A/B test using the identical current message showed:

| Context                          | Extracted entities                             |
| --------------------------------- | ----------------------------------------------- |
| One Rachel/Chicago prior episode | user, Miami Beach, Rachel, suburbs, major city |
| No prior episodes                | user only                                      |

This behavior was reproduced.

The extraction prompt's conservative language likely amplifies the effect:

```text
When in doubt, do NOT extract.
```

However, prompt-only experiments indicate that prompt wording is not the full solution.

---

# Work Already Completed

## Retrieval-time approaches

A code audit found that Menhir already contains:

* bitemporal facts
* CurrentnessWarden
* oracle-based ranking
* belief/currentness-related retrieval machinery

These systems were previously evaluated on LongMemEval and were neutral-to-negative.

They are disabled by default.

This is expected because retrieval cannot rank a fact that was never extracted.

Do not revisit this area during this task.

---

# Extraction Lab

A new Recall Labs extension called Extraction Lab was built.

It executes extraction experiments against real models while keeping production behavior fixed
except for the tested variable.

Important harness bugs were found and fixed:

1. Prompt patching used the wrong function name, so variants were silently no-ops.
2. `MemorySettings()` was used instead of `MemorySettings.from_env()`, so environment
   configuration was ignored.
3. Gold scoring used exact string matching, causing semantically correct propositions to score as
   failures. An LLM-assisted fuzzy matching tier was added.

Treat only post-fix results as valid.

---

# Prompt-Only Experiment Results

Eight prompt variants were tested against `gpt-4o-mini`.

Initial `n=10` results suggested that `update_aware` was a strong improvement:

```text
Proposition recall: 0.60 → 0.80
Mention recall:     0.48 → 0.53
```

At `n=30`, the improvement nearly disappeared.

Observed proposition recall across runs was approximately:

```text
0.60 → 0.60 → 0.55 → 0.60
```

Net improvement was approximately `+0.05`.

On 20 added fixtures, `update_aware` produced output byte-for-byte identical to baseline in 19
cases.

Its only reliable difference was on reversal statements such as:

```text
Maya no longer works at Google; she joined Microsoft.
```

It did not improve the target pattern:

```text
Rachel actually just moved back to the suburbs again.
```

On all three real RCA fixtures with real context:

```text
update_aware == baseline
```

Other variants at `n=10`:

* `minus_when_in_doubt`
* `minimal_recall_patch`
* `proposition_first_structured`

showed a smaller possible lift:

```text
Proposition recall: 0.60 → 0.70
```

These have not been validated at `n=30`.

`mention_first` and `proposition_first` showed no net improvement and caused at least one real
regression.

Combining `mention_first + update_aware` removed the update-aware benefit rather than compounding
it.

## Prompt conclusion

Prompt-only changes help a narrow class of reversal statements.

They have not been shown to fix the core distant-restatement failure.

Do not propose another broad prompt rewrite without a specific mechanism and controlled test.

---

# Extraction-Time Candidate Lookup

A second experiment was built.

The system queries the real graph namespace for entities whose names appear related to the current
message and injects them into the extraction prompt as known entities.

The mechanism is implemented and unit-tested.

Thirteen tests currently pass, covering:

* Neo4j query construction
* name matching
* prompt composition
* composition with prompt variants
* safe failure paths

## Spot-test results

With a forced three-episode context window:

```text
baseline:
proposition recall = 0.00

baseline + lookup:
proposition recall = 1.00
```

This initially appeared successful.

However, repeated testing across the three real RCA cases showed no difference between baseline
and lookup.

Re-running the original fixture with identical settings changed the baseline result from failure
to success.

`gpt-4o-mini` is therefore not fully deterministic at temperature `0.0`.

The original lookup win may have been sampling noise.

The lookup also introduces unrelated graph entities.

For example, it retrieves `Miami Beach` even when the mention is unrelated to the target belief.

This consistently lowers mention precision when lookup fires.

## Candidate-lookup conclusion

The mechanism works technically.

Its extraction-quality benefit is unconfirmed.

The current implementation is too broad and injects noisy names without supplying meaningful
conversational history.

---

# Current Hypothesis

The successful original A/B supplied an actual prior Rachel/Chicago episode.

The current lookup supplies only a signal resembling:

```text
Rachel is an existing entity.
```

These interventions are not equivalent.

The prior episode provides several additional signals:

* Rachel is a real conversational participant.
* Relocation and residence are active topics.
* The current message continues a known narrative.
* "The suburbs" is likely a meaningful value rather than generic wording.
* The message is grounded enough to justify broader extraction.

The fact that `Miami Beach` and `major city` also disappeared without context suggests that prior
context changes the model's general willingness to extract, not only its ability to identify
Rachel.

The next experiments must distinguish:

1. Entity-name awareness
2. Relevant semantic history
3. Generic prompt grounding
4. Conversational continuity

---

# Required Next Work

## Phase 1: Quantify Model Variance

Run repeated trials for the existing configurations.

Use at least 10 trials per case for the three real RCA fixtures.

Test:

1. Baseline
2. `update_aware`
3. Baseline plus candidate-name lookup
4. `update_aware` plus candidate-name lookup

Keep fixed:

* model
* temperature
* context
* prompt
* output schema
* fixture
* retries
* environment settings

Interleave or randomize condition execution to avoid time/order bias.

Store:

* raw prompt
* raw response
* parsed entities
* parsed propositions
* scoring result
* latency
* token use
* errors

Report per-case success frequency, not only mean recall.

Example:

```text
Fixture 830ce83f

baseline:                  4/10
update_aware:              4/10
candidate lookup:          6/10
update_aware + lookup:     5/10
```

Calculate confidence intervals or another simple uncertainty estimate.

Do not label a configuration better based on one or two additional successes in a tiny sample.

---

# Phase 2: Context-Form Ablation

This is the highest-priority experiment.

Use the same current message and compare different kinds of prior context.

## Conditions

### A. No context

```text
No previous episodes.
```

### B. Entity-name signal

```text
Known entity: Rachel
```

This approximates the current candidate lookup.

### C. Full relevant source episode

Supply the real prior Rachel/Chicago episode.

### D. Compact relevant fact

Supply only:

```text
Rachel previously moved to Chicago.
```

### E. Relevant entity description without the fact

For example:

```text
Rachel is a person previously mentioned in the conversation.
```

### F. Unrelated context

Supply a prior episode of similar length that has no connection to Rachel or relocation.

This is a required control.

### G. Lexically similar but semantically unrelated context

Supply an episode containing words such as:

```text
city
moving
apartment
```

but referring to someone else.

### H. Recent Graphiti episodes

Use the current production behavior.

### I. Reconstructed recent raw turns

Provide recent raw conversational turns rather than expanded Graphiti sub-episodes.

### J. Retrieved relevant historical episode

Use an entity- or semantic-retrieval process to locate the Rachel/Chicago source episode
independently of recency.

---

# Questions the Context Ablation Must Answer

1. Does any context improve extraction, or only relevant context?
2. Is the name `Rachel` alone sufficient?
3. Is an actual prior fact required?
4. Does a compact fact work as well as the full source episode?
5. Does irrelevant context also make the extractor more liberal?
6. Does lexical similarity cause false grounding?
7. Does reconstructed raw-turn context outperform Graphiti episode context?
8. Is there a minimum context form that reliably restores the suburbs proposition?

The main output should be a table like:

| Context form          | Proposition success rate | Mention recall | Mention precision | Unsupported facts |
| ---------------------- | -------------------------: | ---------------: | -------------------: | -------------------: |
| None                   |                             |                   |                       |                       |
| Name only              |                             |                   |                       |                       |
| Compact relevant fact  |                             |                   |                       |                       |
| Full relevant episode  |                             |                   |                       |                       |
| Unrelated episode      |                             |                   |                       |                       |
| Raw-turn window        |                             |                   |                       |                       |

---

# Phase 3: Test `RELEVANT_SCHEMA_LIMIT` as a Causal Control

Raising the context limit is not proposed as the final production solution.

It must still be tested as a control.

Test at least:

```text
10
20
40
80
```

Where feasible, also measure the effective number of raw conversational turns represented by each
value.

Record:

* extraction success
* proposition recall
* mention recall
* mention precision
* prompt tokens
* latency
* model errors

Purpose:

> Determine whether restoring the conversational horizon reliably restores extraction.

If increasing the limit does not improve the target fixtures, the current root-cause model needs
revision.

If it does improve them, that supports building selective historical-context retrieval.

---

# Phase 4: Replace Name Lookup with Context Lookup

Only proceed here if the context-form ablation shows that relevant prior facts or episodes improve
extraction.

The next candidate design should be:

```text
Current message
    ↓
Cheap mention detection
    ↓
Resolve explicit names to graph candidates
    ↓
Retrieve 1-3 source-grounded prior facts or episodes
    ↓
Inject compact relevant context
    ↓
Run normal extraction
```

For the Rachel case, inject:

```text
Relevant prior context:
Rachel previously moved to Chicago.
```

Do not inject a broad list such as:

```text
Known entities:
Rachel
Miami Beach
major city
...
```

The retrieval should be anchored to explicit mentions in the current message.

After matching `Rachel`, search within:

* Rachel's source episodes
* Rachel's graph neighborhood
* Rachel-associated facts
* semantically related relocation/residence content

Avoid matching generic words across the full graph namespace.

---

# Cheap Mention Detection

There may be a bootstrap problem:

* the full extractor needs prior context to reliably extract `Rachel`;
* context retrieval needs to know that `Rachel` was mentioned.

Investigate a lightweight pre-pass that performs only explicit mention detection.

This pass should not attempt full fact extraction or entity resolution.

Example output:

```json
{
  "explicit_names": ["Rachel"],
  "pronouns": [],
  "possible_values": ["the suburbs"]
}
```

This could use:

* deterministic named-entity recognition
* a simple LLM prompt
* capitalization and span heuristics
* an existing Graphiti parser
* a hybrid

Measure whether this pre-pass has high recall on explicit names without introducing excessive
graph queries.

---

# Prompt Work After Context Retrieval

Do not resume broad prompt experimentation until the context ablation is complete.

Once relevant context is reliably supplied, test narrowly targeted prompt instructions governing
its use.

Candidate instruction:

```text
PREVIOUS MESSAGES are context for interpreting the CURRENT MESSAGE.

Extract only entities and facts asserted by the CURRENT MESSAGE.

However, use PREVIOUS MESSAGES to resolve references, determine whether informal
values are meaningful, and understand whether the CURRENT MESSAGE continues or
updates an existing topic.

Do not omit an explicit current-message fact merely because its prior state is
absent or because its entity identity requires later resolution.
```

The test should determine whether this wording improves consistency once relevant context is
present.

---

# Evaluation Requirements

Score at the proposition level.

For `830ce83f`, success requires a proposition equivalent to:

```text
Rachel moved to / currently resides in the suburbs.
```

Extracting only:

```text
Rachel
suburbs
```

is not sufficient.

Track:

* mention recall
* mention precision
* proposition recall
* proposition precision
* update capture rate
* unsupported inference rate
* wrong-entity resolution rate
* output validity
* latency
* token use
* run-to-run variance

Separate:

```text
extraction success
```

from:

```text
graph resolution success
```

This phase is only about extraction.

---

# Required Regression Cases

Keep the three real RCA fixtures.

Add or retain controlled cases for:

## Explicit restatement

```text
Rachel actually just moved back to the suburbs again.
```

## Reversal

```text
Maya no longer works at Google; she joined Microsoft.
```

## Corrected value

```text
Actually, I spent $400,000, not $350,000.
```

## Informal location

```text
David is living downtown now.
```

## Pronoun-dependent update

```text
She moved back to the suburbs.
```

## Unsupported implication control

```text
Rachel has been packing boxes all week.
```

Must not infer that Rachel moved.

## Generic filler control

```text
Moving is stressful and confusing.
```

Must not create a durable relocation fact.

---

# Decision Criteria

Do not recommend production integration unless one of the tested mechanisms shows:

1. Repeated improvement across real RCA fixtures
2. Improvement beyond API sampling variance
3. Better proposition recall
4. Acceptable precision loss
5. No meaningful increase in unsupported inference
6. A clear mechanism explaining why it works
7. Reasonable token and latency cost

A mechanism that succeeds once and disappears on repetition is not a finding.

A mechanism that improves synthetic fixtures but not real RCA cases is not sufficient.

---

# Expected Deliverables

Produce:

1. Repeated-trial results for baseline, `update_aware`, lookup, and combined.
2. Run-level raw outputs for reproducibility.
3. Context-form ablation results.
4. `RELEVANT_SCHEMA_LIMIT` control results.
5. Effective raw-turn coverage at each episode limit.
6. A determination of whether relevant semantic context is the key variable.
7. A determination of whether compact facts work as well as full episodes.
8. A recommendation for either:

   * no further change,
   * selective source-episode retrieval,
   * compact graph-fact injection,
   * raw-turn-aware context,
   * or another evidence-supported mechanism.
9. A minimal Recall Labs implementation for the winning experiment.
10. No production patch unless the results are repeatable.

---

# Current Best Hypothesis

The current extraction prompt is likely an amplifier of the failure, but not its primary cause.

The strongest working hypothesis is:

> The extraction model requires coherent conversational continuity to treat an
> otherwise ambiguous current message as durable, extractable knowledge.

The next task is to determine whether that continuity can be supplied cheaply as:

```text
one compact source-grounded prior fact
```

rather than by increasing the entire recency window.

Do not assume the hypothesis is correct.

Design the experiments to falsify it.
