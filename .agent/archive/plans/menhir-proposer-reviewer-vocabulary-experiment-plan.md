---
artifact_schema: 1
artifact_uuid: 78cc178f-d7b2-46fe-8e5d-fdf6d4a05dea
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Plan: Menhir Proposer-Reviewer Vocabulary Experiment

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

**Date:** 2026-07-26  
**Status:** CLOSED - VOCABULARY EFFECT CONFIRMED; PRODUCTION REJECTED. No further prompt-arm work is
planned; see the definitive 2026-07-26 rerun below.
**Projects:** Menhir (experiment) and, only after the offline gate passes, archolith-bench
(end-to-end scoring)  
**Plan ID:** `menhir-proposer-reviewer-vocabulary-experiment`  
**Related:** `.agent/reviews/menhir-scalar-consolidation-rca.md`,
`.agent/plans/menhir-scalar-investigation-register.md`

## Disposition

The experiment answered its question: constrained vocabulary increases correct current Views, but
it also significantly increases wrong current values and loses complete supersession history. The
claim-card/proposer-reviewer protocol must not be promoted.

Current scalar development uses deterministic slot reconciliation plus provenance- and
foundation-gated View authority. Any next benchmark should measure that shipped path after the
non-self binding fix (`1550c56`), not open another prompt-vocabulary arm from this plan.

## Why

- The current typed-scalar path asks three independent samples to discover the fact, choose its
  subject/property vocabulary, choose evidence bounds, and interpret its value. It then requires
  exact agreement over all of those choices.
- Measured surface drift accounts for 22.3% of failed decisions (attribute 16.9%, subject 3.1%,
  unit/scope 2.3%). Exact-span fragmentation accounts for roughly 35% of all failed decisions, with
  some compound cases appearing in both bands.
- Prompting all three samples to invent the same vocabulary has not worked. Global reconciliation by
  matching values risks false merges such as `eggs_stocked=30` with `milk_stock=30`.
- Test whether an asymmetric three-call protocol works better: call 1 proposes ephemeral claim cards
  and vocabulary; calls 2 and 3 independently verify those cards using their IDs, while retaining
  explicit rejection and omission paths.

The hypothesis is not that the first model is authoritative. It proposes the filing vocabulary; the
two reviewers independently decide whether the source supports the fact.

## Scope

### In scope

- A test-only proposer/reviewer boundary with the same three-call budget as production `k=3`.
- Three experimental arms that isolate vocabulary constraint from claim/span alignment.
- Deterministic parsing, decision traces, fake-LLM unit fixtures, and repeatable captured live outputs.
- End-to-end scoring through assertion binding and scalar folding before any production recommendation.
- Production settings for live trials: current model, temperature, token cap, `k=3`, and threshold 1.0.

### Out of scope

- Changing `gate_typed_scalars`, `source_key`, `TypedAssertion`, Neo4j schema, or production prompts.
- Persisting the first model's vocabulary or treating it as a durable slot ID.
- Lowering the agreement threshold.
- Committing a reviewer-created `UNLISTED_CLAIM` without an independent review.
- Solving true model silence when the proposer and both reviewers all omit a fact.
- Deploying a `SourceClaimAlias` or `ScalarSlot` design; a successful experiment earns a separate
  migration/production plan.

## Proposed Design

### Ephemeral claim card

Call 1 emits cards with test-local IDs:

```json
{
  "claim_id": "C1",
  "episode": 0,
  "subject": "user",
  "attribute": "team_size",
  "scope": "",
  "value_kind": "count",
  "unit": "",
  "operation": "absolute",
  "value": 5,
  "when": "",
  "stated_span": "I now lead a team of five engineers"
}
```

The parser applies the existing typed-scalar validation and grounding rules. Cards are never written
to the graph.

### Independent reviewer response

Calls 2 and 3 receive the original episodes plus the same claim cards, but never each other's output.
For each card they must return:

- `SUPPORTED`, `CORRECTED`, or `UNSUPPORTED`;
- their independently read subject, kind, operation, value, unit/scope, and time;
- their own exact grounded evidence span;
- optional `UNLISTED_CLAIM` rows for facts omitted by the proposer.

Reviewers use `claim_id` as the vocabulary/correspondence handle, but re-emit the factual fields.
Candidate order is permuted between reviewers. The prompt forbids treating a card as evidence and
states that rejection is expected.

### Initial commit rule

A proposed card passes the experimental gate only when:

1. both reviewers return `SUPPORTED`;
2. proposer and reviewers agree on the factual core after existing deterministic normalization;
3. every reviewer span grounds uniquely in the same episode; and
4. no reviewer maps two distinct readings to one card.

`CORRECTED`, `UNSUPPORTED`, ambiguous mappings, and reviewer-only `UNLISTED_CLAIM`s abstain in this
experiment. They are measured, not promoted. This retains three-source corroboration and prevents a
two-reviewer correction from silently becoming a new 2/3 policy.

The first card's attribute is the canonical display label for the test result only. Raw reviewer
labels/spans remain in the trace.

## Experimental Arms

Run all arms on the same fixture panel and production settings.

### Arm A - current baseline

`extract_typed_scalars_once` three times, followed by the current exact-`source_key` gate.

### Arm B - vocabulary only

Call 1 proposes the permitted subject/attribute/scope/unit vocabulary. Calls 2 and 3 independently
extract facts using those labels, but the existing exact-span gate remains unchanged.

Purpose: isolate improvement caused by removing label roulette. This arm should affect D4 but not D3.

### Arm C - full claim-card proposer/reviewer

Call 1 proposes claim cards. Calls 2 and 3 review by `claim_id`; their distinct grounded spans are
treated as evidence aliases for that ephemeral card.

Purpose: test the combined user proposal against both surface drift and span fragmentation without
changing durable identity.

### Arm D - vocabulary proposer + deterministic span alignment (added, see Addendum A2)

Arm B, then a deterministic alignment pass over its output (common span intersection, same episode,
at most one proposal per sample, abstain on ambiguity). Zero additional LLM calls over Arm B.

Purpose: the control for Arm C. Span fragmentation is already solved by arithmetic with zero
collisions; Arm C must beat THIS, not Arm A, to justify claim cards.

## Test Matrix

### Phase 0 - deterministic offline fixtures

Add fake-LLM fixtures for:

1. same fact, three attribute names (`team_size`, `lead_count`, `team_outing_size`);
2. same fact, nested/overlapping evidence spans;
3. same value but distinct claims (`30 eggs`, `30 milk`) - must remain separate;
4. different values from the same passage - must abstain;
5. proposer supplies a plausible wrong value and both reviewers reject it;
6. proposer omits a fact and reviewers return `UNLISTED_CLAIM` - must not commit;
7. one reviewer rubber-stamps while the other corrects or rejects - must abstain;
8. two legitimate claims in one sentence - must not collapse;
9. reviewer emits two readings for one card - conflicted/abstain;
10. identical rerun - deterministic decision and trace.

Suggested test file:
`tests/test_typed_scalar_proposer_reviewer.py`.

### Phase 1 - targeted live panel

- Use the seven ground-truth namespaces from the RCA, plus explicit D3/D4 positives and the eggs/milk
  negative control.
- Run at least 20 trials per arm; do not interpret an `n=6` movement.
- Rotate episode/card ordering and alternate arm execution order to reduce ordering/time bias.
- Capture every request, raw completion, parsed card/review, drop reason, and final decision as JSONL
  so gate changes can be replayed without additional LLM calls.
- Keep model, temperature, token limit, context, and three-call budget fixed across arms.

Suggested probe:
`scripts/probe_scalar_proposer_reviewer.py`.

### Phase 2 - end-to-end fold scoring

Only if Phase 0 is clean and Phase 1 improves the pre-registered target:

- run each arm in fresh isolated namespaces;
- bind assertions and rebuild ScalarStateViews;
- score final current Views against the question/ground truth;
- report per-View precision and per-producing-namespace accuracy separately;
- verify that duplicate aliases cannot double-apply a delta;
- do not infer production value from proposal/decision counts alone.

## Metrics and Acceptance Gates

### Primary metrics

- correct current View rate per namespace;
- wrong current Views by wrong attribute, wrong value, stale value, and wrong subject;
- per-View precision (`correct Views / all Views`);
- end-to-end useful namespace rate (`namespaces answering correctly / evaluated namespaces`).

### Diagnostic metrics

- attribute/subject/unit/scope drift rate;
- exact-span fragmentation rate;
- proposer omission rate, exposed by reviewer `UNLISTED_CLAIM`;
- reviewer rejection/correction rate;
- false acceptance rate on injected decoy cards;
- calls, input/output tokens, latency, and parse/drop rate.

### Go/no-go

Proceed to a production design only if all are true:

1. Phase 0 has zero false commits and zero distinct-claim collisions.
2. Arm C materially improves correct final Views over Arm A on the targeted panel; use the
   improvement-tail Fisher test and report confidence intervals.
3. Run the regression-tail test separately for wrong commits; improvement significance must not hide
   a precision regression.
4. No must-be-zero control commits across all trials.
5. Surface-drift failures fall by at least 50% in Arm B or C.
6. Span-fragmentation failures fall by at least 50% in Arm C.
7. Arm C final-View precision is not lower than baseline by more than 5 absolute percentage points.
8. Reviewer decoy rejection is at least 95%.

If vocabulary improves but claim cards do not, pursue D4 only. If claim cards improve decisions but
not final Views, stop: fold/slot behavior is absorbing or transforming the apparent gain. If proposer
omission is high, do not add authority to reviewer-only claims inside this experiment; design and test
a separately corroborated discovery round.

## DEFINITIVE CORRECTED RERUN + PHASE 2 RESULTS 2026-07-26

**Verdict: the first-model vocabulary hypothesis is real, but Arm B must not ship.** It triples exact
current-View recovery and eliminates target-slot fragmentation, but it also significantly increases
wrong current Views and retains the required update history in only 2/20 cells.

The earlier Phase 1 section below is retained as an instrumentation history. Its absolute values are
superseded because its scorer matched benchmark answers on value alone, allowed wrong slots such as
`total_attendees=5` to answer a team-size question, and did not give silent cells an explicit
denominator.

### Corrected Phase 1

**Run:** 10 namespaces x 10 trials x four arms, gpt-4o-mini, temperature 0.7,
`max_tokens=2048`, k=3, decoy-rate 0.30, seed 17. 832 actual calls, zero truncations. Arm B and D
share the same LLM trio. The corrected scorer recomputes every verdict from subject + attribute +
value, keeps silent trials in the denominator, requires all question-required values, and uses an
exact paired test for paired arms.

| arm | correct-slot required-value coverage | wrong-slot commits |
|---|---:|---:|
| A baseline | 22/120 = 18.3% | 3 |
| **B vocabulary** | **43/120 = 35.8%** | 17 |
| C claim cards | 29/120 = 24.2% | 12 |
| D vocab+alignment | 44/120 = 36.7% | 17 |

- B > A: **PASS**, p=0.0017.
- C > A: **FAIL**, p=0.1719.
- D > A: **PASS**, p=0.0011.
- C > D: **FAIL**, p=0.9878.
- D > B paired: D-only=1, B-only=0, **FAIL**, p=0.5.
- B/C/D decoy rejection: 100%.

The corrected conclusion is unchanged on mechanism: vocabulary earns Phase 2; claim cards and
separate span alignment do not.

### Phase 2 real assertion -> fold -> View replay

Arms A and B were replayed into fresh namespaces on a dedicated throwaway Neo4j through the real
`TypedScalarDecision -> TypedAssertion -> ScalarState fold -> ScalarStateView` path. The LME source
graph was read-only. There were 100 paired namespace/trial cells per arm.

| metric | A baseline | B vocabulary |
|---|---:|---:|
| exact correct current View | 10/100 | **30/100** |
| unique target-slot current View | 21/100 | **54/100** |
| missing target-slot current View | 77/100 | **46/100** |
| fragmented target-slot current View | 2/100 | **0/100** |
| wrong target-slot current View | 11/100 | **24/100** |
| precision among unique target-slot current Views | 47.6% | **55.6%** |
| required history + SUPERSEDES chain | **8/20** | 2/20 |
| egg/milk false merge | 0 | 0 |

Correct-current paired cells: B-only=26, A-only=6, both=4, neither=64;
**p=0.0002675**. B repairs 26/90 = **28.9% gross** of baseline current failures; after six
regressions the net repair is 20/90 = **22.2%**.

This gain does not clear the safety gates. Wrong target-slot current Views increased from 11 to 24:
B-only wrong=19, A-only wrong=6, both=5, neither=70; **p=0.00732**, a significant regression. Of
B's 24 wrong current Views, 10 are known historical values and 14 are unmatched to the benchmark's
required current answer.

The update-history gate is also fatal. B preserved both required endpoints plus a `SUPERSEDES`
chain in only 2/20 cells. In `031748ae`, it never retained both team-size values; the value 5 was
often emitted as outing/group attendance instead. In `c6853660`, it retained both coffee-limit
endpoints in only two trials.

### Interpretation

The user's ingest instinct was not the wrong tree. Shared filing vocabulary has to be introduced
before persistence; a fold cannot join observations that were never assigned to the same slot.
However, the current Arm B also shows reviewers the proposer's candidate value. On update passages,
that changes their task into “is this value supported somewhere?” A historical value is supported,
so two reviewers can honestly confirm it while failing to identify the current value or enumerate
the full timeline.

Do not wire Arm B into production. The next bounded experiment should make the vocabulary shared but
the values blind:

1. Call 1 emits test-local `slot_id` definitions and keeps its value readings private.
2. Calls 2 and 3 see the source plus slot definitions, never Call 1's values or spans.
3. All calls enumerate every grounded mention for each slot and label it
   `historical`, `current`, or `unknown`.
4. Vote on `(slot_id, normalized value, temporal role)`; spans remain evidence aliases.
5. Unsupported slots, temporal disagreement, and reviewer-only slots abstain.
6. Replay through this same Phase 2 harness.

Run a value-blind/no-temporal control beside the temporal version to isolate anchoring from timeline
enumeration. A four-call control (one vocabulary call plus three independent readers) should measure
whether the three-call budget itself is the limiting constraint.

---

## SUPERSEDED PHASE 1 RESULTS 2026-07-26 - retained for measurement history

**Run:** 10 namespaces x 10 trials x 4 arms, gpt-4o-mini, temp 0.7, max_tokens 2048, k=3,
threshold 1.0, decoy-rate 0.30, seed 17. 900 calls, 0 truncated. B and D **paired** (one shared
proposer+reviewer trio scored two ways), so the B-vs-D contrast carries no sampling noise.
Capture: `pr_final.jsonl`. Scorer: `scripts/score_proposer_reviewer_gates.py`.

### Gate verdicts

| gate | B vocabulary | C claim cards | D vocab+alignment |
|---|---|---|---|
| G2 beats baseline on recording the asked fact | **PASS** p=0.0126 | **PASS** p=0.0193 | **PASS** p=0.0287 |
| G3 no stale regression | **FAIL** p=0.0312 | PASS p=0.1829 | **FAIL** p=0.0312 |
| G6 (A2) claim cards beat alignment | -- | **FAIL** p=0.500 | -- |
| A2 alignment beats vocabulary (PAIRED) | -- | -- | **FAIL** p=0.695 |
| G4/G8 decoy rejection >= 95% | PASS 100% | PASS 98.6% | PASS 100% |

Per namespace-trial (69 cells): answered A=23, B=37, C=36, D=35. Stale-only A=1, B=7, C=4, D=7.

### G3 IS RETRACTED -- it measured history as if it were staleness

**The G3 failures above are an artifact of the scorer and must not be cited.** ScalarStateView
preserves full history by design: superseded versions are KEPT with `view_current=false` and linked
by `SUPERSEDES` (`view_repository.py:5`, `:694-709`). An assertion carrying an earlier value is the
raw material for that history, not a defect.

Worse, two panel questions REQUIRE both values:

    031748ae  "How many engineers do I lead when I just started my new role?
               How many engineers do I lead now?"          -> needs BOTH 4 and 5
    c6853660  "Did I most recently increase or decrease the limit?"
                                                           -> needs both endpoints

Those two namespaces produced the ENTIRE stale signal. G3 therefore penalised B and D for recording
exactly what the architecture wants and the questions ask for.

**The genuine defect is one level down and was never measurable in Phase 1.** In production
`031748ae` holds one view -- `team_size=4`, `view_current=true`, **zero SUPERSEDES chains** --
because the update to 5 was never perceived. A current view pinned to a superseded value is only
observable after folding. **G3 belongs to Phase 2.**

Fixed in `bdf8117`: `STALE` -> `HISTORICAL`, plus `required_values()` / `coverage()` scoring how many
of the values a question actually needs were recorded, with the full panel as denominator so silence
scores zero. Recording history now counts FOR an arm.

### Re-scored on coverage (architecture-correct)

| arm | coverage of needed values | vs baseline |
|---|---|---|
| A baseline | 32/105 = 30% | -- |
| **B vocabulary** | **47/105 = 45%** | **p=0.023** |
| C claim cards | 43/105 = 41% | p=0.075 |
| D vocab+alignment | 46/105 = 44% | p=0.032 |

Paired D vs B: 46 vs 47, **p=0.61**.

### Verdict

- **B and D beat baseline on coverage; C does not reach significance.**
- **Alignment adds NOTHING over vocabulary** (paired, p=0.61) -- this survived every metric revision
  and is the most robust result in the experiment.
- **Claim cards add nothing over alignment** (G6, p=0.50).
- No arm is cleared for production: the Phase 2 fold-level evidence does not exist yet, and G3 --
  the gate that actually matters for a knowledge-update system -- can only be run there.

### Health warning on this experiment's instrumentation

The scorer was wrong three times, each time in a way that changed the verdict: per-View precision
penalised recording true unlabelled facts; STALE penalised history the design intends; and
**still open** -- `classify()` matches on VALUE ALONE, ignoring the attribute, so `total_attendees=5`
scores as a correct answer to a question about engineers led. That last one inflates every arm's
score and is not yet fixed. Treat all absolute numbers here as provisional; the paired D-vs-B
comparison is the only result immune to it, because both arms share the same defect identically.

### Finding 1 - vocabulary is the whole effect; alignment adds NOTHING on top of it

D vs B is **paired** -- identical LLM output, only the grouping rule differs -- and it is 35 vs 37,
p=0.695. Deterministic span alignment genuinely repairs fragmentation in the BASELINE protocol
(measured separately: buckets 172 -> 146, commits 14 -> 18, zero collisions). But under a proposer
protocol the reviewers already anchor near the card's span, so there is nothing left to merge.
**D solves a problem that B has already removed.** These two results are not in conflict; they are
about two different input distributions, and that distinction is the load-bearing part.

### Finding 2 - the recall gain is bought partly with STALENESS, and that is this project's defect

All arms capture ~50% more true facts (23 -> 35-37 of 69). But stale-only cells rise 1 -> 7.
Both stale namespaces are the classic update pattern:

    031748ae  "you led 4 engineers. Now, you lead 5 engineers"
    c6853660  "You increased the limit (from one cup to two cups)"

**Mechanism (hypothesis, not yet confirmed):** showing a reviewer a card that already contains a
value makes it likelier to confirm that value, and when the source states an old AND a new value the
proposer sometimes cards the old one. Reviewer "independence" is preserved for *whether the source
supports the value* but not for *which of two stated values is current*. The decoy control does not
catch this: decoys corrupt a value to something the source never states, which reviewers reject 100%
of the time. A STALE value is genuinely present in the source, so a reviewer verifying "does the text
support this" correctly answers yes. **The control tests grounding; staleness is a recency failure,
which is a different question the experiment never asked.**

### What was predicted vs what happened

Prediction of record (written before the run): *D > B > A on CURRENT; C weakest proposer arm; all
decoy gates pass; H2 holds for all (D's earlier 4-vs-1 stale was small-n noise).* Scored 2 of 4:
decoy gates and beats-baseline were right; **D > B was wrong (tied), and the stale regression was
real, not noise.**

### Also corrected during scoring

The gate scorer initially computed G2/G3 as CURRENT-over-total-commits -- the metric retracted BEFORE
this run, which punishes an arm for recording true facts the benchmark does not label. The two
denominators give opposite verdicts (no arm beats baseline vs all three do), so this was decisive,
not cosmetic. Fixed in `820af72`; the pre-registration had already named per-namespace as primary.

---

## Addendum 2026-07-26 (Claude) - revised baselines, Arm D, decoy weighting

Added after measurements taken the same day the plan was written. Nothing in the design is retracted;
the motivation gets stronger and one arm is added.

### A1. The "Why" numbers above are superseded

Lines citing attribute 16.9% / subject 3.1% / unit-scope 2.3% and ~35% span fragmentation come from a
run that the RCA now marks NOT COMPARABLE: it counted 156 claims where a k=3 re-run of the same 7
namespaces finds 40, and the arithmetic indicates it was taken at **k=6** (`p^6` vs production `p^3`).
Re-derived at production k=3 with the corrected token budget (30 namespaces, 90 calls, 0 truncated,
n=145, `scripts/measure_absence_band.py`):

| band | superseded | re-derived |
|---|---|---|
| span fragmentation | ~35% | **43.4%** |
| overlap, DIFFERENT value | 3.3% | **9.7%** |
| true silence | -- | 46.9% |

And from `scripts/eval_span_alignment.py`, decomposing the 35 merged heads where all k samples
provably read the SAME span and the claim still failed:

| blocking axis | heads |
|---|---|
| **attribute** (alone or combined) | **26** |
| scope | 11 |
| subject | 7 |
| unit | 4 |
| **value** | **1** |

**This strengthens the plan's core hypothesis.** Once spans are aligned, the samples agree on the
NUMBER in 34 of 35 cases and disagree on the NAME. Vocabulary drift is not one contributor among
several -- it is essentially the whole residual.

### A2. Arm D is missing, and it is nearly free

Arm C solves span fragmentation with LLM claim cards. But span fragmentation has since been shown to
yield to **deterministic arithmetic**: group proposals sharing a common span intersection, same
episode, at most one per sample, abstain on ambiguity. Measured on frozen samples (30 namespaces):
buckets 172 -> 146, commits 14 -> 18, **0 collisions**, 0 conflicting-value heads committed.

So Arm C spends LLM calls and takes on anchoring risk to solve a problem already solved for free.

**Arm D - vocabulary proposer + DETERMINISTIC span alignment.** Run Arm B, then apply the alignment
pass to its output. Costs **zero additional LLM calls beyond Arm B** -- it is post-processing, and
`scripts/eval_span_alignment.py` already implements it.

Arm D is the control that tells us whether the claim-card machinery earns its complexity. If Arm D
matches Arm C, prefer Arm D: fewer moving parts, no anchoring surface on spans, and span grouping
becomes a pure function that can be unit-tested exhaustively rather than sampled.

**Revision to go/no-go gate 6:** span-fragmentation reduction in Arm C must be measured against
**deterministic alignment (Arm D)**, not against Arm A. Beating a baseline that a free arithmetic
pass already beats is not evidence for claim cards.

### A3. Decoy rejection is the only real independence check -- weight it accordingly

Measured: when two samples read the same span, they already agree on the value ~97% of the time
(value blocks 1 of 35 aligned heads). Therefore **"the reviewers agreed with the proposer's value" is
nearly uninformative** -- it is exactly what pure acquiescence would produce, and it would score well
under a rubber-stamping reviewer.

Consequences:
- Gate 8 (>= 95% decoy rejection) is not a secondary safety check; it is the primary evidence that
  reviewers are reading the source at all. Treat a decoy-rejection miss as arm-fatal.
- Raise decoy density above what a "few controls" reading of Phase 1 would suggest, and include
  PLAUSIBLE decoys (right magnitude, wrong attribute; right value, wrong subject) -- an implausible
  decoy is rejected by prior, not by reading.
- Report reviewer agreement rate on decoys and on real cards SEPARATELY. A single pooled agreement
  number cannot distinguish comprehension from compliance.

### A4. Smaller notes

- The commit rule correctly abstains when BOTH reviewers return `CORRECTED` and agree with each
  other. That is a deliberate refusal of a 2/3 majority (plan line ~98) and will surface as
  measured-but-unclaimed gain. Report it as its own bucket so the size of what is being declined is
  visible.
- Phase 0 fixture 3 (`30 eggs` / `30 milk`) is the right negative control: it is precisely the case
  that makes namespace-wide attribute reconciliation unsafe, and the reason the alignment design
  keeps value out of the grouping key.
- Frozen-sample tooling now exists (`scripts/freeze_scalar_samples.py`), so Phase 1 arms can be
  replayed against changed gate logic at zero LLM cost. Prefer it to re-running live trials.

---

## Alternatives Considered

- **Keep three independent extractors and reconcile afterward:** preserves independence but repeats
  the existing attribute/span correspondence problem and requires a riskier global merge.
- **Force later calls to copy the first vocabulary with no rejection:** maximizes nominal agreement
  but converts first-sample error into manufactured unanimity. Rejected.
- **Lower threshold to 2/3:** already rejected on precision.
- **Persist the first call's vocabulary immediately:** makes a stochastic label durable before it is
  corroborated. Rejected.
- **Fourth adjudication call:** potentially rescues reviewer-only claims, but changes cost and safety.
  Deferred until the three-call experiment measures proposer omissions.

## Risks

- **Anchoring/acquiescence bias:** reviewers may accept plausible cards because they are shown.
  Mitigate with wrong-value/attribute decoys, permuted order, explicit rejection language, and
  independently re-emitted factual fields.
- **First-proposer omission:** the vocabulary can exclude a true fact. Measure `UNLISTED_CLAIM`; never
  silently treat absence as agreement.
- **Loss of independence:** label choice is intentionally shared, but factual support must remain
  independent. Do not count copied labels as evidence.
- **Sticky bad vocabulary:** vocabulary is ephemeral in this plan and is persisted only under a
  separately reviewed durable-slot design.
- **Metric inflation:** proposal-level truth counts can overstate useful Views. The production decision
  is based on final fold output.
- **Overfitting to LME:** include ordinary Menhir episode controls before any production recommendation.

## Invariants

- Perception remains a transcriber; it may not create facts unsupported by admitted source evidence.
- Threshold remains 1.0 for factual support.
- Every accepted reading retains an exact grounded source span.
- A shown candidate is not evidence.
- Different values, operations, times, subjects, or ambiguous mappings abstain.
- `UNLISTED_CLAIM` is observable but non-committing.
- Flag-off/current production behavior remains byte-identical during the experiment.

## Validation

- Unit-test card/reviewer parsing, all verdict paths, grounding, normalization, ambiguity, and
  deterministic traces.
- Run the existing typed-scalar, assertion, fold, consolidation-audit, and scalar-view suites.
- Replay captured JSONL through the decision layer and prove identical results without LLM access.
- Use fresh namespaces for end-to-end arms; compare final graph assertions and Views, not log totals.
- Record both improvement and regression statistical tails with the hypothesis direction named.

## Docs To Update

During the test-only phase:

- this plan;
- `menhir-scalar-investigation-register.md` with the arm results and verdict.

Only after a production direction is approved:

- Menhir `.agent/architecture.md`;
- Menhir `.agent/data_models.md` if durable claim/slot aliases are introduced;
- Menhir `.agent/memory-governance.md` for the revised admission protocol;
- Menhir `CHANGELOG.md`;
- a separate migration/rollback plan for any identity or graph-schema change.
