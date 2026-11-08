---
artifact_schema: 1
artifact_uuid: a634a38e-7ead-4ffa-aa0f-34b9cd680b51
artifact_type: review
artifact_status: COMPLETE
---

# Menhir anchored-time attach check: P3 gate (G1-G4)

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Date: 2026-10-12. Plan: `.agent/archive/plans/menhir-anchored-time-attachment-plan.md` (P3, section 5).
Code: Archolith/menhir PR #275, branch `feat/anchored-time-attach`. The prompt was frozen at
`b8995975` (commit e3847f6) after 2 tune runs. Model: gpt-6-luna, Flex.
Eval: `scripts/eval_attach.py` (commit 02122f3). Checkpoints: `attach_<set>_<tag>.jsonl`, not kept
in this repository.

Verdict: **G1 FAILS** (6 wrong writes on blind set 2) and **G2 FAILS as stated** (-9 correct
facts). P5, retiring the regex guards, is not supported. The check is still a large safety gain
when used together with the guards (arm C); see "What the data supports".

## Arms

All arms replay the same frozen resolver output; only the decision rule differs.
- A: regex guards (a0a9814, main).
- D: no guards.
- B: no guards + attach (the P5 target).
- C: guards + attach (what PR #275 ships when the flag is on).

Failed attach calls (V3) stay in every denominator. In all runs here there were 0 failed calls
(0/449 for the frozen prompt).

## Scorer check (before any gate)

- `--fake agree` gives B == D and C == A on every row; `--fake reject` makes B and C write nothing.
- Arm A reproduces a0a9814: 2263/2374 facts ok, 138 writes, 0 wrong.
- Hand-checked 10 written rows and all 8 guard-blocked rows, including the historical wrong writes
  G17.1, K04.3 and G28.0. Every written value lies in its gold window, and every blocked row is
  scored correctly.

## Set 1 (22 frozen runs, gold-window scorer)

| split | facts | A ok | D ok / wrong writes | B ok / writes / wrong | C ok / writes / wrong |
|---|---|---|---|---|---|
| tune (heldout, b11R, b11X) | 981 | 915 | 915 / 0 | 910 / 43 / 0 | 910 / 41 / 0 |
| blind (heldout2/3/4) | 1393 | 1348 | 1342 / 6 | 1344 / 88 / 0 | 1344 / 88 / 0 |
| all | 2374 | 2263 | 2257 / 6 | 2254 / 131 / 0 | 2254 / 129 / 0 |

How to count set 1: all numbers here are run-facts. The eval makes one attach call per distinct
turn and reuses that answer for every resolver run of the turn (`requests_for` makes one request
per turn; `score_runs` looks it up by turn id). Run-facts of the same turn under the same prompt are
therefore one attach observation, not several. A difference repeated across runs ("H19.5 x5") is
one call.

Paired comparisons (corrected 2026-10-12; the first version reported sign-test p-values as if
run-facts were independent):
- B vs A (all): B-only ok 0, A-only ok 9 run-facts. These come from 2 attach answers: H19.5 (1 call,
  5 runs) and K09.5 (1 call, 4 runs). With 2 independent observations no p-value means anything.
  The earlier "p = 0.0039" was wrong.
- Blind only: 0 vs 4 run-facts, which is 1 call (K09.5). The earlier "p = 0.125" was wrong.
- C vs A: identical counts.

## Set 2 (target fact per case; WRONG = a write with no_write label or outside the correct window)

| set | n | A WRONG / miss | D WRONG / miss | B WRONG / miss | C WRONG / miss |
|---|---|---|---|---|---|
| tune (review reproducers) | 270 | 9 / 47 | 153 / 3 | 3 / 6 | 0 / 48 |
| tune, sure labels | 213 | 5 / 34 | 119 / 0 | 1 / 1 | 0 / 35 |
| **blind fresh third** | 89 | **32** / 3 | 51 / 0 | **6** / 3 | **6** / 6 |
| blind fresh, sure labels | 86 | 31 / 3 | 49 / 0 | 6 / 2 | 6 / 5 |

How the fresh third was made:
- An agent fenced off from the attach prompt, the other cases and the results wrote it after the
  prompt freeze.
- I reviewed its labels before any attach answer existed; one was marked [UNSURE].

## Gates (regex guards OFF, arm B)

- **G1, 0 wrong writes on blind set 1 and blind set 2: FAIL.**
  - Blind set 1: 0 wrong of 88 writes. All 6 historical wrong writes are blocked.
  - Blind set 2: 6 wrong. All 6 are also wrong in arm A, which has 32 there.
  - f-016, "I did not go to the party last Saturday", dates "went to the party". This is negation.
  - f-018, "Did you finish the quarterly report last Friday?". This is a question.
  - f-015, "If I had moved to Berlin in 2018, ...". This is a hypothetical.
  - f-049, "The article claims that in 1997 ..., but I think it was found earlier". This is a
    disputed claim.
  - f-012: the resolver says "2020" and the check "Sometime after 2020". The check's longer
    phrase holds a qualifier the resolver dropped.
  - f-041: the resolver says "January" and the check "Each January". It is a habit, with the same
    qualifier pattern as f-012.
  - The first four are not attachment errors: the check correctly finds the phrase that would date
    the event, and nothing asks whether the event happened.
  - The last two are V1 accepting containment when the CHECK's phrase is the longer one. The same
    pattern appeared in tuning, in r1-L1 ("since early 2019" vs "2019").
- **G2, correct facts >= 2263: FAIL as stated.** B has 2254 (-9 run-facts, 0.4%), from 2 attach
  answers. These are misses, not wrong dates.
  - H19.5 (tune, 5 runs, 1 call): "exchanged numbers" at a meeting dated last Sunday. The check
    says "none" for the sub-event.
  - K09.5 (blind, 4 runs, 1 call): the resolver quotes "about 4 years and 3 months now" and the check "for
    about 4 years and 3 months". The phrases overlap but neither contains the other, so V1 rejects
    it. This is boundary drift.
- **G3, cost and calls.**
  - Frozen-prompt calls: 449. Spend: about $0.0001 per call at luna Flex rates.
  - Latency: p50 6.6 s, p95 19.1 s, max 62 s. It is sequential after the resolver.
  - Candidates occur in 106 of 649 cue-bearing run-turns (16%), and only those make the call.
    Projection: attach calls = 0.16 x resolver calls, and attach spend is about 1/6 of resolver
    spend per call.
  - Total P3 spend: $0.078.
    - Tune: $0.0034 + $0.0256 (run t1) and $0.0325 (run t2).
    - Blind: $0.0103 + $0.0059.
- **G4, attach allows a write the regex blocked.**
  - 2 run-facts, R26.0 in b11R g1a and g1b, from 1 attach call. Both write 2023-03-20, inside the gold window
    2023-03-13..2023-05-20.
  - Hand check: "I think it all started about 2 months ago when I joined a ... volleyball league"
    dates the fitness effort, so both are correct.

## Independence (frozen prompt, 2374 run-facts with an answer)

- The resolver and the check agree on dated vs not for 2240/2374 facts (94%).
- When both date a fact, they point at the same words in 635/653 (97%).
- On the 8 run-facts the regex guards block, the check agrees with the resolver on 2 (R26.0 x2, 1
  call, correct) and blocks the other 6 (the historical wrong writes).
- This is not the near-total agreement on guard-blocked cases that would suggest shared blind spots.
- The shared blind spot is elsewhere: facts that did not happen (G1 above). Neither call judges
  that.

## After the gate: prompt e6ecd4d1 (commit 0bf1908)

- How it was found: prompt mutation against f-016. Each round makes one edit to the frozen prompt and
  runs 14 calls: f-016 x3, 5 rewordings of the negation, and 6 controls that must still be written.
  - Round 0 (b8995975): f-016 was written in 3 of 3 calls, and "didn't make it", "never" and the
    mixed sentence were also written. "skipped", "cancelled" and "almost" were already not written.
    This is probably because their verb does not match the fact's, not because the model recognized
    the negation.
  - Round 1 passed: the "none" definition gained "or the TURN does not say the fact is true". All 6
    negation probes are not written, and all 6 controls are still written.
  - Probes are n=1 each (n=3 for f-016). I wrote them knowing the fix.
- Paired rerun on the tune sets (tag `m1`, 360 calls, 0 failed): no loss.
  - Set 1 (981 facts): 910 ok both before and after, 0 wrong writes, in arm B and arm C.
  - Set 2 (270 cases): arm B WRONG 3 -> 2 and miss 6 -> 5; arm C miss 48 -> 47. Neither changed case
    involves an event that did not happen, so these 2 gains are likely sampling noise.
- Not measured blind: this blind set has been seen. A fresh set should include events that did not
  happen and over-rejection traps (hedged, reported and reworded facts).
- Spend: $0.0018 for the mutation rounds and $0.0319 for the rerun. The P3 total is about $0.112.
- Decision: adopted, because it costs nothing measurable and fixes the main failure type.

## Prompt 3aec9649 (commit f29e779) and the confirmation round

- Confirmation on e6ecd4d1 (17 calls, $0.0011):
  - Questions and hypotheticals pass: f-018 and f-015 are not written in 2 of 2 calls each, and 4
    rewordings pass.
  - Disputed claims fail: f-049 is written in 1 of 2 calls, and "my uncle says he met Elvis in 1975,
    but nobody believes him" is written.
  - All 5 over-rejection traps are still written: an answered question, a real event with an "if"
    clause, a confirmed report, a plain report ("my sister says") and a hedge ("I think").
- H19.5 mutation (12 calls per round: H19 x3 with all 7 facts, 4 rewordings, 4 controls where the
  time must not carry over, and f-016):
  - Round 0 (e6ecd4d1): H19.5 is not written in 3 of 3 calls. The airport/emails rewording also
    fails, and the job-offer rewording quotes "on the spot".
  - Round 1: the sub-event rule now covers the next clause on the same occasion unless a word moves
    it. H19.5 is written in 2 of 3 calls, and both rewordings are fixed.
  - Round 2 passes: it adds "a purpose or plan inside the fact is not its event". H19.5 is written in
    3 of 3 calls, everything else passes, and all controls hold.
- Paired tune-set rerun of 3aec9649 vs e6ecd4d1 (tag `h2`, 360 calls, 0 failed):
  - Set 1: 910 -> 915 of 981 run-facts, 0 wrong writes, in arm B and arm C. The +5 is H19.5's 5
    runs sharing 1 attach call (corrected; the first version said "5 of 5 runs, p = 0.0625"). The
    stronger evidence is the probes: H19.5 written in 5 of 5 probe calls on 3aec9649.
  - Set 2, arm C: unchanged (WRONG 0, miss 47).
  - Set 2, arm B: WRONG 2 -> 4.
    - r5-X4-5 (sure): "Ever since the meeting on 3/4, my back is sore, and I'm job hunting" dates
      the job hunting. A later rate check showed this leak mostly predates the rule: written in 3 of
      6 calls on e6ecd4d1 and 5 of 6 on 3aec9649. In "Ever since X, A, and B" the opening phrase can
      cover both clauses, so the "sure no_write" label is debatable.
    - r5-E21 (unsure): a causal "since" with a perfect. The rate check found it written in 1 of 6
      calls on e6ecd4d1 and 0 of 6 on 3aec9649, so it is noise.
- Decision: adopted (the user's choice). It is a pure gain with the guards on; the leak in arm B
  matters only for P5.
- Spend: $0.0384, so the P3 total is about $0.150.

## Rejected: two scope rules for the r5-X4-5 leak

Both were measured on top of 3aec9649 with probe rounds plus a paired tune-set rerun. Neither is
adopted; 3aec9649 stays.

- **7de1ba28: "a time phrase that opens a sentence dates only the first clause after it"** (tag
  `s3`).
  - Probes: r5-X4-5 written 0 of 8 (3aec9649: 5 of 6), H19.5 5 of 6.
  - Tune rerun: set 1 915 -> 910 (H19.5, 1 call). Set 2 arm C miss 47 -> 48 (r2-F4, "Three weeks
    ago, I started physical therapy", 1 call). Set 2 arm B WRONG 4 -> 2.
  - Limiting the rule to actions instead (049cace9, probes only) did not fix r5-X4-5 (2 of 4).
- **99ca1a23: "a since phrase dates only the one clause it is attached to"** (tag `n1`).
  - Probes: r5-X4-5 4 of 4 ok, and everything else at its baseline rate.
  - Tune rerun:
    - Set 1: 915 -> 910 (H19.5, 1 call).
    - Set 2, arm B: WRONG 4 -> 6, with 4 new wrong "since" writes (r1-M3, r3-T-broken-ribs,
      r3-T-set-hours, t-driven-to-coast) and 2 new misses (r2-F1, r2-F3).
    - Set 2, arm C: 1 new wrong write (t-driven-to-coast, unsure) and 1 new miss (r2-F3).
  - The rule makes "since" phrases date facts more eagerly in general.
- Lesson: a few probe calls cannot certify a prompt; only the tune-set rerun caught both
  regressions. Prompt work on the "since" scope has stopped; r5-X4-5 is a labeling question.
- Spend: 7de1ba28 $0.0487 (probes $0.0129, rerun $0.0358); 99ca1a23 $0.0393 (probes $0.0039,
  rerun $0.0354). The P3 total is about $0.238.

## Follow-up issues (filed 2026-10-12, after PR #275 merged as 93285d20)

Archolith/menhir:
- #276: disputed claims.
- #277: relabel r5-X4-5.
- #278: fresh blind set for 3aec9649.
- #279: V1 qualifier extension.
- #280: V1 boundary drift.
- #281: flag decision.
- #282: budget accounting of the attach call.
- #283: extraction from negated or hypothetical turns.

Set-2 facts are written by hand, not extracted, so set 2 says nothing about what Graphiti stores.

## What the data supports

- **Keep the regex guards** (D3 / P5: no). B is not safe enough on fresh blind cases.
- **Arm C** (guards + attach, the PR as is, flag on) is never worse on wrong dates and much better
  on blind cases:
  - Blind set 2 wrong writes: 32 -> 6.
  - Set 2 tune: 9 -> 0.
  - Set 1: 0 -> 0.
  - The cost is 9 correct run-facts out of 2374 (2 attach answers; H19.5 is fixed since 3aec9649)
    and one extra call on 16% of cue-bearing turns.
  - Turning the flag on is the user's decision.
- **Next design questions** need a new tune round and a new fresh blind third, because this blind
  set has now been seen:
  1. Fact validity: the event did not happen, is asked about, is hypothetical, claimed, or
     habitual. This could go in the attach prompt (relation `none`) or be a separate check.
     It is not attachment.
  2. V1 direction: reject when the check's phrase strictly contains the resolver's with extra words
     (f-012, f-041, r1-L1). The cost is misses like "about two weeks ago" vs "two weeks ago".
  3. V1 boundary drift (K09.5): accept a partial overlap only when the overlap itself holds the
     time words.
