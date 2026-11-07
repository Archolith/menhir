---
artifact_schema: 1
artifact_uuid: f536342a-0b56-475d-baf5-9e5f58c17fcf
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Menhir anchored time: attachment by the model, deterministic code as veto only

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Disposition: IMPLEMENTED (P1-P4 shipped, default off; P3 gate failed so P5 is not supported; P6 tracked in Archolith/menhir#281)
Last verified: 2026-10-21 - DONE (high); PR #275 MERGED 2026-10-12T23:02:44Z, merge commit 93285d20. Follow-ups #276-#283 are open issues.
P1, P2, P4 IMPLEMENTED (default off) in Archolith/menhir PR #275, MERGED 2026-10-12
(merge commit 93285d20; attach prompt 3aec9649).
P3 RUN: G1 and G2 FAIL with the regex guards off, so P5 is not supported (section 10.1 and
`.agent/reviews/menhir-anchored-time-attach-gate.md`). Decisions D1-D4 below are the user's; see
section 10 for what was assumed.

## 1. Goal

A time is written as a fact's `valid_at` only when a model has said, with a verbatim quote, that
the expression dates THIS fact (its start for a state, its occurrence for a point event).
Deterministic code only vetoes on signals it can check exactly. It no longer infers attachment
by parsing English clauses.

Why: PR #274 needed five review rounds. Each round found new wrong-date shapes, because
`clause_guard` and `state_start_guard` infer attachment with regexes over open-ended text
(`.agent/plans/menhir-anchored-time-resolver-p1-plan.md` sections 21-21.2). Two known residuals
remain: round-4 RS-1 ("Last week, I started PT for my back" dates "back is sore") and RS5-5.

Done when, at the end of P5: the attachment-inference code is gone, and the surface vetoes plus
the attachment check give 0 wrong writes on the 22 frozen and gate runs and on the adversarial set
(section 5, P1). Correct facts are no lower than at a0a9814 (2263/2374), unless the user accepts a
stated loss.

## 2. Constraints (from current code)

- Production makes one resolver call per episode
  (`graphiti_extraction_policy.py:1697`). It then runs `clause_guard` and `state_start_guard`
  (1710) and `plan_overlay` (1712), and mutates edges only after all results exist (1713-1716).
  The call is skipped with no edges, a non-user turn, no speech time, >40 edges, >4000 chars, or
  no regex cue (1673-1684).
- Any resolver failure keeps Graphiti's times and persists nothing
  (`graphiti_extraction_policy.py:1699-1705`; `graphiti_client.py:621`). The new check must
  fail the same way: no write.
- The resolver prompt is frozen (`anchored_time.py:5-6`). `PROMPT_VERSION` "a7797891" (22) is in:
  - the cache key (`anchored_time_resolver.py:82`);
  - the persisted `time_contract = at1|<version>|<model>` (`anchored_time_persist.py:115`);
  - `prompt_golden.json` (`tests/test_anchored_time_detect_parse.py:136-141`);
  - `tests/test_anchored_time_hook.py:138`.
- Frozen checkpoints hold raw resolver output (`ckpt_*.jsonl` in the workspace eval dir;
  `scripts/eval_anchored_time.py:140-146`). A resolver prompt change makes every checkpoint, every
  `PINNED_O2` row (`tests/test_anchored_time_replay.py:31-41`) and `prototype_expected*.json`
  stale.
- Gold is per fact `{basis, window}`. Scoring checks only whether the window midpoint is inside
  the gold window, and counts false dates on facts with no gold window
  (`scripts/eval_anchored_time.py:67-80`). It can judge whether a write is right. It cannot judge
  "start vs occurrence" or "which expression" except through the window.
- Live eval spend is capped at $0.10 per run (`scripts/eval_anchored_time.py:304-305`), at luna
  Flex rates (51-52).
- Persistence:
  - `time_outcome` is a free string with no enum (`anchored_time_persist.py:41-56`).
  - `guard` is not persisted.
  - Writes are write-once, so old edges keep their `at1` contract.
- Render: any outcome other than `written`, `graphiti_inside_window`, `state_start` and
  `graphiti_resolved` falls back to "event time unknown" (`event_time.py:15,67-79`). Only the
  `state_start` branch suppresses a Graphiti date inside the borrowed window.
- `plan_overlay`'s `state_start` reason merges two causes: the guard mark and "state window past
  the speech date" (`anchored_time.py:1120-1123`). They cannot be told apart once persisted.

## 3. Design

### 3.0 Prior art: the scalar proposer-reviewer experiment

The scalar work tested a model that drafts a claim and later calls that confirm it
(`archive/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md`). The confirmers anchored
on the draft: agreement rose without being independent. Correct-slot coverage went 18.3% -> 35.8%,
but wrong-slot commits went 3 -> 17, and it was rejected for production. There, agreement was used
to commit MORE. Here a second judgment may only block. Even so, a check shown the resolver's
answer would anchor on it, and once the regex guards are retired (P5) a rubber-stamping check would
be less safe than today. So the check is blind (3.1).

The scalar win from deterministic reconciliation came from surface drift: naming and span bounds,
which code can normalize. Attachment is a semantic relation, and five review rounds on PR #274
showed regexes cannot decide it. The lesson carried over is only the part that matches:
deterministic code for exact surface checks, model judgment for the relation.

### 3.1 The attachment check (shared by both options)

The check is blind: it never sees the resolver's expression, basis or kind. Given the TURN, the
speech date and ALL facts of the episode (not only the candidates, so it is not told which facts
were dated), it returns per fact:
- `expression`: the verbatim phrase of the TURN that dates THIS fact's start (a state) or occurrence
  (a point event), or null when none does. A causal "since" is not a time.
- `relation`: `start`, `occurrence`, `plan` or `none`.

A candidate is an item that reaches the point in `plan_overlay` where it would be written: dated
basis, closed window, every surface veto passed. Only candidates are decided; the other answers are
kept for the agreement metrics in section 5.

Deterministic vetoes. Each is exact; none parses clauses:
- V1 (agreement): the check's phrase, after `_fold`, is found in the turn and overlaps an
  occurrence of the resolver's expression (one contains the other at the same position). Two
  independent judgments must point at the same words.
- V2 (relation): a state needs `start`, a point event needs `occurrence`. `plan` and `none` never
  write.
- V3 (failure): an error, timeout, budget refusal, parse failure or a missing fact means no write
  (outcome `attach_unverified`).
- Disagreement (V1 or V2 fails) is outcome `attach_rejected`. The item keeps its basis, so other
  facts can still anchor on it. A drop like `causal_since` happens only through the surface vetoes,
  never because the check disagreed.

Comparing two short verbatim time phrases at the same position is an exact check. It avoids the
span-fragmentation that hurt the scalar k-sample gate, because both sides quote the same few words
of the turn and position overlap tolerates different bounds ("about two weeks ago" vs "two weeks
ago").
- The existing surface vetoes stay unchanged:
  - `plan`;
  - state window past the speech date (split out as its own outcome `state_future`);
  - `graphiti_inside_window`, `graphiti_resolved`;
  - the three ambiguities;
  - `would_invert_interval`.

New outcomes and their render:
- `attach_rejected` (V1 or V2) and `attach_unverified` (V3) render like `state_start`: never the
  borrowed window, and Graphiti's own date only outside it.
- Both keep the item's basis, so other facts can still anchor on it.

### 3.2 Option A: new fields in the resolver output (one call)

Add `relation` and a verbatim `evidence` span to `INSTRUCTIONS` (`anchored_time.py:60-109`) and its
two examples. Bump `PROMPT_VERSION`. V1 becomes "evidence is in the turn and contains the
expression", not agreement.
- For: no extra call or latency.
- Against:
  - Every checkpoint, pin and golden file must be redone (a new dev/held-out round).
  - Basis, offset and kind outputs can shift, so the whole resolver is re-validated, not only
    attachment.
  - It is not independent: the call that borrowed the time also vouches for it. This is the
    anchoring the scalar experiment measured (3.0), with nothing to break the correlation.

### 3.3 Option B: a separate attachment call for candidates only (recommended)

New pure module `src/menhir/infrastructure/anchored_time_attach.py` holds `build_messages`,
`parse` and `decide`. A small async caller mirrors `anchored_time_resolver.py` (same client, model
profile, Flex tier, backoff-aware timeout, label "anchored_time_attach"). The production flow
becomes:
1. Resolve.
2. Run the surface vetoes.
3. Collect the candidates.
4. If there are any, make ONE blind attach call for the episode (all facts, no resolver output).
5. Apply V1-V3 to the candidates.
6. Write.

Episodes with no candidate make no extra call. The blind call could run concurrently with the
resolver to save latency, but then it runs on every cue-bearing episode; start sequential and
measure (G3).
- For:
  - The resolver prompt, checkpoints and pins stay valid.
  - Evaluation needs attach calls only on candidate-bearing turns from the frozen runs, not a new
    resolver round.
  - Two independent judgments must agree before anything is written: a stricter AND, which suits
    the safety direction.
- Against:
  - One more call per episode with candidates (latency, spend, per-job budget).
  - Two prompts to maintain.
  - Shared model family: the two calls are independent in input, not in model. If they share a
    blind spot, both can pick the same wrong phrase. Measured by G1; D2 can pick a different model.

This differs from what I first suggested ("the extractor outputs it"). B keeps the frozen
resolver untouched and isolates the one new judgment, so it can be tested and costed on its own.

## 4. Steps (Option B; section 7 lists what changes under A)

| Phase | Files | What |
|---|---|---|
| P0 | none | Decide D1-D4. Run the n=500 first. |
| P1 | `scripts/build_attach_set.py` (new); workspace eval dir; `tests/fixtures/anchored_time/attach_adversarial.json` (new) | Build the two labeled sets (section 5). No API calls. |
| P2 | `anchored_time_attach.py` (new); `anchored_time_attach_client.py` (new); `anchored_time.py` (`plan_overlay` gains an `attach` decisions argument and the `state_future`, `attach_*` reasons); `graphiti_extraction_policy.py:1708-1716`; `settings_model.py`, `feature_flags.py` (flag `MENHIR_ANCHORED_TIME_ATTACH`, default off); tests | Mechanism behind a flag, with a fake client in the tests. The regex guards stay on (AND). |
| P3 | `scripts/eval_anchored_time.py` (`--attach` replays the frozen resolver output and calls attach live for candidates, under the same `--max-usd` cap) | Paid, small. Tune the attach prompt on the dev split; freeze it; run once on the blind split and the adversarial set. |
| P4 | `anchored_time_persist.py` (`time_contract` becomes `at2|<resolver>|<attach>|<model>`); `event_time.py` (render the new outcomes); tests | Persist and render. Old `at1` edges are unchanged. |
| P5 | `anchored_time.py` (delete the attachment parts of `clause_guard` and `state_start_guard`; keep a narrow `not_a_time` drop only if P3 shows the model misses causal "since"); `tests/test_anchored_time_guard.py`, `_state_start.py`, `_overlay.py` (tests that pin the removed inference, named in the changelog) | Retire the regex inference only after the P3 gates pass with the guards off. |
| P6 | settings / deploy docs | Flag on for the next LME run; compare against the n=500. |

P2 detail:
- `decide(turn, fact, item, answer)` is pure and holds V1/V2. The attach prompt never receives
  `items`; a test asserts that `build_messages` takes only turn, speech date and facts.
- The client returns `{i: answer}` or a failure status. It never raises, like the resolver
  (`anchored_time_resolver.py:111-128`).
- The candidate list is computed by `plan_overlay` in a dry pass, so "candidate" means exactly
  "would be written".

## 5. Evaluation

- Set 1, 22-run candidates (from frozen checkpoints):
  - Every item that is written at a0a9814, or that is blocked only by `clause`, `causal_since` or
    `state_start` (the attachment guards).
  - Each item is labeled right or wrong from the gold window. This covers the 4 historical wrong
    writes (K04.3, G17.1, G28.0 x2) once the guards are off.
- Set 2, adversarial:
  - Every reproducer from the PR #274 reviews (rounds 1-5, the `r*rev/c_*.py` probes) and the
    round-4 RS-1 shape, hand-labeled write/no-write with the correct date.
  - A held-back third is written fresh, after the attach prompt is frozen, by an agent that has
    not seen the prompt. This guards against fitting the benchmark.
- Split: tune on heldout + b11R/b11X candidates and two thirds of set 2. Blind: heldout2/3/4
  candidates and the fresh third.
- Measurement rules (from the scalar work, where the scorer was wrong three times and each time
  flipped the verdict):
  - Paired: every arm replays the SAME frozen resolver output; only the decision rule differs
    (regex guards on/off, attach check on/off). Report a paired count (A-only vs B-only) with an
    exact test, not two independent rates.
  - Explicit denominators: every rate states its denominator, including facts the check left
    unwritten and failed calls (V3). Silent cases stay in the denominator.
  - Real path: score through `plan_overlay` and the gold-window scorer
    (`scripts/eval_anchored_time.py:67-80`), never on candidate counts or the check's raw
    answers alone.
  - Scorer check first: before any gate, hand-verify the scorer on 10 written and 10 blocked
    candidates, including at least one historical wrong write.
  - Independence metric: on all facts, report how often the blind check agrees with the resolver
    (dated vs not, and same phrase). Near-total agreement on cases the regex guards block would
    suggest shared blind spots rather than independence.
- Gates. All are measured with the regex guards OFF, because P5 depends on that:
  - G1: 0 wrong writes on blind set 1 and blind set 2.
  - G2: correct facts on the 22 runs at least 2263, or a stated loss the user accepts.
  - G3: attach spend and calls per episode reported. Projected cost for the next LME run.
  - G4: every case where attach allows a write the regex guard blocked is hand-checked and listed.
- Cost: one attach call per candidate-bearing turn. Set 1 is a few hundred turns. Each live run is
  capped at $0.10 by the eval script. The exact count is in P1's output.

## 6. Risks

- Mitigable:
  - The attach model also borrows times. Handled by the blind agreement (V1), V2, the AND with
    surface vetoes, and G1 on a blind adversarial set. If G1 fails, keep the regex guards on (AND)
    and report it.
- Mitigable:
  - Anchoring (the scalar proposer-reviewer failure). Designed out by never showing the check the
    resolver's answer; measured by the independence metric in section 5.
- Acceptable:
  - Blind agreement misses writes where both calls are right but quote different, non-overlapping
    words. A miss, bounded by G2.
- Mitigable:
  - The per-job LLM budget is unknown here. Memory records `LlmBudgetExceeded` under a cap of 40
    per job, and the usage-callback consumers (`observability.py:111-160`) were not traced.
  - Trace them in P2. A refusal is V3: no write.
- Mitigable:
  - Gold cannot label start vs occurrence. Set 2's hand labels cover it; set 1 judges only through
    the window.
- Acceptable:
  - Mixed `at1`/`at2` contracts on edges (write-once). `time_contract` tells them apart; no
    reader depends on it.
- Acceptable:
  - More misses (default deny). Bounded by G2.
- Blocked until D1:
  - A and B touch different files and test sets.

## 7. If Option A is chosen instead

P2 edits `INSTRUCTIONS`/examples and `PROMPT_VERSION`. It regenerates `prompt_golden.json`
(generator `gen_fixtures.py` in the eval dir) and the hook test pin. P3 re-runs the resolver live
on all 22 sets: new checkpoints, new `PINNED_O2`, and re-validation of basis/offset/kind as well.
P4-P6 are the same. There is no attach client and no extra call.

## 8. Decisions (user)

- D1: Option B (recommended) or A.
- D2: attach model. The resolver model on luna Flex is the default; a stronger model costs more
  per candidate.
- D3: at P5, delete the regex attachment inference (recommended once G1-G4 pass), or keep it as an
  extra veto. Keeping it costs only misses, but keeps the rotating maintenance.
- D4: approve P3's paid runs (small, capped per run). Approval is asked before each run.

## 9. Validation per phase

- P1: set sizes printed. Each historical wrong write and each review reproducer is present.
  Labels are spot-checked by hand.
- P2: unit tests for V1-V3 and for each new outcome, with a fake attach client that returns
  wrong relations, missing facts, a phrase at another position or another occurrence, a phrase
  not in the turn, and errors. The production hook test shows no
  write on a failure. With the flag off, the replay is identical to a0a9814.
- P3: gate table G1-G4 written to `.agent/reviews/menhir-anchored-time-attach-gate.md`.
- P4: persist exact-key test (`tests/test_anchored_time_persist.py:69`) and render tests for the
  new outcomes. Throwaway Neo4j live test.
- P5: replay with the guards removed equals the P3 guards-off numbers. All anchored-time suites
  pass.
- P6: next LME run report compared with the n=500.

## 10. Progress (2026-10-12, PR #275, branch `feat/anchored-time-attach`)

Assumed decisions (the user said "Use delegated orchestration" without choosing): D1 = Option B;
D2 = the resolver model by default, `MENHIR_ANCHORED_TIME_ATTACH_MODEL` overrides; D3 deferred to
P5; D4 = ask before every paid run.

Commits: 0b86090 (P2 + P4), 1d60891 (test ASCII fix), 8b0737a (P1), e12da04 (changelog).

- P1:
  - Set 1: `scripts/build_attach_set.py` writes `attach_set1.json` in the eval dir.
    - 146 candidates over 106 run-turns (30 distinct turns): 140 right, 6 wrong; 48 tune, 98 blind.
    - Wrong: K04.3, G17.1, G28.0 x2, G21.4, G21.5. R29.2 is not a candidate, because the year rule
      blocks it.
    - So one P3 arm over set 1 is 106 attach calls.
  - Set 2: `tests/fixtures/anchored_time/attach_adversarial.json`.
    - 270 cases (93 write / 177 no_write) drafted by an executor from the review probes.
    - Checked mechanically, with 11 cases checked by hand; `t-christmas-hosting` was relabeled
      no_write because it is a plan.
    - 57 cases are `[UNSURE]`: they need the user's label review before G1.
    - The regex guards at a0a9814 still write 9 no_write cases: the RS-1 family x4, "have" plus an
      adjective read as a perfect x2, causal since x1, a "for a while" item x1, and
      t-driven-to-coast.
    - The fresh blind third is not written yet (after the prompt freeze).
- P2:
  - Implemented as planned, with these deviations:
    - `decide(turn, kind, resolver_expression, answer)` takes the item's kind and expression, not
      the fact text, which V1/V2 do not use.
    - `state_future` is split out only when the check is on, so flag-off outcomes stay identical.
    - The attach timeout reuses `MENHIR_ANCHORED_TIME_TIMEOUT_S`.
    - `ATTACH_PROMPT_VERSION` is the sha256 prefix of the prompt text, so any wording change bumps it.
  - The candidates come from a dry `plan_overlay` pass; a second pass applies the decisions.
  - Flag-off replay is identical to a0a9814 for all 2374 facts.
  - The usage-callback and budget consumers are still not traced; a budget refusal is V3.
- P4: `time_contract` is `at2|<resolver pv>|<resolver model>|<attach pv>|<attach model>`. Both
  models are recorded because D2 allows them to differ. The three new outcomes render like
  `state_start`.
- Verification:
  - New tests: 61 + 35 + 271 (set 2 integrity).
  - 11 hand-made mutants of the new logic, all killed.
  - Related suites: 1074 passed. Anchored-time suites with the eval dir: 709 passed, plus the
    known pre-existing `heldout3:frozen` failure.
- Next: P3, which needs approval for each paid run.
  - The eval script needs `--attach`: replay the frozen resolver output and call attach live only for
    candidate turns.
  - Review the `[UNSURE]` labels first.
  - The attach prompt in PR #275 is a first draft, unmeasured.

### 10.1 P3 result (2026-10-12)

Commits: 02122f3 (`scripts/eval_attach.py`), e3847f6 (prompt frozen at `b8995975`), bf872e1
(fresh blind third, 89 cases). Spend: $0.078 total (t1 $0.029, t2 $0.0325, blind $0.016), luna
Flex, 0 failed calls, no 429s. The full table is in
`.agent/reviews/menhir-anchored-time-attach-gate.md`.

- Tuning: t1 (`76651e6f`) left 12 wrong writes on set 2 in arm B; t2 (`b8995975`) left 3. The
  prompt was frozen after 2 of the 4 allowed tune runs.
- G1 FAIL: blind set 1 has 0 wrong of 88 writes. The fresh blind set 2 has 6 wrong in arm B (A has
  32, C has 6).
  - 4 are events that did not happen or are not asserted: negation, question, hypothetical,
    disputed claim.
  - 2 are V1 accepting a check phrase that adds a qualifier: "Sometime after 2020", "Each January".
- G2 FAIL as stated: arm B has 2254/2374 run-facts vs 2263 (-9). The losses are H19.5 x5 (a
  sub-event left undated) and K09.5 x4 (boundary drift rejected by V1).
  - That is 2 attach answers: the eval makes one call per distinct turn and reuses it across the
    turn's runs.
  - The p = 0.0039 reported first treated run-facts as independent and was wrong; with 2
    observations no p-value applies.
- G3: p50 6.6 s, p95 19.1 s; the call is made on 16% of cue-bearing run-turns; about $0.0001 per
  call.
- G4: R26.0 x2 (1 call), hand-checked correct.
- Decision for the user:
  - P5 is not supported.
  - Arm C (the PR as is, flag on) is never worse on wrong dates than A: wrong writes go 32 -> 6 on
    the fresh blind set. It loses 9 correct facts on set 1.
  - A further round (an assertion check plus the two V1 fixes) needs a new tune split and a new
    fresh blind set.
- After the gate, the prompt became `e6ecd4d1` (commit 0bf1908). The "none" definition now also
  covers facts the turn does not say are true.
  - It was found by mutating the prompt against f-016 and passed on round 1.
  - The paired rerun on the tune sets shows no loss: set 1 910/981 before and after; set 2 arm B
    WRONG 3 -> 2.
  - It has not been measured blind.
  - Extra spend: $0.034, for a total of about $0.112. Details are in the gate review.
- The prompt then became `3aec9649` (commit f29e779). Something done on a dated occasion in the
  next clause now gets that time, unless a word moves it.
  - It was found by mutating the prompt against H19.5 and passed on round 2.
  - Set 1: 910 -> 915 run-facts. That is H19.5's 1 attach call reused over 5 runs; the probes add
    5 of 5 calls. Set 2 with the guards on: no change. With the guards off, wrong writes go
    2 -> 4.
  - The main new wrong write, r5-X4-5, mostly predates the rule: written in 3 of 6 calls on
    e6ecd4d1 and 5 of 6 on 3aec9649. Its "sure" label is debatable.
  - Disputed claims (f-049) still get written.
- Two scope rules for r5-X4-5 were tried on top of 3aec9649 and rejected:
  - 7de1ba28, "an opening time phrase dates only the first clause": it loses r2-F4 with the guards
    on.
  - 99ca1a23, "a since phrase dates only its own clause": it adds 4 wrong "since" writes with the
    guards off and 1 with them on.
  - Prompt work on the "since" scope has stopped.
- Open: disputed claims; a fresh blind set for 3aec9649.
- Total spend about $0.238.
- Follow-ups filed after the merge (Archolith/menhir):
  - #276 disputed claims (f-049);
  - #277 relabel r5-X4-5;
  - #278 fresh blind set for 3aec9649;
  - #279 V1 qualifier extension (f-012, f-041);
  - #280 V1 boundary drift (K09.5);
  - #281 decide whether to enable the flag;
  - #282 trace the budget accounting of the attach call;
  - #283 investigate whether extraction stores facts from negated or hypothetical turns. The set-2
    facts were written by the fixture author, not Graphiti.

## Coverage

- Inspected:
  - `src/menhir/infrastructure/anchored_time.py` (prompt, parse, guards, overlay);
  - `graphiti_extraction_policy.py:1625-1716`;
  - `anchored_time_persist.py:112-116`;
  - `domain/event_time.py:15-80`;
  - `scripts/eval_anchored_time.py` (rates, cap, offline path).
  - A cited trace by an investigator agent of `anchored_time_resolver.py`, `graphiti_client.py`
    wiring, settings and flags, the persist contract, render consumers, the eval data layout and
    gold format, and the tests that pin prompt and guards.
- Not inspected:
  - `_place`/`compute` internals (unchanged by this plan).
  - `gate_score.py`, `run_r1.py`, `run_replay.py` in the eval dir (P1 reads them).
  - The usage-callback consumers (open, P2).
- Assumptions without code evidence:
  - Candidate-bearing turns are a small share of episodes. P1 measures it.
  - A focused attach prompt is more accurate than the resolver's own attach rule. P3 tests it.
