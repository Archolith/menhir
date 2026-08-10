# Shadow Context Composition — Contrastive Judge Validation Experiment Results

**Date:** 2026-07-16
**Scope:** fourth step in the response chain, following the pairwise LLM-judge lab
(`.agent/reviews/menhir-shadow-llm-judge-lab-2026-07-16.md`), which found 3 pairwise binary-judge
prompt framings never both preserved recall AND rejected the "boundaries" `wrong_state_family`
decoy — the pairwise design lets a decoy look plausible in isolation, without ever competing
against the real answer.
**Proposal tested:** per direct instruction, a CONTRASTIVE design — one call per entity, message +
ALL real candidates shown together, forced structured extraction (subject/slot/value/evidence) for
the message and every candidate, then a comparative selection (`selected_candidate_ids`) rather
than an isolated yes/no per candidate. Explicit design constraint honored: scoring reads
**only** `selected_candidate_ids`, never the extracted `slot`/`same_slot` strings — that would
recreate the original exact-match failure.
**Status:** offline validation only. Nothing wired into production.

## Headline result: mixed — solved the specific case asked about, surfaced two new problems

**The boundaries decoy (and its category generally) was correctly rejected in all 3 test
cases** (`boundaries_rejected: 3/3`) — the first approach in this entire chain (similarity, 3
pairwise judge framings, now this) to cleanly separate that specific adversarial case. But
**true-positive recall dropped to 1/3** — the contrastive design introduced two new failure modes
neither similarity nor the pairwise judge exhibited.

## Method

New `src/menhir/explorer/shadow_contrastive_judge_lab.py` + `scripts/run_shadow_contrastive_judge_lab.py`.
Built 3 test cases (one per fixture family), each showing the message against **7 real candidates
together**: the true positive, one of each of the 5 real `DecoyType` categories
(`wrong_state_family`, `wrong_scope`, `stale`, `wrong_subject`, `missing_metadata`), plus one
cross-family "unrelated control" candidate (round-robin: each family borrows another family's true
positive as its own control). Every candidate came from the same already-reviewed Phase 4b
fixtures used in the prior two labs — the full battery the instruction asked for
(boundaries + true positive + normal update positives + other same-subject/wrong-topic negatives +
unrelated control), in a single contrastive call per family.

## Real results (all 3 cases, full raw JSON below)

**`lme-2698e78f` (the target case) — CORRECT.** `selected_ids=['lme-2698e78f_cand6']` (the true
positive, "sessions... every two weeks"). Zero negatives selected. The model's own extraction:
`{"subject": "user", "slot": "session frequency", "new_value": "every week", ...}` for the message,
correctly distinguishing this from the boundaries decoy's actual slot (discussion topics) —
the boundaries decoy wasn't even included in the model's own candidate extraction, meaning it
didn't consider it plausible enough to reason about further.

**`lme-830ce83f` — WRONG.** `selected_ids=['lme-830ce83f_cand3']` — but `cand3` is the
`missing_metadata` decoy ("Rachel mentioned something about her living situation changing"), NOT
the true positive (`cand4`, "Rachel previously moved to an apartment in Chicago"). The model's own
extraction for the selected candidate: `old_value="unknown"`. It picked the vaguer,
under-specified candidate over the concrete, clearly on-topic one — a new failure mode: not
fooled by a lexically-overlapping decoy, but by a deliberately ambiguous one that's trivially
compatible with any residence-change claim precisely because it says nothing specific.

**`lme-852ce960` — EMPTY, but for an interesting reason.** `selected_ids=[]` — yet the model's own
extraction correctly identified **two** real same-slot candidates: `cand2` ("$325,000... initially
pre-approved", the `stale` decoy) and `cand4` ("$350,000... previously pre-approved", the true
positive), both marked `same_slot=true`. Faced with two genuinely plausible same-slot candidates,
the model selected neither rather than picking the more recent one or flagging both. This may be
partly a test-design artifact: the recommended pipeline (temporal filter → similarity → judge)
would filter the `stale` candidate out **before** this stage in production, so the judge would
likely never see two same-slot candidates in the first place — this test intentionally included
the full decoy battery, which handed the judge a disambiguation problem (recency) that isn't its
job in the intended pipeline shape.

## Aggregate

| Metric | Result |
|---|---|
| Test cases | 3 |
| True positive preserved | 1/3 |
| Clean (positive preserved, zero negatives selected) | 1/3 |
| Boundaries-category decoy correctly rejected | 3/3 |

## A secondary, real limitation found: incomplete self-audit

The prompt instructs "for EVERY candidate fact given, extract the same shape" — but the raw
responses show the model only populated its own `candidates` audit array with the 1-2 entries it
considered plausible (`same_slot=true`), silently omitting the other 5-6 candidates it implicitly
rejected. This isn't a scoring problem (scoring never reads this array, by design), but it means
the audit trail is not trustworthy for understanding *why* a rejected candidate was rejected — only
for inspecting what the model considered a serious contender. Anyone building on this later should
not assume the audit array is complete.

## Findings

**1. The contrastive framing solves exactly the problem it was designed for.** Every attempt in
the prior labs (similarity threshold, 3 pairwise judge framings) either accepted the boundaries
decoy or rejected everything. Forcing the model to compare candidates against each other — not
just against the message in isolation — cleanly separated this specific adversarial case in all 3
tests, including the literal "boundaries" case that motivated this whole line of experiments.

**2. But it traded that fix for two new problems, both real:** favoring an under-specified decoy
over a concrete correct answer (830), and refusing to choose between two legitimately same-slot
candidates rather than picking the better one or surfacing both (852). Neither of these occurred
in the pairwise judge's failure modes — they are new, not a subset of the old ones.

**3. N=3 is too small to generalize from.** Each finding above is a single data point. The 852
case specifically may not recur once the temporal filter runs upstream of this stage as the
recommended pipeline specifies — worth re-testing with only temporally-valid candidates in the
pool before concluding the "two same-slot candidates" failure mode is a real production risk.

## Practical read

This is genuine progress, not a clean win. The contrastive design is the first approach in this
entire chain to solve the specific case it was built for, which is meaningful — but it is not yet
a drop-in replacement for anything, since it introduced two new, real failure modes on the same
tiny fixture set. The likely next iteration is not another prompt rewrite in isolation, but
**re-running this exact test with the `stale` candidate excluded from the 852 pool** (matching the
intended pipeline order) and a larger fixture set to see whether the 830 vagueness-preference
failure is a one-off or a pattern.

## Recommendation (still not integrating anything into production)

1. Re-run with `stale` candidates pre-filtered per family (matching the real pipeline order:
   temporal filter runs before this stage) to isolate whether the 852 non-selection was a
   test-design artifact or a real judge weakness.
2. Expand the fixture set beyond 3 families before drawing conclusions about the 830-style
   "vague decoy preferred over concrete answer" failure — one data point is not a pattern yet.
3. Only after both of the above, consider whether this contrastive shape is stable enough to
   prototype inside `shadow_context_composition.py` (still Stage 1 / shadow-only — no production
   wiring should happen before it is).

## References

- `.agent/reviews/menhir-shadow-llm-judge-lab-2026-07-16.md` — the pairwise judge experiment this
  responds to
- `.agent/reviews/menhir-shadow-semantic-similarity-lab-2026-07-16.md` — where the boundaries case
  was first found (similarity score 0.5794, above every true positive)
- `.agent/plans/menhir-context-composition-production-integration.md` — Stage 1 spec, cross-referenced
- `src/menhir/explorer/shadow_contrastive_judge_lab.py` — fixture assembly, prompt, parsing, scoring
- `scripts/run_shadow_contrastive_judge_lab.py` — runner (real LLM calls)
- `results/shadow_contrastive_judge_lab.json` — full raw responses and audit trails for all 3 cases
