---
artifact_schema: 1
artifact_uuid: d82568dc-691c-42bb-b087-b378a25fc366
artifact_type: review
artifact_status: COMPLETE
---

# Menhir PR #274 anchored-time state-start guard: all review rounds (combined)

Combined on 2026-10-22 during a review-resolution audit. Moved into this repository on 2026-11-07. Only local paths and this header changed, in place, so each source below keeps its line count. The five per-round files were not moved.

PR #274 (fix/anchored-time-cross-sentence) went through five review rounds against commits a43c75e, f8bf270, 2d5a311, c0dbdb0 and d81901a, then a final fix 608c9a6; it merged as `a0a98148`. Each round re-checks the previous one, so the latest status of an id is in the status table below, not in the round where it was raised.

**Open now (all Low, untracked):** r4/R4-6, r4/R4-7, r5/R5-3, r5/R5-4.

## Sources

| Prefix | Source file (not moved here) | Starts at line | Summary status 2026-10-22 |
|---|---|---|---|
| `r1` | `menhir-pr274-state-start-guard-review.md` | 118 | Round 1 (a43c75e), confidence 78. Superseded by later rounds. |
| `r2` | `menhir-pr274-round2-review.md` | 543 | Round 2 (f8bf270), confidence 82. Superseded. |
| `r3` | `menhir-pr274-round3-review.md` | 1037 | Round 3 (2d5a311), confidence 80. Superseded; R3-5/R3-7 carried. |
| `r4` | `menhir-pr274-round4-review.md` | 1523 | Round 4 (c0dbdb0), confidence 82. Two Lows still open on main. |
| `r5` | `menhir-pr274-round5-review.md` | 1990 | Round 5 (d81901a), confidence 84. Mediums and residuals closed by 608c9a6; two Lows open. |

Line references inside a source (`file.md:N`) map to combined line `start + N - 1`.

## Status of every finding

Ids are prefixed by source so ids that repeat across sources stay distinct. "Src line" is the line in the original source file. Statuses come from the 2026-10-22 review-resolution audit; "not individually re-checked" means the audit did not trace that id, not that it is open or fixed.

| Id | Finding (as written in the source) | Status 2026-10-22 | Src line |
|---|---|---|---|
| `r1/M1` | (Medium) - `state_start` is shadowed by `graphiti_inside_window`, so the borrowed window is persisted and rendered as the event time | Fixed: state_start checked before graphiti_inside_window on main | 56 |
| `r1/M2` | (Medium) - "start/begin" anywhere in the expression's clause lets a borrowed time through, including when the co-mentioned event is itself a start | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 90 |
| `r1/M3` | (Medium) - `_PERFECT` matches non-perfect have/has/had anywhere in the sentence, so a causal "since" is kept and written | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 108 |
| `r1/M4` | (Medium) - "ever since X" is dropped as `causal_since` when the model's expression starts at "since" | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 128 |
| `r1/L1` | (Low) - "since" separated from the date, or inside the expression, is not seen; correct state starts get `state_start` (real case G19.2) | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 148 |
| `r1/L2` | (Low) - Temporal "since" with a non-perfect main clause is dropped as causal | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 169 |
| `r1/L3` | (Low) - `low.find(expr)` takes the first occurrence; a repeated expression is judged in the wrong sentence (fail-open) | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 185 |
| `r1/L4` | (Low) - Curly or straight apostrophe mismatch between turn and expression skips the guard (fail-open) | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 198 |
| `r1/L5` | (Low) - `str.lower()` can change string length, so offsets from `low` index the wrong text in `turn` | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 213 |
| `r1/L6` | (Low) - `_RUNS_TO_NOW` exempts any expression starting with "for" and any containing "now" | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 225 |
| `r1/L7` | (Low) - Timeline ordering uses the borrowed window of a `state_start` row | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 238 |
| `r1/L8` | (Low) - Index-bound sub-case of `test_items_outside_the_turn_or_undated_are_left_alone` passes vacuously | Superseded: re-checked in round 2 (r2 claim tables), then rounds 3-5 | 252 |
| `r1/I1` | (Info) - `guard_drops` and the persisted contract conflate guard kinds | Info; superseded by later rounds | 264 |
| `r1/I2` | (Info) - Test gaps | Info; superseded by later rounds | 275 |
| `r1/I3` | (Info) - `plan_overlay` docstring not updated | Info; superseded by later rounds | 291 |
| `r1/I4` | (Info) - Brief vs code on the since-clause exemption | Info; superseded by later rounds | 296 |
| `r1/I5` | (Info) - Dependents of a `state_start` anchor inherit the borrowed time | Info; superseded by later rounds | 304 |
| `r1/I6` | (Info) - Eval "resolver (P0)" column ignores the state-start mark | Info; superseded by later rounds | 312 |
| `r2/N1` | (Medium) - `_PERFECT` still treats "'ve got" / "have got" (possession and obligation) and -ed adjectives as perfect aspect, so a causal "since" is... | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 103 |
| `r2/N2` | (Medium) - The "outside a leading since-clause" rule depends on a comma; without it, or with "since" mid-sentence, a perfect inside the since-claus... | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 140 |
| `r2/N3` | (Medium) - The M2 fix only checks coordinate clauses of the same sentence; borrowed times with a start word still write | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 167 |
| `r2/N4` | (Medium) - Every-occurrence loop is quadratic; 8.9 s synchronous CPU per episode within production caps | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 201 |
| `r2/N5` | (Medium by the brief's rule; rare input) - U+0130 in the expression is no longer found, so a causal since is written (regression) | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 222 |
| `r2/N6` | (Low) - A since-clause without a comma, followed by a later comma, has its perfect cut away; a correct temporal since is dropped (regression) | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 239 |
| `r2/N7` | (Low) - Narrowed `_RUNS_TO_NOW` no longer exempts common running durations (regression) | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 257 |
| `r2/N8` | (Low) - Narrowed `_PERFECT` misses irregular participles, "'s"/"'d" + participle, and two-adverb perfects; temporal since dropped (regression for s... | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 277 |
| `r2/N9` | (Low) - "Strictest occurrence wins" uses substring matches and unrelated repeats; correct writes dropped (regression) | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 294 |
| `r2/N10` | (Low) - `_COORD` splits coordinated objects and verbs on " and ", so "started X and Y <time>" loses its write (regression) | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 314 |
| `r2/N11` | (Medium by the brief's rule; not a regression) - M1 fix is partial for render: a state_start row still renders Graphiti's date inside the borrowed... | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 330 |
| `r2/N12` | (Low) - Test gap: "strictest occurrence wins" is not pinned | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 353 |
| `r2/N13` | (Low) - Expression after a newline: `_main_clause` passes a negative start to `str.find` | Superseded: re-checked in round 3 claim table, then rounds 4-5 | 362 |
| `r3/R3-1` | (High) - Narrowed `_COORD` stops cutting at "and/but/so" before a non-subject word; the M2 borrowed-time write is back for common connectives | Closed (round-4 closure table) | 144 |
| `r3/R3-2` | (Medium) - Broadened `_RUNS_TO_NOW` exempts forward and bounded "for" phrases; a future date is written on a state | Closed (round-4 closure table) | 183 |
| `r3/R3-3` | (Medium) - The N1 fix is partial: -ed adjectives before an `_AFTER_VERB` word, and the newly added irregulars "broken"/"read", still make a causal... | Closed (round-4 closure table) | 216 |
| `r3/R3-4` | (Medium; partial fix of N4) - The guard is still quadratic in sentence length; 3.6-9.6 s on a single 4000-char sentence | Closed (round-4 closure table) | 246 |
| `r3/R3-5` | (Low by the brief's fail-safe rule; likely the most frequent loss) - -ed participle followed by a bare noun, gerund, name or number is no longer a... | Partial in round 4; carried into rounds 4-5 (see r4/R3-5) | 270 |
| `r3/R3-6` | (Low; regression vs f8bf270, accepted in the new docstring) - A leading since-clause without a comma swallows the main clause, so a temporal since... | Accepted miss (CHANGELOG) | 295 |
| `r3/R3-7` | (Low; regression) - The `_BACK_REF_START` lookahead also blocks relative and "it all" back-references followed by a gerund or "to" | Partial in round 4; carried (see r4/R3-7) | 314 |
| `r3/R3-8` | (Low; test quality) - The every-occurrence loop is no longer pinned; four new behaviours are unpinned | Superseded: re-verified by later rounds (see r5) | 328 |
| `r3/R3-9` | (Low; test quality) - A new test pins a correct-write loss as desired behaviour | Superseded: re-verified by later rounds (see r5) | 368 |
| `r3/R3-10` | (Low) - Render compares the UTC date with the borrowed window; an evening Graphiti date at the window edge renders as Graphiti's "own" date | Superseded: re-verified by later rounds (see r5) | 379 |
| `r3/RS-1` | (Medium by the brief's rule, N3b). Positive evidence is satisfied by any shared content word, | Residual; carried as RS4-2, then RS5-x (see r5) | 394 |
| `r3/RS-2` | (Medium by the brief's rule). A perfect anywhere outside the since-clause makes a causal since | Residual; carried as RS4-2, then RS5-x (see r5) | 400 |
| `r3/RS-3` | (Low). The N9 unrelated-repeat cases are still fail-safe losses: "I've lived in Denver since 2019. | Residual; carried as RS4-2, then RS5-x (see r5) | 406 |
| `r3/RS-4` | (Low). "she's moved to the night shift" ('s + participle) -> causal_since on both. "for the better | Residual; carried as RS4-2, then RS5-x (see r5) | 409 |
| `r3/RS-5` | (Low). N6 C1 "Since I moved to Denver in May I've tried every taco place, and I love it." and C6 | Residual; carried as RS4-2, then RS5-x (see r5) | 411 |
| `r3/RS-6` | (Medium by the brief's rule, pre-existing). A forward "for" duration with a number is exempt on | Residual; carried as RS4-2, then RS5-x (see r5) | 413 |
| `r3/RS-7` | (Info). The L1 ("since about February 20th" -> state_start) and L2 ("I work from home since March." | Residual; carried as RS4-2, then RS5-x (see r5) | 416 |
| `r4/R4-1` | (High) - The prepositional-since rule makes a causal "since" temporal when a subject pronoun, "my/our" or a conjunction follows the time | Closed (round-5 closure table) | 96 |
| `r4/R4-2` | (Medium) - Every-"and" cut plus the fronted-time extension: the start word comes from one coordinate clause and the shared word from the next | Closed by 608c9a6 | 163 |
| `r4/R4-3` | (Medium) - `_BACK_REF_START` now takes "which/that/this/everything started" + a gerund as a back-reference without checking that the antecedent is... | Closed (round-5 closure table) | 195 |
| `r4/R4-4` | (Low) - `_RUNS_TO_NOW` newly exempts long forward forms, and its explicit `more` slot keeps "for two more weeks" exempt | Closed by 608c9a6 | 225 |
| `r4/R4-5` | (Low; fail-safe) - The prepositional rule never fires when the resolver's expression starts with "since" | Closed (round-5 closure table) | 246 |
| `r4/R4-6` | (Low; fail-safe) - Past the occurrence cap, any "since" occurrence, even "ever since", drops the basis | **Open (Low, untracked)**: cap branch at anchored_time.py:904-905 unchanged; 'ever since' past 8 occurrences still drops | 258 |
| `r4/R4-7` | (Low) - The render margin hides Graphiti's own date one day outside the borrowed window | **Open (Low, untracked)**: event_time.py:131-140 _near_window still applies a one-day margin both sides | 277 |
| `r4/R4-8` | (Low; fail-safe) - Correct writes lost in c0dbdb0, and claims that hold only in part | Superseded: re-verified by later rounds (see r5) | 292 |
| `r4/R3-5` | , event-anchored half, is unchanged: "Since the meeting on 3/4, I've started running every day / learned | Round-4 view of r3/R3-5 (partial) | 301 |
| `r4/R3-7` | is only partly fixed. "My back went out. It all started going downhill last week." is still | Round-4 view of r3/R3-7 (partial) | 304 |
| `r4/R4-9` | (Low; test quality) - Mutation results for the new behaviour | Test quality; partly overlaps r5/R5-3 | 311 |
| `r4/RS4-1` | (Medium by the brief's rule; c0dbdb0 claim gap).** The CHANGELOG says side clauses "such as ', as I've | Closed for listed heads; remaining shapes became RS5-1, closed by 608c9a6 | 355 |
| `r4/RS4-2` | (round-3 residuals, unchanged). | Round-3 residuals unchanged in round 4; see r5 residuals | 369 |
| `r5/R5-1` | (Medium; looser than 2d5a311) - The start-word check can pass on two different "started": one in the earlier coordinate, one in the clause the fron... | Closed by 608c9a6 (probe on main returns state_start) | 116 |
| `r5/R5-2` | (Medium by the brief's rule; Low by likely frequency; looser than 2d5a311) - `_RUNS_TO_NOW` exempts 5+-token "for" spans that 2d5a311 did not, incl... | Closed by 608c9a6 (plan_overlay blocks state end > speech_date, anchored_time.py:1142) | 174 |
| `r5/R5-3` | (Low; test quality) - Mutants of the new code that the test file does not kill | **Open (Low, untracked)**: mutants unkilled; not re-run | 213 |
| `r5/R5-4` | (Low; dead code / claim precision) - The `;` handling in `_perfect_in_main_clause` cannot run | **Open (Low, untracked)**: dead ';' handling still at anchored_time.py:798 | 236 |
| `r5/R5-5` | (Low; fail-safe; CHANGELOG gap) - Correct temporal writes lost relative to 2d5a311 that the accepted-miss list does not name | Accepted miss (CHANGELOG-archive.md:94) | 248 |
| `r5/RS5-1` | (Medium) - A causal leading "since" becomes temporal through a perfect in a clause that the span rules | Closed by 608c9a6 | 268 |
| `r5/RS5-2` | (Medium) - Once "since" is judged temporal, the date goes on whatever fact the resolver attached it to. | Closed by 608c9a6 | 288 |
| `r5/RS5-3` | (Medium) - A relative or pronoun back-reference start never checks its antecedent.** `_BACK_REF_START` | Closed by 608c9a6 | 298 |
| `r5/RS5-4` | (Medium) - `CUES["duration"]` exempts a state even when the expression is forward.** The exemption at 856 | Closed by 608c9a6 | 305 |
| `r5/RS5-5` | (Low) - "since" at the end of a line.** "I'm job hunting since\nMarch, I've told you." writes 2023-03-16 on | Closed by 608c9a6 | 319 |
| `r5/Q1` | why the since path cannot be looser than 2d5a311 | Analysis section, not a finding | 323 |
| `r5/R4-1` | A1, A2, A4, A9, A11 (`c_r5b` R1) / causal_since / causal_since / yes | Round-5 disposition of the round-4 id (see r4 row) | 366 |
| `r5/R4-5` | every `c_r5a` row, bare vs "since" expression / - / identical pairs / yes (rule removed) | Round-5 disposition of the round-4 id (see r4 row) | 368 |
| `r5/R4-2` | C1, C2, C3 / state_start / state_start / yes | Round-5 disposition of the round-4 id (see r4 row) | 369 |
| `r5/R4-3` | D1, D2, D3, D5 / state_start / state_start / yes | Round-5 disposition of the round-4 id (see r4 row) | 371 |
| `r5/RS4-1` | like / so / because / coordinated head, and-no-comma tail, 'cause, then; side as/like/and/if/once/though/even though/because / written / causal_sin... | Round-5 disposition of the round-4 id (see r4 row) | 373 |
| `r5/R4-4` | for two more weeks / a couple more / a few more / one more / an extra / a further / two whole / written / state_start / yes | Round-5 disposition of the round-4 id (see r4 row) | 375 |


---

<!-- combined source: menhir-pr274-state-start-guard-review.md (prefix r1) -->

# menhir PR #274 review - state-start guard (fix/anchored-time-cross-sentence)

- Branch: fix/anchored-time-cross-sentence, one commit a43c75e on origin/main db990a1
- Clone reviewed: a local scratch clone
- Reviewer: Claude (read-only lane; nothing pushed, committed, or commented; no paid calls; no containers)
- Date: 2026-10-12

## Executive summary

The write-path invariant holds: an item marked `state_start` never reaches the `written`
branch of `plan_overlay`, dependents of a `causal_since`-dropped anchor fail safe
(`open_or_unplaceable`), the resolver cache is protected from in-place guard mutation, the
eval script runs the guards in production order, and the CHANGELOG relocation is a
byte-exact move. The focused tests pass (40 passed, 1 failed = the known pre-existing
`heldout3:frozen` parity failure).

Most important finding: the claim "the borrowed window is never rendered" does not hold.
`graphiti_inside_window` is checked before `state_start`, so whenever Graphiti's value lies
inside the borrowed window (always the case for "currently", "today", "this week" and similar
when Graphiti defaulted to the speech date), the item is persisted with
`time_outcome = "graphiti_inside_window"` and rendered as a verified event time. This happens
in the frozen eval data: K08.2 ("currently", gold basis none) renders as
`2023-05-22 (from 'currently')`, and G19.2 renders `~2023-04-24..2023-05-08`. Not a regression
against main (main rendered the same), but the guard's decision is lost for these items.

Other findings are regex and heuristic gaps, reproduced on concrete inputs. In some the guard
fails open: "start" in the borrowed event's clause, "have" as possession or "have to", the
first occurrence of a repeated expression, and curly or straight apostrophe mismatch. In
others it drops correct times: "ever" before an expression that starts with "since", "since"
inside the expression (real case G19.2), "since about/early ...", and non-perfect temporal
"since".

Counts: High 0, Medium 4, Low 8, Info 6.

## Test run

Command (from the clone, repo venv):

```
MENHIR_ANCHORED_TIME_EVAL_DIR=<eval-dir> \
  .venv/Scripts/python.exe -m pytest tests/test_anchored_time_state_start.py \
  tests/test_anchored_time_guard.py tests/test_anchored_time_replay.py -q -p no:cacheprovider
```

Output tail:

```
FAILED tests/test_anchored_time_replay.py::test_heldout_replay_matches_the_prototype[heldout3:frozen]
1 failed, 40 passed, 2 warnings in 1.74s
```

The one failure is the known pre-existing one, `('K09', 'windows')`.

## Findings

### M1 (Medium) - `state_start` is shadowed by `graphiti_inside_window`, so the borrowed window is persisted and rendered as the event time

- Location: `src/menhir/infrastructure/anchored_time.py:969-975`;
  `src/menhir/domain/event_time.py:15,67-68`;
  `src/menhir/infrastructure/anchored_time_persist.py:130-136`
- Mechanism: in `plan_overlay` the `graphiti_inside_window` branch (line 969) precedes the new
  `state_start` branch (line 974). A marked item whose window contains any of Graphiti's dates
  gets `reason = "graphiti_inside_window"`. `contract_rows` persists
  `time_outcome = result.reason` plus `time_window_start/end`. `EventTime.render` treats
  `graphiti_inside_window` as a `_WINDOW_OUTCOMES` member and prints the window with
  `(from '<expr>')`. The `guard` field is not persisted (`CONTRACT_PROPERTIES`, persist.py:41-56,
  has no guard column), so the renderer cannot tell this window was marked as borrowed.
- Trace (synthetic): turn "I fixed the fence this week, so the goats are safe.", item 1 = state
  with "this week". Guard returns `[1]` with `guard = "state_start"`. Result:
  `('graphiti_inside_window', 'state_start', 2023-06-12, 2023-06-18)`. Render:
  `~2023-06-12..2023-06-18 (from 'this week')`.
- Trace (real frozen data, `heldout3:frozen` K08.2): fact "The user currently uses a physical
  notebook...", item `{"expression": "currently", "basis": "speech_relative", "kind": "state",
  "calendar": {"which": "this", "unit": "day"}, "guard": "state_start"}`. Gold basis is
  `none`. Overlay reason `graphiti_inside_window`. Render: `2023-05-22 (from 'currently')`.
  Also `heldout2:frozen` G19.2 renders `~2023-04-24..2023-05-08 (from 'every morning since then')`.
- Impact: `valid_at` is never changed (correct), but the contract records the borrowed window as
  agreed-upon event time, and render-on recall shows it as such.
  `report.kept_inside_window` also counts these items. The PR's own invariant ("plan_overlay
  refuses to write it" and the borrowed window is not this fact's time) holds only for writes,
  not for the persisted outcome.
- Fix: move the `elif item.get("guard") == "state_start"` branch above
  `if any(start <= d <= end for d in g_dates)` (line 969). This changes no write, because the
  inside-window branch also keeps Graphiti's value. Keep it below nothing else, or place it
  after `graphiti_resolved`; either way `graphiti_resolved` still renders Graphiti's own date
  through `event_time.py:69-71`. Expect the `PINNED_O2` kept column to move:
  `heldout2:frozen` 13 -> 12 (G19.2) and `heldout3:frozen` 16 -> 15 (K08.2).
  Alternatively, persist the guard (`time_guard`) and exclude it in `render`.

### M2 (Medium) - "start/begin" anywhere in the expression's clause lets a borrowed time through, including when the co-mentioned event is itself a start

- Location: `src/menhir/infrastructure/anchored_time.py:757-759` (and `_STARTED`, line 708)
- Mechanism: `_clause_start_and_text(turn, pos)` returns the clause around the expression. When
  the time is borrowed, that clause belongs to the other event. Any start/begin word there
  satisfies the check for the state. Without commas the clause is the whole sentence.
- Trace: turn "I started fixing the fence three weeks ago and the goats are safe."; items 0
  (point_event) and 1 (state) both "three weeks ago". `state_start_guard` returns `[]`; overlay:
  `[('written', 2023-05-25), ('written', 2023-05-25)]`. This is the docstring's own motivating
  shape ("fixed the fence three weeks ago" for "the goats are safe") with "started" added.
- Impact: a false `valid_at` is written on a state (a wrong write on the path this PR is meant to
  close). Likely common: "I started a new job last week and my back is sore".
- Fix: require the start word to govern the fact, for example only accept `_STARTED` in the
  expression itself or in the clause segment between the fact's own content words and the
  expression. At minimum, do not accept `_STARTED` from a clause whose content words overlap
  another item's fact that shares this expression (the same data `clause_guard` already computes
  through `_content`).

### M3 (Medium) - `_PERFECT` matches non-perfect have/has/had anywhere in the sentence, so a causal "since" is kept and written

- Location: `src/menhir/infrastructure/anchored_time.py:707` (regex), `749` (`rest` = whole
  sentence minus the expression), `752`
- Mechanism: `\b(?:have|has|had)(?:n't)?\b` matches possession ("I have a long commute"),
  obligation ("I have to drive"), and auxiliaries in unrelated clauses. `rest` spans the whole
  sentence, not the main clause the "since" attaches to.
- Trace 1: "Since I started at the clinic on 2/15, I have a long commute every day." Item 1
  (state, event_anchored "Since I started at the clinic", anchor 0). Guard: `[]`. Overlay:
  item 1 `written 2023-02-15`.
- Trace 2: "Since I started at the clinic on 2/15, I'm hoping to get a parking spot, and I have
  to drive an hour." Fact "hopes to get a parking spot". Guard `[]`. Overlay writes
  2023-02-15. This is the docstring's own causal example shape ("Since I started on 2/15, I'm
  hoping"), defeated by "have to" in a later clause.
- Impact: a wrong `valid_at` on exactly the causal-since case the guard is meant to drop.
- Fix: require have/has/had (or 've/'s/'d) followed within a word or two by a past participle
  or "been" (`(?:have|has|had|'ve|'s|'d)(?:n't)?\s+(?:\w+\s+)?(?:been|\w+ed|\w+en)\b`), and
  restrict the search to the clause that is not the since-clause (the segment after the
  since-clause's comma, or before the "since").

### M4 (Medium) - "ever since X" is dropped as `causal_since` when the model's expression starts at "since"

- Location: `src/menhir/infrastructure/anchored_time.py:719` and `752`
- Mechanism: `_since_operator` first looks for `(ever\s+)?since\s*$` in the 12 characters before
  the expression; failing that it uses `_SINCE.match(expr)`. When the expression is
  "since the half-marathon" and the turn reads "ever since the half-marathon", the lookbehind
  sees "hurts ever " (no "since") and `_SINCE.match(expr)` returns plain "since". The
  "ever" sits outside both, so `_EVER_SINCE.search(since)` fails. With no perfect elsewhere, the
  item is dropped.
- Trace: "My knee hurts ever since the half-marathon on May 3rd."; item 1 state,
  event_anchored "since the half-marathon", anchor 0. Guard: `[1]`, `causal_since`, basis
  `none`; overlay `('guard', None)`. Control: the same item with expression
  "ever since the half-marathon" is not marked and is written 2023-05-03.
- Impact: a correct start time is erased, contradicting the PR rule "'ever since' is always
  temporal". Because the basis becomes `none`, any fact anchored on this one also becomes
  unplaceable. Models routinely trim the leading "ever" from copied spans.
- Fix: when `m` comes from `_SINCE.match(expr)`, also test
  `re.search(r"\bever\s+$", turn_low[max(0, pos - 8):pos])`; or compute the operator on
  `turn_low[max(0, pos - 12):pos + len(expr)]` with `_EVER_SINCE.search`.

### L1 (Low) - "since" separated from the date, or inside the expression, is not seen; correct state starts get `state_start` (real case G19.2)

- Location: `src/menhir/infrastructure/anchored_time.py:719` (`since\s*$` immediately before
  the expression, or `_SINCE.match` at the start of the expression)
- Trace (real data, `heldout2:frozen` G19.2): "...I bought it about three weeks ago ... and I've
  been using it every morning since then." Item `"every morning since then"`, event_anchored,
  state. "since" is mid-expression, so it is not detected. The state branch finds no
  duration, "now", or start word, so the item is marked `state_start`. Gold accepts
  event_anchored with window 2023-04-24..2023-05-22. It was not written only because Graphiti's
  baseline 2023-05-01 fell inside the window. With a speech-date baseline the correct write
  would be blocked.
- Trace (synthetic): "I've been sick since about February 20th." (expr "February 20th"),
  "I've lived in Austin since early 2019." (expr "2019"), and "I've lived in Denver since I moved
  here in May." (expr "since I moved here in May", explicit_date; `_SINCE_CLAUSE` exempts it
  from the operator path). All three are marked `state_start` and not written.
- Impact: lost correct writes (fail-safe direction). The CHANGELOG claim "no correct write lost"
  holds on the frozen runs only because G19.2 was rescued by the inside-window branch.
- Fix: search for `\bsince\b` within the expression too, not only at index 0. Allow up to one
  modifier word between "since" and the expression
  (`since\s+(?:about|around|early|late|mid|roughly)?\s*$`) and widen the 12-character window.

### L2 (Low) - Temporal "since" with a non-perfect main clause is dropped as causal

- Location: `src/menhir/infrastructure/anchored_time.py:751-752`
- Trace: each of "I work from home since March.", "Since last spring I run three times a week.",
  and "Since 2019 I live in Lisbon." gives guard `[0]`, `causal_since`, overlay reason `guard`.
- Mechanism: "since" plus a bare time noun phrase ("since March", "since 2019") cannot mean
  "because" (there is no clause after it), but the rule tests only the main-clause aspect.
- Impact: correct starts are dropped, and dependents become unplaceable. This is common in
  non-native and casual English. It follows the PR's stated rule, so it is a design gap,
  not a coding slip.
- Fix: treat "since" as causal only when it introduces a finite clause (a pronoun or determiner
  plus verb follows, which is what `_SINCE_CLAUSE` already approximates). "since" directly
  followed by the time expression (the lookbehind case) is temporal regardless of aspect.
  The test case "Since February 20th is my deadline" would then need its own rule: a time
  noun phrase followed by a finite verb.

### L3 (Low) - `low.find(expr)` takes the first occurrence; a repeated expression is judged in the wrong sentence (fail-open)

- Location: `src/menhir/infrastructure/anchored_time.py:745` (same pattern pre-exists in
  `clause_guard`, line 672)
- Trace: "I started a pottery class last week. My sister visited last week, so the guest room is
  full."; item 2 = state "last week" for "The guest room is full." `find` lands in sentence 1,
  whose clause contains "started". Guard `[]`, item 2 written 2023-06-08. Judged in its own
  sentence ("My sister visited last week") it would be marked.
- Impact: a borrowed time is written on a state.
- Fix: when an expression occurs more than once, evaluate every occurrence and mark the item if
  any occurrence fails (fail closed), or pick the occurrence whose sentence shares content
  words with the fact (`_content(facts[i])`).

### L4 (Low) - Curly or straight apostrophe mismatch between turn and expression skips the guard (fail-open)

- Location: `src/menhir/infrastructure/anchored_time.py:742-747`; `_norm` at `659-660`
- Mechanism: the turn is lowercased but its U+2019 characters are kept for `find`. Only `rest`
  is normalized (line 749). An expression the model returns with a straight apostrophe is not
  found, so the item is "left alone".
- Trace: turn "Since New Year\u2019s, I\u2019m hoping to run more."; item
  `"Since New Year's"`, event_anchored state, holiday anchor. Guard `[]`, written 2023-01-01.
  Control with a straight-apostrophe turn: guard `[0]`, reason `guard`.
- Impact: a causal since (or a borrowed state time) is written when the model normalizes
  apostrophes.
- Fix: normalize both sides before matching:
  `low = turn.lower().replace("\u2019", "'")` and the same inside `_norm`. Both are
  length-preserving, so offsets stay valid.

### L5 (Low) - `str.lower()` can change string length, so offsets from `low` index the wrong text in `turn`

- Location: `src/menhir/infrastructure/anchored_time.py:739,745,748-749,758` (pre-existing in
  `clause_guard` 665-684)
- Trace: `len("\u0130stanbul trip") == 13`, `len(lower) == 14`. With 12 U+0130 characters at the
  start of "... hotel. I fixed the fence today, then I started a garden, and the goats are safe.",
  `pos` in `low` is 50 against a true 38. `_clause_start_and_text(turn, 50)` returns
  `' then I started a garden,'`, so item 1 is not marked. The ASCII control turn marks `[1]`.
- Impact: a misplaced clause or `rest` window; either direction is possible. Rare input.
- Fix: use `turn.casefold()` only for matching through `re.search(re.escape(expr), turn, re.I)`
  to get positions in `turn` itself.

### L6 (Low) - `_RUNS_TO_NOW` exempts any expression starting with "for" and any containing "now"

- Location: `src/menhir/infrastructure/anchored_time.py:709,755`
- Mechanism: `^\s*for\b` accepts "for Christmas", "for my birthday", "for the move"; `\bnow\b`
  matches "two weeks from now". Neither is a duration running to now.
- Trace: "I fixed the fence before Christmas, and I'm staying with my parents for Christmas."
  The state item "for Christmas" (event_anchored, holiday) is not marked and is written
  2023-12-25. Here that is plausibly right, so this is a weak example: the regex exempts by
  the first token, not by meaning. A plain check confirms `_RUNS_TO_NOW.search("two weeks from
  now")` is True.
- Fix: replace `^\s*for\b` with the duration shape (`CUES["duration"]` already covers
  "for N units") plus `for (?:ages|years|a while)`, and use `\bnow\s*$` or `(?<!from )\bnow\b`.

### L7 (Low) - Timeline ordering uses the borrowed window of a `state_start` row

- Location: `src/menhir/infrastructure/memory_queries.py:636` and `648-653`
- Mechanism: with `event_time` on, facts order by
  `coalesce(r.time_window_start, toString(r.valid_at))` and are cut to `[0..20]`.
  `contract_rows` persists `time_window_start` for `state_start` rows (persist.py:130), so the
  co-mentioned event's date orders the state and decides whether it survives the 20-fact cap.
  The same applies to other non-written outcomes, which is pre-existing.
- Impact: misordered timeline and possibly a dropped fact in episodes with more than 20 facts.
- Fix: in `plan_overlay`, return `window_start=None, window_end=None` for `reason ==
  "state_start"`. Dependents are unaffected because they use `_place` on `items`, not results.
  Alternatively, order by the window only when `time_outcome IN ['written',
  'graphiti_inside_window']`.

### L8 (Low) - Index-bound sub-case of `test_items_outside_the_turn_or_undated_are_left_alone` passes vacuously

- Location: `tests/test_anchored_time_state_start.py:200-210` (item 7, line 206)
- Mechanism: item 7 "for my sister" is in the turn, but `_RUNS_TO_NOW` (`^\s*for`) exempts it
  anyway. Removing the `0 <= i < len(facts)` check from `state_start_guard` (line 743) would not
  fail the test.
- Proof: with eight facts so that index 7 is in range, `state_start_guard(turn, facts, {7: item})`
  returns `[]`.
- Fix: use an out-of-range item that would otherwise be marked, for example
  `7: _item(7, "a gift", "explicit_date", "state", date_="--05")` (in the turn, state, no
  start or duration).

### I1 (Info) - `guard_drops` and the persisted contract conflate guard kinds

- Location: `src/menhir/infrastructure/graphiti_extraction_policy.py:1708-1709`;
  `scripts/eval_anchored_time.py:168-171`; persist `CONTRACT_PROPERTIES` (41-56)
- `report.guard_drops` now lists `state_start` marks. Those items keep their basis and may end as
  `graphiti_inside_window` or `graphiti_resolved`, so they were not dropped. The log line
  `guard_drops=%d` overcounts drops. On the edge, `causal_since` persists as
  `time_outcome = "guard"`, the same as `clause`, and `state_start` keeps a dated `time_basis`.
  The guard name is not persisted. Consider a separate `report.state_start_marks` and a
  `time_guard` contract property.

### I2 (Info) - Test gaps

- No test covers `plan_overlay` precedence of `state_start` against `graphiti_inside_window`,
  `graphiti_resolved`, `year_ambiguous`/`two_options`, or `would_invert_interval`. All tests use
  a speech-date baseline outside the window, so M1 is not caught.
- No test covers "ever" outside the expression (M4), "since" mid-expression (L1), or
  possession "have" (M3).
- `test_heldout_replay_matches_the_prototype` (`_replay`, replay.py:55-81) runs only
  `clause_guard`, by design for P0 parity. The new guard is exercised on held-out data only
  through the `PINNED_O2` totals, which cannot localize a change.
- The tests that do exist fail when their rule breaks: the curly-apostrophe parametrization
  fails without the `.replace` at line 749, the `_SINCE_CLAUSE` exemption is pinned by item 0 of
  `test_a_date_inside_a_causal_since_clause_dates_that_clause_event`, and each
  `test_state_start_markers_keep_the_write` row exercises a distinct exemption branch. L8 is the
  exception.

### I3 (Info) - `plan_overlay` docstring not updated

- Location: `src/menhir/infrastructure/anchored_time.py:931-937`. It lists every write condition
  but not "the item is not marked `state_start`".

### I4 (Info) - Brief vs code on the since-clause exemption

- The review brief says a since-clause that is an event_anchored item's expression is not treated
  as the operator. The code does the opposite (`anchored_time.py:720`: the exemption applies when
  `basis != "event_anchored"`). The docstring (716-718) and tests
  (`test_causal_since_is_dropped_for_every_kind`, the event_anchored "Since I signed up ..." is
  dropped) agree with the code, so the brief's phrasing is inverted, not the code.

### I5 (Info) - Dependents of a `state_start` anchor inherit the borrowed time

- `_place` (434-476) ignores `guard`, so a fact anchored on a marked state is placed from the
  borrowed window and can be written. This is intended by the PR
  (`test_a_marked_anchor_still_places_its_dependents`). Note that the anchor's window is, by the
  guard's own judgement, another event's time. Usually that is the event the dependent's "that"
  refers to, but nothing checks it.

### I6 (Info) - Eval "resolver (P0)" column ignores the state-start mark

- `scripts/eval_anchored_time.py:189` scores `compute(...)` on the guarded items. `compute`
  ignores `guard`, so `state_start` items still count as resolver-dated (the `heldout2:frozen`
  resolver cell "22/23, 4/76" is unchanged while the written cell moved). This is consistent with
  P0 parity, but the column no longer reflects "what the resolver path asserts". The eval also
  passes no `speech_tz` to `plan_overlay` (line 177); this is pre-existing.

## Verified as correct (load-bearing for the brief's questions)

- No branch writes a `state_start` item: `new_valid_at` is set only in the final `else`
  (anchored_time.py:980-982), which follows the `state_start` branch. `kind == "plan"` precedes
  it, and the guard only marks `kind == "state"`.
- `state_start` does not shadow a branch that should win against a write: everything after it
  (`ambiguity`, `would_invert_interval`) is also a no-write. `time_ambiguity` is still persisted
  from `result.ambiguity` (persist.py:138), and `render` checks ambiguity independently.
- The guard does not override `graphiti_resolved` or `graphiti_inside_window` (Graphiti's dates
  stay untouched). The order issue is M1, which concerns render and outcome only.
- Dependents of a `causal_since` anchor fail safe. Reproduction: anchor item 1 dropped, dependent
  item 2 "two days after that" gets `('open_or_unplaceable', None)`, because `_place` returns
  `(None, None)` for basis `none` (lines 455-466 and 477).
- Rendering of `time_outcome = "state_start"` with a speech-date Graphiti default: outcome not in
  `_WINDOW_OUTCOMES`. `recorded == time_speech_date` because both are UTC
  (graphiti_client.py:632-633), so the render is "event time unknown (...)". `valid_at` is
  untouched (`edge.valid_at` is assigned only when `new_valid_at is not None`,
  graphiti_extraction_policy.py:1713-1715). The production hook test asserts
  `edges[1].valid_at == speech`.
- No other consumer switches on outcome values. Grep for
  `graphiti_inside_window|time_outcome|guard_drops|"guard"|state_start|causal_since` in `src/`
  found only anchored_time.py, anchored_time_persist.py:52,136, event_time.py:15,25,92,
  cypher.py:417 (projection), and graphiti_extraction_policy.py:1661,1708-1709,1718,1723,1726.
  No `.guard` reader exists outside anchored_time.py (grep `\.guard\b` over non-test files: none).
  `_with_world_end` and `MenhirEdgeExpiryHook` key on `reason == "no_item"` and `kind`, both
  unchanged by the guard.
- Mutation and aliasing: the guard replaces dict entries (`items[i] = {**it, ...}`) and never
  mutates inner dicts. `AnchoredTimeResolver` stores `_copy_items(items)` on insert and returns
  `_copy_items` on hit (anchored_time_resolver.py:93,130,136-138), so the cache never sees guard
  output. Re-running is idempotent on item state. A `causal_since` item becomes basis `none` and
  is skipped on rerun, so a rerun's return value would omit it. Production runs it once.
- Order: Python evaluates `set(clause_guard(...)) | set(state_start_guard(...))` left to right,
  so production (policy.py:1708) and eval (eval.py:168-169) both run clause_guard, then
  state_start_guard, then plan_overlay (policy.py:1710, eval.py:177).
- Case handling: `_SINCE_CLAUSE` has no `re.I`, but both inputs are lowercase (`expr` via `_norm`
  `.lower()`, `turn_low`). Non-int keys cannot reach the guard: `parse_items` keys through
  `_fact_index` and returns ints only (lines 776-806). Index bounds: `pos - 12` is clamped with
  `max(0, ...)`, and `pos + len(expr) <= len(low)` since `find` succeeded.
- `_PERFECT` "'s been" vs possessive or "is": a bare "'s" never matches (requires `\s+been`), and
  "'s been" is always "has been". "haven't/hasn't/hadn't" match. The over-match problem is M3.
- CHANGELOG relocation is a pure move. Script check: new archive equals
  `moved_block + "\n" + old_archive` (True), and the new CHANGELOG tail equals the old CHANGELOG
  minus the moved block with the trailing separator stripped (True). No non-ASCII characters in
  any added line of the diff.
- `PINNED_O2` change: `heldout2:frozen` written "19/23, 2/76" -> "19/23, 0/76", overridden
  11 -> 9, reproduced by `test_offline_eval_reproduces_the_pinned_o2_table` passing.
  Guard marks over the four frozen runs (reproduced by script):
  `heldout:fix H06.2 'after a long day of work' -> open_or_unplaceable`,
  `heldout2:frozen G17.1 'three weeks ago' -> state_start`,
  `G19.2 'every morning since then' -> graphiti_inside_window` (see M1, L1),
  `G28.0 '2018' -> state_start`,
  `heldout3:frozen K08.2 'currently' -> graphiti_inside_window` (see M1).

## Open questions (not proven)

- OQ1: CHANGELOG says "Offline over the 22 frozen and gate runs: wrong writes 4 -> 0 ... no
  correct write lost". I checked only the four `FROZEN_RUNS`. The b11R/b11X gate checkpoints and
  the other `ckpt_*` files in the eval dir were not rescored. To settle it, rescore every
  `ckpt_*.jsonl` with and without `state_start_guard` and diff the written cells.
- OQ2: frequency of M2, M3 and L3 on live traffic is unknown. The frozen runs contain no instance
  that flips a write, so these are reproduced only on constructed turns.

## Coverage table

Scope enumerated with `git diff --stat origin/main..HEAD`: 7 files. Line totals with `wc -l`:
1002 + 1878 + 310 + 252 + 135 + 99 + 2718 = 6394.

| File | Lines | Status |
|---|---|---|
| src/menhir/infrastructure/anchored_time.py | 1002 | read-deep (1-1002, all) |
| src/menhir/infrastructure/graphiti_extraction_policy.py | 1878 | read-light: 1620-1780 (`_apply_anchored_time`, `_with_world_end`, expiry hook head); rest NOT READ (unchanged) |
| scripts/eval_anchored_time.py | 310 | read-deep 1-219; 220-310 (live, paid path) NOT READ beyond grep showing no other guard call |
| tests/test_anchored_time_state_start.py | 252 | read-deep (all) |
| tests/test_anchored_time_replay.py | 135 | read-deep (all) |
| CHANGELOG.md | 99 | diff hunks read; move verified by script |
| CHANGELOG-archive.md | 2718 | diff hunk read; move verified by script; rest not read (unchanged) |

Rows: 7 = 7 enumerated files.

Out-of-scope files read to answer the brief: src/menhir/domain/event_time.py (all, 165),
src/menhir/infrastructure/anchored_time_persist.py (30-179),
src/menhir/infrastructure/memory_queries.py (600-669),
src/menhir/infrastructure/anchored_time_resolver.py (grep of items/cache lines), and
graphiti_client.py:630-633 (grep).

## Verification notes

- All reproductions ran through the clone's `.venv` Python with `PYTHONPATH=src`, against
  current branch code. Inputs and outputs are quoted in each finding.
- Not checked: live Neo4j persistence and the rendered recall output end to end (no containers by
  instruction). Render was checked by feeding `contract_rows` output into `rendered_event_time`.
- Not checked: `tests/test_anchored_time_guard.py` content (it ran and passed; unchanged by the
  PR).

## Review confidence: 78/100

The write-path invariants and every M finding are traced and reproduced, M1 on real frozen
data. Confidence is capped because only the changed region of graphiti_extraction_policy.py
and the scoring half of the eval script were read, and the "22 runs" claim (OQ1) was not
rescored.


---

<!-- combined source: menhir-pr274-round2-review.md (prefix r2) -->

# menhir PR #274 round 2 review - review-fix commit f8bf270

- Branch: fix/anchored-time-cross-sentence. HEAD f8bf270, parent a43c75e, base origin/main db990a1
- Clone: a local scratch clone
- Round 1: .agent/reviews/menhir-pr274-state-start-guard-review.md
- Reviewer: Claude (read-only lane; nothing pushed, committed, or commented; no paid calls; no containers)
- Date: 2026-10-12

## Executive summary

f8bf270 fixes every round-1 finding it claims (M1-M4, L3-L6, L8): each round-1 repro input was
re-run against HEAD and against a43c75e, and each one changes outcome as claimed (table below).
The 22-run offline replay is unchanged on writes: 2263 ok, 140 writes, 0 wrong writes, 0 harmful,
identical to a43c75e. Per-fact outcome changes are only reason relabels (graphiti_inside_window ->
state_start on G19.2 x4, K08.2 x4, R20.1, X09.6; R04.1 newly marked state_start, outcome
unchanged). Focused tests: 51 passed, 1 failed (the known pre-existing heldout3:frozen parity).

The fixes are narrower than their CHANGELOG wording. Wrong dates can still be written:

- "I've got a long commute" / "I've got to drive" and -ed adjectives ("I have mixed feelings",
  "I have limited options", "I have tired eyes") still count as a perfect. A causal "since"
  stays temporal and its date is written. This is M3's own shape with "'ve got" in place of
  "have" (N1).
- The "perfect read outside a leading since-clause" rule only works when the since-clause ends
  in a comma. Without that comma, or when "since" is mid-sentence, a perfect inside the
  since-clause still makes a causal "since" temporal (N2).
- The M2 fix (`_fact_elsewhere`) only looks within the expression's own sentence. A start word
  near the time still lets the time through when the fact is in the next sentence. It also lets
  the time through when the fact shares a word with the start clause, when the fact is
  paraphrased, or when the subject is expletive "it" ("It started raining yesterday") (N3).
- The new every-occurrence loop is quadratic. At the production caps (4000 chars, 40 edges) it
  takes 8.9 s of synchronous CPU inside `async _apply_anchored_time`, against 0.07 s on
  a43c75e (N4).
- `_fold` does not fold the expression the same way it folds the turn. A U+0130 in the
  expression now makes the guard skip the item, so a causal "since" is written. a43c75e
  dropped it (N5).

The commit also introduces several fail-safe regressions vs a43c75e. Each drops a correct write;
none occurs in the 22 runs:

- the since-clause comma split cuts out the real main clause (N6)
- the narrowed `_RUNS_TO_NOW` misses "for the past couple of weeks", "for two and a half years"
  and "for 2.5 years" (N7)
- the narrowed `_PERFECT` misses "'ve driven", "'ve eaten" and "have hardly ever missed" (N8)
- "strictest occurrence wins" counts substring hits with no word boundary ("2/15" inside
  "12/15", "last week" inside "last weekend") and unrelated repeats (N9)
- the `_COORD` " and " split drops "I started yoga and meditation last month" (N10)

Counts: High 0, Medium 6, Low 7, Info 6.

## Test run

```
MENHIR_ANCHORED_TIME_EVAL_DIR=<eval-dir> \
  .venv/Scripts/python.exe -m pytest tests/test_anchored_time_state_start.py \
  tests/test_anchored_time_replay.py tests/test_anchored_time_guard.py -q -p no:cacheprovider
FAILED tests/test_anchored_time_replay.py::test_heldout_replay_matches_the_prototype[heldout3:frozen]
1 failed, 51 passed, 2 warnings in 1.49s
```

Offline replay (`.venv/Scripts/python.exe -I ../xs_replay.py <tree> <out.json>`), with the parent
and base trees extracted by `git archive` into a private scratch directory (the clone was not
touched):

```
f8bf270  runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0
a43c75e  runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0
db990a1  runs 22 facts 2374 ok 2259 writes 144 wrong_writes 4 harmful 4
```

Per-fact diff a43c75e -> f8bf270 (written, value, reason, guard): no `written` or `value`
changes. Reason changes: heldout2 G19.2 (p1path120, p1l2, p1helper, frozen) and heldout3 K08.2
(p1path, p1path120, p1l2, frozen) graphiti_inside_window -> state_start; b11R:g1a R20.1 and
b11X:g1b X09.6 graphiti_inside_window -> state_start; b11R:g1a/g1b R04.1 guard None ->
state_start (reason stays open_or_unplaceable). The commit message's "no correct write lost vs
a43c75e" holds on these runs.

## Round-1 fix verification

Each row is a round-1 repro input run on f8bf270 and on a43c75e through the production order
(clause_guard, state_start_guard, plan_overlay), Graphiti baseline = speech date unless noted.

| Round-1 | Input | a43c75e | f8bf270 | Fixed |
|---|---|---|---|---|
| M1 | "I fixed the fence this week, so the goats are safe." item 1 state "this week" | graphiti_inside_window (guard state_start) | state_start | yes (render residual: N11) |
| M2 | "I started fixing the fence three weeks ago and the goats are safe." | item 1 written 2023-05-25 | item 1 state_start | yes, for this shape (gaps: N3) |
| M2 | "I started a new job last week and my back is sore." | written 2023-06-08 | state_start | yes |
| M3 t1 | "Since I started at the clinic on 2/15, I have a long commute every day." | written 2023-02-15 | guard causal_since | yes (gaps: N1, N2) |
| M3 t2 | "..., I'm hoping to get a parking spot, and I have to drive an hour." | written 2023-02-15 | guard causal_since | yes ("'ve got to" variant still writes: N1) |
| M4 | "My knee hurts ever since the half-marathon on May 3rd." expr "since the half-marathon" | guard causal_since | written 2023-05-03 | yes |
| L3 | "I started a pottery class last week. My sister visited last week, so the guest room is full." | item 2 written 2023-06-08 | item 2 state_start | yes |
| L4 | "Since New Year\u2019s, I\u2019m hoping to run more." expr "Since New Year's" | written 2023-01-01 | causal_since | yes |
| L5 | 12 x U+0130 + " hotel. I fixed the fence today, then I started a garden, and the goats are safe." | not marked | marked [1] (matches ASCII control) | yes for the turn side; expression side regressed (N5) |
| L6 | "for Christmas" state; "two weeks from now" state | not marked / written 2023-06-29 | state_start / state_start | yes (over-narrowed: N7) |
| L8 | item 7 "my sister" | n/a | mutant removing the bounds check fails the test (IndexError at `facts[i]`) | yes |

Not claimed and still reproducing on f8bf270: L1 ("I've been sick since about February 20th." ->
state_start), L2 ("I work from home since March." -> guard causal_since), L7 (plan_overlay still
returns `window_start/window_end` for state_start rows, anchored_time.py:1056-1057).

## Findings

### N1 (Medium) - `_PERFECT` still treats "'ve got" / "have got" (possession and obligation) and -ed adjectives as perfect aspect, so a causal "since" is written

- Location: `src/menhir/infrastructure/anchored_time.py:708-714` (`_PARTICIPLE` includes `got`
  and `[a-z]{2,}ed`; `_PERFECT` allows `'ve` before it), used at 784
- Mechanism: the participle list includes `got`. "I've got X" and "I've got to" are the most
  common possession and obligation forms, the exact meanings M3 was about. The catch-all
  `[a-z]{2,}ed` matches predicate and attributive adjectives after possessive "have": tired,
  mixed, limited, used, assigned. It also matches names ("Fred"). The optional `[a-z]+ly`
  adverb slot widens it further ("have really limited").
- Reproduced (item 0 = "on 2/15" or "on 3/4" point_event, item 1 = event_anchored state
  "Since ..." anchored on fact 0):
  - "Since I started at the clinic on 2/15, I've got a long commute every day." -> marked [],
    item 1 `written 2023-02-15`
  - "Since I started at the clinic on 2/15, I've got to drive an hour each way." -> written
    2023-02-15
  - "Since I started at the clinic on 2/15, I'm hoping to get a parking spot, and I've got to
    drive an hour." -> written 2023-02-15 (M3 trace 2 with "'ve got to")
  - "Since the meeting on 3/4 went badly, I have mixed feelings about the job." -> written
    2023-03-04. The same holds for "I have limited options at work.", "I have tired eyes from
    worrying.", "I've mixed feelings ...", "I have really limited options.", and "I have Fred
    helping me.". Control: "Since the meeting on 3/4 went badly, I want a new job." -> causal_since.
  - `_PERFECT.search("i've got a car")` -> "'ve got"; `"i have tired legs"` -> "have tired".
- Real data: the shape is in the 22-run corpus. b11X X06 (g1a and g1b): "Since I rearranged the
  furniture, I've got a nice little reading nook ...". Items X06.0 and X06.2 have expression
  "Since I rearranged the furniture" and get guard None on f8bf270, because "'ve got" counts as a
  perfect. Nothing is written only because the anchor is undated (reason open_or_unplaceable).
  With a dated anchor ("... the furniture on Sunday") the "since" would be written as a start.
- Impact: a wrong `valid_at` (the anchor's date) on a causal-since state, the case the guard is
  meant to drop. The output is the same as a43c75e and main, so the M3 fix is partial rather than
  regressed.
- Fix: drop `got` from `_PARTICIPLE` (keep `gotten`), and do not accept `have got` / `'ve got`
  unless followed by a verb participle. Require the -ed word not to be followed by a noun, or
  accept `[a-z]{2,}ed` only when the next token is a determiner, preposition, pronoun, or the end
  of the clause. The cheapest safe option is to accept -ed participles only after an explicit
  auxiliary contraction or "have/has/had" when the next token is not a plural noun. At minimum,
  remove `got` and add tests for "I've got a long commute" and "I have mixed feelings".

### N2 (Medium) - The "outside a leading since-clause" rule depends on a comma; without it, or with "since" mid-sentence, a perfect inside the since-clause makes a causal since temporal

- Location: `src/menhir/infrastructure/anchored_time.py:759-767` (`_main_clause`)
- Mechanism: the since-clause is removed only when `_sentence_initial(turn, at)` holds and a
  comma follows. If either fails, the whole sentence minus the expression is searched, including
  the rest of the since-clause after the expression. A non-`_LEAD` opener ("Honestly,") makes the
  since non-initial.
- Reproduced (item 1 = event_anchored state "Since the meeting on 3/4", anchor fact 0 "on 3/4";
  speech 2023-03-20):
  - "Since the meeting on 3/4 had ended early I'm heading to the gym." -> written 2023-03-04
  - "I'm heading to the gym, since the meeting on 3/4 had ended early." -> written 2023-03-04
  - "Honestly, since the meeting on 3/4 had ended early, I'm heading to the gym." -> written
    2023-03-04
  - "Since the meeting on 3/4, which had run long, I'm heading to the gym." -> written
    2023-03-04 (the first comma is inside the since-clause, so the "main clause" starts at
    "which had run long")
  - Control with the comma right after the since-clause: "Since the workshop had ended early,
    I'm heading to the gym." -> causal_since (the new test).
- Impact: a wrong `valid_at` on a causal since. Same output as a43c75e. The CHANGELOG states the
  perfect is "read outside a leading since-clause", which holds only for the comma-terminated
  case the test covers.
- Fix: bound the since-clause by its own syntax rather than the first comma. For example, end it
  at the first finite main-clause subject after the expression
  (`\b(?:i|we|you|he|she|they|it)(?:'m|'ve|'re|'s|'d)?\s` after the expression). For a
  mid-sentence since, search only `low[s0:at]` (the clause before "since"). Add tests for the
  no-comma and mid-sentence causal forms.

### N3 (Medium) - The M2 fix only checks coordinate clauses of the same sentence; borrowed times with a start word still write

- Location: `src/menhir/infrastructure/anchored_time.py:770-777` (`_fact_elsewhere`), 789-793,
  722-724 (`_BACK_REF_START`)
- Mechanism: `_fact_elsewhere` returns True only if the fact's content words occur in another
  `_COORD` segment of the expression's sentence and in none of the expression's segment. It
  returns False, so the time is written, when:
  - (a) the fact is in a different sentence
  - (b) the fact shares any content word with the start clause
  - (c) the fact is paraphrased
  - (d) the fact has no content words
  Separately, `_BACK_REF_START` skips the check for any "it started" / "this began", including
  the weather/expletive "it".
- Reproduced (item 0 point_event and item 1 state share the expression; Graphiti = speech
  date 2023-06-15):
  - (a) "I started a new job last week. My back is sore." fact 1 "The user's back is sore." ->
    item 1 written 2023-06-08
  - (b) "I started a new medication for my back last week and my back feels better." fact
    "The user's back feels better." -> written 2023-06-08
  - (c) "I started a new job last week and my back is killing me." fact "The user experiences
    lumbar discomfort." -> written 2023-06-08
  - (d) "I started fixing the fence three weeks ago and all is ok." fact "All is ok." -> written
    2023-05-25
  - expletive: "It started raining yesterday, and my sister is visiting." fact "The user's
    sister is visiting." -> written 2023-06-14
  - Control: "I started a new job last week and my back is sore." -> state_start.
- Impact: a borrowed time is written on a state, the PR's target defect. (a) is the
  cross-sentence shape the branch is named for, and the R29.2 test covers it only when the
  sentence has no start word. All outputs are the same as a43c75e.
- Fix: when the start word is in the expression's clause, also require the fact's content words
  to overlap the start word's clause (the positive form of the test), not only "not elsewhere".
  Treat an empty `own` as "not the same event" for a state. Exclude expletive "it" before
  weather verbs, or limit `_BACK_REF_START` to "it all", "that", "which", and "this".

### N4 (Medium) - Every-occurrence loop is quadratic; 8.9 s synchronous CPU per episode within production caps

- Location: `src/menhir/infrastructure/anchored_time.py:819-823` (loop), 780-794 (each call
  runs `_sentence` over the whole turn, up to 3 times, plus `_clause_start_and_text`);
  caller `src/menhir/infrastructure/graphiti_extraction_policy.py:1708` inside
  `async def _apply_anchored_time` (1637); caps `ANCHORED_TIME_MAX_TURN_CHARS = 4000`,
  `ANCHORED_TIME_MAX_EDGES = 40` (1633-1634)
- Mechanism: for each item, every occurrence of the expression in the turn is judged.
  `_sentence` does a `finditer` over the whole turn on every call, so cost is
  O(items x occurrences x turn length). a43c75e judged the first occurrence only.
- Reproduced: turn = `("2023-05-01 ok. " * 400)[:4000]` (267 occurrences of "2023"), 40 facts,
  40 state items with expression "2023": f8bf270 8.90 s, a43c75e 0.07 s. Expression "5": 8.93 s
  vs 0.07 s. Unbounded by the caps: a 20000-char turn with 769 occurrences takes 2.56 s for
  one item.
- Impact: the guard runs synchronously on the event loop, so a pasted log or table with a
  repeated date stalls every other coroutine in the process for seconds per episode. The result
  is still correct.
- Fix: compute sentence ends once per turn (pass a precomputed list to `_sentence`), and cap the
  occurrences judged (for example the first 8), failing closed (`state_start`) if the cap is
  hit. Matching only on word boundaries (N9) also cuts the occurrence count.

### N5 (Medium by the brief's rule; rare input) - U+0130 in the expression is no longer found, so a causal since is written (regression)

- Location: `src/menhir/infrastructure/anchored_time.py:816` (`expr = _fold(_norm(...))`),
  659-660 (`_norm` calls `.lower()` first), 732-734 (`_fold` keeps characters whose lower form
  has length != 1)
- Mechanism: `_fold(turn)` keeps "\u0130" unchanged. The expression goes through `_norm` first,
  whose `str.lower()` turns "\u0130" into "i\u0307" (two characters), so `low.find(expr)` fails
  and the item is "left alone".
- Reproduced: "Since I moved to \u0130zmir on 3/4, I'm hoping to learn Turkish." with item 1
  event_anchored state expr "Since I moved to \u0130zmir" (anchor fact 0 "on 3/4"): f8bf270
  marked [] -> item 1 `written 2023-03-04`. a43c75e: marked [1], causal_since. Control with
  ASCII "Izmir" on f8bf270: causal_since.
- Impact: a wrong `valid_at` on a causal since whose expression contains Turkish dotted
  capital I. Rare.
- Fix: fold the raw expression the same way as the turn: `expr = _fold((it.get("expression") or
  "").strip().strip('"'))`. Do not call `_norm` before `_fold`.

### N6 (Low) - A since-clause without a comma, followed by a later comma, has its perfect cut away; a correct temporal since is dropped (regression)

- Location: `src/menhir/infrastructure/anchored_time.py:763-766`
- Mechanism: `sent.find(",", at - s0)` takes the first comma after "since", wherever it is. If
  the since-clause has no comma, that comma is inside or after the main clause, and the
  perfect before it is discarded.
- Reproduced:
  - "Since I moved to Denver in May I've tried every taco place, and I love it." (item 0
    explicit_date "Since I moved to Denver in May"; item 1 event_anchored state, anchor 0):
    f8bf270 item 1 `guard causal_since`; a43c75e item 1 written 2023-05-16. Without the
    trailing ", and I love it" f8bf270 also writes.
  - "Since the meeting on 3/4 I have been running daily, so I'm fitter." -> causal_since on
    f8bf270, written 2023-03-04 on a43c75e.
- Impact: a correct start is erased. Because the basis becomes `none`, any dependent anchored on
  this item becomes unplaceable. Not seen in the 22 runs.
- Fix: same as N2 (bound the since-clause syntactically). At minimum, search for the perfect in
  both halves when the comma is not right after the since-clause.

### N7 (Low) - Narrowed `_RUNS_TO_NOW` no longer exempts common running durations (regression)

- Location: `src/menhir/infrastructure/anchored_time.py:716-720`; `CUES["duration"]` 43-45
  and `_NUM` 24 do not cover these forms either
- Reproduced: state item, speech_relative with an offset, turn "I've been doing physio <expr>.",
  speech 2023-06-15. Each of the following is state_start on f8bf270 and written on a43c75e:
  "for the past couple of weeks" (a43c75e 2023-06-01), "for the last couple of months"
  (2023-04-15), "for two and a half years" (2021-06-15), "for 2.5 years" (2021-06-15),
  "for half a year" (2022-12-15), "for most of the year", "for the better part of a year",
  "for a good few months", "for quite a while", "for so long".
  Still exempt on f8bf270: "for 9 years", "about 4 years now", "for a while", "for the past few
  months", "for a year and a half", "for over a decade", "for the past 18 months", "for about a
  month", "for around six weeks", "for weeks", "for a couple of years", "for a few weeks now".
- Impact: correct state starts lost. "quite a while" and "so long" are usually basis `vague` and
  never reach the guard. "couple of weeks/months", "two and a half years", and "2.5 years" are
  typically speech_relative with an offset, so those are real losses. Not seen in the 22 runs.
- Fix: allow an optional `(?:a\s+)?couple\s+of` and `\d+(?:\.\d+)?` in the number slot, allow
  `\w+\s+and\s+a\s+half` and `half\s+a`, and allow `quite\s+a|so|most\s+of\s+the` as qualifiers.
  Alternatively keep `^\s*for\b` and exclude only a following capitalized or holiday noun.

### N8 (Low) - Narrowed `_PERFECT` misses irregular participles, "'s"/"'d" + participle, and two-adverb perfects; temporal since dropped (regression for some)

- Location: `src/menhir/infrastructure/anchored_time.py:708-714`
- Reproduced (item 1 = event_anchored state "Since the meeting on 3/4", anchor fact 0):
  - "Since the meeting on 3/4, I've driven to work every day." -> f8bf270 causal_since, a43c75e
    written 2023-03-04
  - "..., I've eaten lunch at my desk." -> same split
  - "..., I have hardly ever missed a day." -> same split (one adverb slot only)
  - "..., she's moved to the night shift." -> causal_since on both (pre-existing: "'s" accepted
    only before "been")
- Impact: a correct temporal since is erased and its dependents become unplaceable. Missing
  participles include eaten, driven, ridden, fallen, broken, spoken, forgotten, chosen, worn,
  drawn, shown, thrown, come, read, fed, and fought.
- Fix: accept `'ve`/`have` + any token when followed by a typical participle suffix (-en, -wn,
  -ne, -ung, -unk, -ought) or an irregular list. Allow up to two adverbs. Accept `'s|'d` +
  participle when the participle is -ed or from the irregular list.

### N9 (Low) - "Strictest occurrence wins" uses substring matches and unrelated repeats; correct writes dropped (regression)

- Location: `src/menhir/infrastructure/anchored_time.py:819-823` (`low.find` has no word
  boundary; `max(..., key=rank.get)`)
- Reproduced (f8bf270 vs a43c75e):
  - "I've lived here since 2/15. My rent is due on 12/15." expr "2/15" (state): state_start vs
    written 2023-02-15 ("2/15" found inside "12/15")
  - "I've been sleeping badly since last week, and last weekend I went camping." expr "last week":
    state_start vs written 2023-06-08 (found inside "last weekend")
  - "I've lived in Denver since 2019. In 2019 my dad retired." expr "2019": state_start vs
    written 2019-07-02
  - "I've been vegan since March. Since March was so busy, I'm taking April off." expr "since
    March" (explicit_date state): guard causal_since (basis none) vs written 2023-03-16
- Impact: correct writes lost, and in the causal case dependents are lost too. Fail-safe
  direction, not seen in the 22 runs.
- Fix: find occurrences with
  `re.finditer(rf"(?<![a-z0-9/]){re.escape(expr)}(?![a-z0-9/])", low)`. When occurrences
  disagree, prefer the occurrence whose sentence shares content words with `facts[i]` (round-1
  L3 option 2), and use "strictest" only as the tiebreak.

### N10 (Low) - `_COORD` splits coordinated objects and verbs on " and ", so "started X and Y <time>" loses its write (regression)

- Location: `src/menhir/infrastructure/anchored_time.py:726`, 770-777
- Reproduced (item 0 point_event, item 1 state, "last month", speech 2023-06-15):
  - "I started yoga and meditation last month." fact 1 "The user practices yoga." ->
    f8bf270 state_start; a43c75e written 2023-05-16
  - "I started lifting weights and running last month." fact 1 "The user lifts weights." ->
    same split
  - Pre-existing on both: "Three weeks ago, I started physical therapy." fact "The user is doing
    physical therapy." -> state_start, because the comma clause "Three weeks ago," has no start
    word.
- Impact: correct state starts lost for coordinated objects or verbs. Not seen in the 22 runs.
- Fix: treat " and " as a clause boundary only when a subject pronoun or determiner plus verb
  follows (`\sand\s+(?:i|we|my|the|he|she|they|it)\b`), the same approximation `_SINCE_CLAUSE`
  uses.

### N11 (Medium by the brief's rule; not a regression) - M1 fix is partial for render: a state_start row still renders Graphiti's date inside the borrowed window, attributed to the borrowed expression

- Location: `src/menhir/infrastructure/anchored_time.py:1033-1036`;
  `src/menhir/domain/event_time.py:69-71`
- Mechanism: the outcome is now `state_start`, so the window is not rendered. `render` still
  prints `recorded` with `(from '<expr>')` whenever `recorded != speech_date`. If Graphiti itself
  assigned the borrowed date to the state, that date is shown with the borrowed expression as its
  source.
- Reproduced: item `{"expression": "three weeks ago", "kind": "state", "guard": "state_start"}`,
  Graphiti `valid_at` 2023-05-25, speech 2023-06-15 -> `plan_overlay` reason `state_start`,
  persisted window 2023-05-22..2023-05-28, `rendered_event_time` ->
  `2023-05-25 (from 'three weeks ago')`. With Graphiti's speech-date default instead: `event
  time unknown (said 2023-06-15; 'three weeks ago')`.
- Frozen data: G19.2 (Graphiti 2023-05-01, speech 2023-05-22) now renders `2023-05-01 (from
  'every morning since then')`, which is within its gold window. R20.1 renders 2023-05-24
  (gold). K08.2 and X09.6 (Graphiti = speech date) render "event time unknown".
- Impact: the CHANGELOG says "the borrowed window is never rendered". The window is not
  rendered, but a borrowed date inside it still can be. Graphiti's own value is never
  overridden by design, so the date itself is pre-existing; what is new is the outcome label.
- Fix: in `EventTime.render`, skip the `(from '<expr>')` suffix, or the resolved-date line,
  when `outcome == "state_start"`. That needs no new persisted field, since `time_outcome` already
  carries it.

### N12 (Low) - Test gap: "strictest occurrence wins" is not pinned

- Location: `tests/test_anchored_time_state_start.py:273-282`
- Mutation on a private copy of the tree: replacing `reason = max(reason, found, key=rank.get)`
  with `reason = found` (last occurrence wins) leaves all 29 tests in the file passing. The test
  puts the stricter occurrence last.
- Fix: add a case whose stricter occurrence comes first, for example the two sentences
  swapped.

### N13 (Low) - Expression after a newline: `_main_clause` passes a negative start to `str.find`

- Location: `src/menhir/infrastructure/anchored_time.py:761-766`
- Mechanism: `_sentence` splits on "\n", but the "since" lookbehind (`since\s*$`, 743) crosses
  it, so `at < s0` and `at - s0` is negative. `str.find(",", -k)` searches only the last k
  characters of the sentence.
- Reproduced: `_main_clause("Since\nFebruary 20th, I'm hoping, ok", <folded>, at=0, pos=6,
  n=13)` returns `' ok'` (the text after the last comma) instead of the text after the first
  comma.
- Impact: the wrong slice is searched for a perfect, in either direction. Rare input (a line
  break between "since" and the date).
- Fix: clamp: `comma = sent.find(",", max(0, at - s0))`. Also stop the lookbehind at "\n".

### Info

- I-a: `_since_operator` "ever" lookback (751) uses `turn_low[pos-8:pos]` with `\bever\s+$`.
  A slice that starts inside a word ending in "ever" passes the `\b` test at the slice start.
  `_since_operator("do whatever    since the race", 15, "since the race", "event_anchored")` ->
  `('ever since', 7)`. This needs four whitespace characters after "whatever", so it is
  pathological. The same slicing pattern pre-exists in the 12-character lookbehind (743). Fix:
  search `turn_low[:pos]` with `(?<![a-z])ever\s+$`, or check the character before the slice.
- I-b: `_RUNS_TO_NOW` still exempts any `\bnow\b` except "from now": "right now" and "for now"
  are exempt (graphiti_inside_window, window rendered), while "from now on" is now state_start
  ("event time unknown"). None of these writes. The inconsistency is only in render and outcome.
- I-c: `kept_inside_window` consumers: grep for `kept_inside_window|kept_inside|
  graphiti_inside_window` over the clone (excluding .venv) found only telemetry
  (graphiti_extraction_policy.py:1718,1723-1725), the eval script (scripts/eval_anchored_time.py:
  117,179,203,206), event_time.py:15 (render), and tests. No logic consumer depends on the
  count. `contract_rows` is called once (anchored_time_persist.py:177) with
  `speech_date=_utc_date(reference_time)` (graphiti_client.py:633), never None. So moving
  `state_start` above `graphiti_resolved` does not change any render: event_time.py:69-70 renders
  `recorded` for both outcomes when `recorded != speech_date`.
- I-d: plan_overlay reorder safety: `kind == "plan"` (1027) is still checked before `state_start`,
  and the guard marks only `kind == "state"` with state_start. Everything after `state_start`
  (`graphiti_inside_window`, `graphiti_resolved`, ambiguity, would_invert) is a no-write, so the
  reorder changes no write. `time_ambiguity` is still persisted from `result.ambiguity`
  independently of the reason.
- I-e: `_fold` length preservation holds by construction (one output character per input
  character). `low` (folded) and `turn` (original) are used consistently: positions from `low`
  index `turn` in `_sentence`, `_sentence_initial`, and `_clause_start_and_text`. The only
  inconsistency is the expression side (N5). `_fold` does not fold U+2018 or U+02BC
  apostrophes.
- I-f: `_fact_elsewhere` cut arithmetic is correct. `_COORD.finditer(low, s0, e0)` returns absolute
  `m.end()`. `s0` and `e0` are always candidates, so `max` and `min` are never empty. A cut strictly
  inside the expression (for example "between May and June") is ignored by both filters.
  Termination of the occurrence loop: `pos` strictly increases (`find(expr, pos + 1)`), so it ends.
  Overlapping matches are visited.
- Round-1 items not claimed by f8bf270 and still open: L1, L2, L7 (reproduced above), I1-I6
  unchanged.
- CHANGELOG and diff: no non-ASCII characters in added lines (Python scan of the
  `git diff a43c75e..f8bf270` "+" lines found none). The PINNED_O2 changes (heldout2:frozen kept
  13 -> 12, heldout3:frozen 16 -> 15) match the replay relabels of G19.2 and K08.2.

## Test quality (new tests in f8bf270)

Mutation run on a private `git archive` copy of f8bf270 (scratch r2rev/mut; the clone was not
modified), running `tests/test_anchored_time_state_start.py`:

| Mutant | Result | Killing test |
|---|---|---|
| state_start check moved back below graphiti_inside_window | KILLED | test_overlay_never_shows_a_state_start_window_even_when_graphiti_agrees |
| restore a43c75e `_PERFECT` (any have/has/had) | KILLED | test_have_without_a_participle_is_not_perfect (both params) |
| `_main_clause` never strips the since-clause | KILLED | test_a_perfect_inside_the_leading_since_clause_is_not_the_main_clause |
| no "ever" lookback | KILLED | test_ever_before_an_expression_that_starts_at_since |
| no `_fact_elsewhere` | KILLED | test_a_start_word_about_another_clause_event_does_not_date_the_state |
| no `_BACK_REF_START` exemption | KILLED | test_a_start_that_refers_back_still_dates_the_state |
| `_COORD` without "and" | KILLED | test_a_start_word_about_another_clause_event_does_not_date_the_state |
| first occurrence only | KILLED | test_a_repeated_expression_is_judged_at_every_occurrence |
| last occurrence wins (no max) | SURVIVED | none (N12) |
| `low = turn.lower()` (no fold) | KILLED | curly-apostrophe test, curly perfect param |
| fold without apostrophe mapping | KILLED | same |
| no `_FROM_NOW` | KILLED | test_for_and_now_... [a week from now] |
| `_RUNS_TO_NOW` back to `\bnow\b|^\s*for\b` | KILLED | test_for_and_now_... [for Christmas] |
| bounds check removed | KILLED | test_items_outside_the_turn_or_undated_are_left_alone (IndexError at `facts[i]`; with 8 facts the item would be marked, so it is not vacuous) |

No vacuous assertion found apart from N12. Coverage gaps: no test for "'ve got" (N1), the
no-comma or mid-sentence causal since (N2, N6), a start word with the fact in another sentence
(N3a), the coordinated "and" (N10), substring occurrences (N9), the restored durations (N7), or
a U+0130 expression (N5).

## Open questions

- OQ1: frequency of N1 ("'ve got") and N3a (start word plus fact in the next sentence) in live
  traffic. Neither occurs as a flipped write in the 22 runs. Across the 172 distinct replay
  turns, 36 contain "since", 4 contain "'ve got/have got/has got", and 1 (X06) contains both,
  with an undated anchor. To settle it, run the replay with a mutant that removes `got` and
  sample live turns.
- OQ2: whether N4's stall matters in deployment depends on how many episodes are processed
  concurrently on one event loop. I measured CPU time, not end-to-end latency.

## Coverage table

Scope enumerated with `git diff --stat a43c75e..f8bf270`: 4 files. Current line counts
(`wc -l`): 1068 + 360 + 137 + 99 = 1664.

| File | Lines | Status |
|---|---|---|
| src/menhir/infrastructure/anchored_time.py | 1068 | read-deep: 593-831 (guards, all new code), 835-1068 (overlay and dataclasses); 25-48 (CUES); rest unchanged since round 1 (read there) |
| tests/test_anchored_time_state_start.py | 360 | read-deep 1-320 (all new tests); 321-360 production hook tests unchanged since round 1 |
| tests/test_anchored_time_replay.py | 137 | diff hunk read; pinned values reproduced by the passing test and by replay relabels |
| CHANGELOG.md | 99 | diff hunk read; ASCII check by script |

Rows: 4 = 4 enumerated files.

Out-of-scope files read for context: src/menhir/domain/event_time.py 1-120,
src/menhir/infrastructure/anchored_time_persist.py 110-180,
src/menhir/infrastructure/graphiti_extraction_policy.py 1630-1730,
src/menhir/infrastructure/graphiti_client.py 615-640, ../xs_replay.py (all).

## Verification notes

- Every reproduction ran through the clone's `.venv` Python with `-I`, importing `menhir` from the
  tree under test. HEAD used the clone's src. a43c75e and db990a1 used `git archive` extracts
  in the scratch directory r2rev/. Production order was used in each run: clause_guard, then
  state_start_guard, then plan_overlay, with Graphiti baseline = speech date unless stated.
- Mutation testing wrote only to a private extract (r2rev/mut), never to the clone.
- Not checked: live Neo4j persistence and end-to-end recall rendering. Render was checked by
  feeding `contract_rows` output into `rendered_event_time`. The N4 measurement is CPU time on
  a local machine, not production hardware.

## Review confidence: 82/100

Every claimed fix was re-run on its round-1 input against both commits. Every new finding has a
reproduced input -> output pair on f8bf270, with the a43c75e output alongside, so regression vs
residual is stated. The 22-run replay was diffed per fact. Confidence is held back because
severity for N1 and N3 depends on live frequency (OQ1), and the N4 impact depends on deployment
concurrency (OQ2).


---

<!-- combined source: menhir-pr274-round3-review.md (prefix r3) -->

# menhir PR #274 round 3 review - review-fix commit 2d5a311

- Branch: fix/anchored-time-cross-sentence. HEAD 2d5a311, parent f8bf270, base origin/main db990a1
- Clone: a local scratch clone (not modified)
- Round 2: .agent/reviews/menhir-pr274-round2-review.md
- Reviewer: Claude (read-only lane; nothing pushed, committed, or commented; no paid calls; no containers)
- Date: 2026-10-12
- Work dir: r3rev (git-archive extracts `f8bf270/`, `d5/` = 2d5a311, `mut/` for mutation; probe
  scripts `c_r3.py`, `c_r3b.py`, `c_r3c.py`, `c_cost3.py`, `sent_eq.py`, `corpus_shapes.py`, `mutate3.py`)

## Executive summary

2d5a311 fixes what it claims for the round-2 repro inputs. Every round-2 case was re-run on f8bf270 and
2d5a311. N1, N2, N3 (a, c, d, expletive), N4 (period-separated turns), N5, N7 (all but one form), N8
(driven, eaten, two adverbs), N9 (substring cases), N10, N11, N13, I-a and C4 all change as claimed.
The 22-run replay is unchanged on writes: 2263 ok, 140 writes, 0 wrong writes, 0 harmful, the same as
f8bf270. The only per-fact changes are relabels on b11X X06 (open_or_unplaceable -> guard causal_since).
`_sentence` gives identical results to the old implementation at every position of 291 distinct eval
turns (80,248 positions, 0 mismatches). Focused tests: 119 passed, 1 failed (the known heldout3:frozen
parity).

The commit also introduces regressions that write wrong dates:

- **R3-1 (High).** `_COORD` no longer cuts at "and/but/so" before a non-subject word, so the M2 shape
  writes again. Examples: "I started a new job last week and now my back is sore", "... and John is
  visiting", "... so Mom is helping", "... and then ...", "... but honestly ...". The fronted form
  ("Last week, I started a new job and now my back is sore") writes too. f8bf270 marked all eight
  reproduced shapes `state_start`. 2d5a311 writes the borrowed date on all eight.
- **R3-2 (Medium).** The broadened `_RUNS_TO_NOW` treats any "for ... <unit>" phrase as running up to
  now. Forward and bounded phrases are now written: "for the next two weeks" -> 2023-06-29, "for next
  week" -> 2023-06-22, "for the coming weeks" -> 2023-06-22. These are future `valid_at` values on a
  state. f8bf270 marked them `state_start`.
- **R3-3 (Medium).** The N1 fix is partial. An -ed adjective still counts as a participle when the next
  word is in `_AFTER_VERB`. That covers `[a-z]+ly` words like "family" and "weekly", punctuation, "and",
  and "up". So "I have limited family time", "I have tired, sore legs", "I have mixed and confusing
  feelings", "I have assigned weekly chores" and "I have mixed up feelings" still make a causal "since"
  temporal. Two irregulars added in this commit are common adjectives, so "I have broken ribs" and "I have
  read-only access" now write where f8bf270 dropped them.

There are also fail-safe regressions: no write, but a correct write and its dependents are lost.

- **R3-5.** An -ed participle followed by a bare noun, gerund, name or number is no longer a perfect.
  Examples: "I've practiced yoga since March.", "I've played tennis since 2019.", "Since the meeting on
  3/4, I've started running every day.". The temporal "since" becomes `causal_since`, which drops the
  basis. This is likely the most frequent loss in live traffic. It does not occur in the 22 runs.
- **R3-6.** A leading since-clause without a comma ("Since March I've been vegan.") is now always
  `causal_since`. The new `_main_clause` docstring accepts this.
- **R3-7.** "which started hurting last week" is no longer a back-reference.

N4 is only half fixed (R3-4). A 4000-character turn that is one long sentence still takes 3.6-9.6 s in the
guard. The every-occurrence loop is no longer pinned by any test: a "first occurrence only" mutant
survives (R3-8).

Counts: High 1, Medium 3, Low 6, Info/residual 7.

## Test run

```
MENHIR_ANCHORED_TIME_EVAL_DIR=<eval-dir> \
  .venv/Scripts/python.exe -m pytest tests/test_anchored_time_state_start.py tests/test_anchored_time_replay.py \
  tests/test_anchored_time_guard.py tests/test_anchored_time_render.py -q -p no:cacheprovider
FAILED tests/test_anchored_time_replay.py::test_heldout_replay_matches_the_prototype[heldout3:frozen]
1 failed, 119 passed, 2 warnings in 2.04s
```

Offline replay (`python -I -B ../xs_replay.py <tree> <out.json>` on private git-archive extracts):

```
2d5a311  runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0
f8bf270  runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0   (byte-identical to round-2 f8.json)
```

Per-fact diff f8bf270 -> 2d5a311 (written, value, reason, guard), from `r2rev/cmp.py`:

```
b11X:g1a X06.0 (False, None, 'open_or_unplaceable', None) -> (False, None, 'guard', 'causal_since')
b11X:g1a X06.2 (False, None, 'open_or_unplaceable', None) -> (False, None, 'guard', 'causal_since')
b11X:g1b X06.0 (False, None, 'open_or_unplaceable', None) -> (False, None, 'guard', 'causal_since')
```

No `written` or `value` change, so no correct write in the 22 runs is lost (priority 3: none). X06 is
"Since I rearranged the furniture, I've got a nice little reading nook". It is now causal because
"'ve got" is no longer a perfect, which is the N1 fix. Its anchor is undated, so nothing was written
before either.

## `_sentence` change (priority 2)

- Location: `src/menhir/infrastructure/anchored_time.py:632-640`.
- Exhaustive equivalence: `r3rev/sent_eq.py` loads every `*turns*.json`, `*gold*.json` and `dev_set*.json`
  in the eval dir (17 files) plus 10 synthetic edge strings (empty, "...", "Dr. Smith. Mr. X...", U+0130,
  a trailing "\n"). It compares the new `_sentence` with a verbatim copy of the old list-scan version at
  every position from -2 to len+2. Output: `files 17 distinct turns 291 total chars 78793`,
  `positions checked 80248 mismatches 0`. The 179 distinct replay turns are a subset of these sets
  (`corpus_shapes.py` prints `distinct replay turns 179`).
- Why it is equivalent: the pattern cannot match empty (`[.!?]+` or one of `[;\n]`), and `finditer`
  matches do not overlap. So the `m.end()` values are strictly ascending, and `bisect_right(ends, pos)`
  returns the count of ends <= pos. That gives `ends[k-1]` = max(e <= pos) and `ends[k]` = min(e > pos),
  with the same defaults.
- lru_cache: the key is the turn `str` (immutable, hashable). The cached value is a `tuple` of ints, and
  no caller mutates it (the only reader is `_sentence`, which indexes it). `maxsize=16`, so at most 16
  turns are retained. Production turns are capped at 4000 chars (`ANCHORED_TIME_MAX_TURN_CHARS`,
  graphiti_extraction_policy.py, per round 2), so the cache holds at most about 64 KB plus tuples.
  `functools.lru_cache` is safe to call from several threads. The worst case under concurrency is a
  duplicate compute, with no corruption. The eval run's `cache_info()` was hits=79957 misses=291
  currsize=16. Safe.
- `_sentence` is shared with `clause_guard` (lines 684, 688, 645, 660). Identical output means
  `clause_guard` behaviour is unchanged. The replay agrees: there were no `clause` guard changes.

## Round-2 fix verification (priority 1, regressions vs f8bf270)

Every round-2 harness file (`c_new.py`, `c_new2.py`, `c_new3.py`, `c_got.py`, `c_l12.py`, `c_round1.py`)
was run on both extracts and diffed line by line.

| Round 2 | Input (abridged) | f8bf270 | 2d5a311 | Status |
|---|---|---|---|---|
| N1 | "Since I started at the clinic on 2/15, I've got a long commute every day." | written 2023-02-15 | causal_since | fixed |
| N1 | "... I have mixed feelings / limited options / tired eyes / Fred helping me" | written 2023-03-04 | causal_since | fixed for these inputs (gaps: R3-3) |
| N2 | C2 no comma, C3 mid-sentence, C4 relative clause, C5 "Honestly," | written 2023-03-04 | causal_since | fixed |
| N3a | "I started a new job last week. My back is sore." | written 2023-06-08 | state_start | fixed |
| N3b | "I started a new medication for my back last week and my back feels better." | written | written | open (residual RS-1) |
| N3c | "... and my back is killing me." / fact "lumbar discomfort" | written | state_start | fixed |
| N3d | "... fence three weeks ago and all is ok." | written | state_start | fixed |
| N3 expl. | "It started raining yesterday, and my sister is visiting." | written 2023-06-14 | state_start | fixed |
| N4 | 40 items, expr "2023", period-separated 4000-char turn | 8.83 s | 0.12 s | fixed for this shape (gap: R3-4) |
| N5 | "Since I moved to \u0130zmir on 3/4, I'm hoping ..." | written 2023-03-04 | causal_since | fixed |
| N6 | C1 "Since I moved to Denver in May I've tried every taco place, and I love it." | causal_since | causal_since | not claimed, still open |
| N6 | C1b same without ", and I love it" | written 2023-05-16 | causal_since | **new regression** (R3-6) |
| N7 | couple of weeks/months, two and a half, 2.5, half a year, quite a while, most of the year, a good few months, so long | state_start | written | fixed |
| N7 | "for the better part of a year" | state_start | state_start | open (Low) |
| N8 | "'ve driven", "'ve eaten", "have hardly ever missed" | causal_since | written 2023-03-04 | fixed |
| N8 | "she's moved to the night shift" | causal_since | causal_since | not claimed, open |
| N9 | "2/15" in "12/15"; "last week" in "last weekend" | state_start | written | fixed |
| N9 | "I've lived in Denver since 2019. In 2019 my dad retired." | state_start | state_start | open (unrelated repeat) |
| N9 | "I've been vegan since March. Since March was so busy, ..." | causal_since | causal_since | open |
| N10 | "I started yoga and meditation last month." | state_start | written 2023-05-16 | fixed (cost: R3-1) |
| N10/F4 | "Three weeks ago, I started physical therapy." | state_start | written 2023-05-25 | fixed (fronted-time rule) |
| N11 | state_start, Graphiti 2023-05-25 in window 05-22..05-28 | "2023-05-25 (from 'three weeks ago')" | "event time unknown (said 2023-06-15)" | fixed |
| N13 | `_main_clause("Since\nFebruary 20th, I'm hoping, ok", ...)` | `' ok'` | `"  i'm hoping, ok"` | fixed |
| I-a | `_since_operator("do whatever    since the race", 15, ...)` | `('ever since', 7)` | `('since', 15)` | fixed |
| round 1 | M1-M4, L3-L6 repros (`c_round1.py`) | | | identical output on both |

## Findings

### R3-1 (High) - Narrowed `_COORD` stops cutting at "and/but/so" before a non-subject word; the M2 borrowed-time write is back for common connectives

- Location: `src/menhir/infrastructure/anchored_time.py:736-738` (`_COORD` lookahead allows only
  `i|we|you|he|she|they|it|its|my|our|your|his|her|their|the|this|that|there`), used by `_segment`
  785-794 and the positive-evidence test at 825-827.
- Mechanism: `about_fact` is `_content(fact) & _content(low[a:b])`, where `[a,b)` is the expression's
  `_COORD` segment. When "and/but/so" is followed by "now", "then", "also", "honestly", or a name ("John",
  "Mom"), there is no cut. The segment then runs through the next clause and contains the fact's own
  words, so `about_fact` is non-empty. The start word ("started") is found in `c_text`, and the item is
  not marked. f8bf270 cut at every " and ", so `_fact_elsewhere` found the fact only in the other clause
  and returned `state_start`.
- Reproduced (item 0 point_event, item 1 state, same expression; Graphiti = speech date 2023-06-15;
  `r3rev/c_r3.py` section P). For item 1, f8bf270 gives `state_start` in every row and 2d5a311 gives
  the written date shown:

  | Input | Fact 1 | 2d5a311 item 1 |
  |---|---|---|
  | "I started a new job last week and now my back is sore." | "The user's back is sore." | written 2023-06-08 |
  | "I started a new job last week and John is visiting." | "John is visiting the user." | written 2023-06-08 |
  | "I started a new job last week so Mom is helping with the kids." | "The user's mom helps with the kids." | written 2023-06-08 |
  | "I started a new job last week and then my back got sore." | "The user's back is sore." | written 2023-06-08 |
  | "I started a new job last week but honestly my back is sore." | "The user's back is sore." | written 2023-06-08 |
  | "I started a new job last week and also my sister moved in." | "The user's sister lives with the user." | written 2023-06-08 |
  | "Last week, I started a new job and now my back is sore." | "The user's back is sore." | written 2023-06-08 |
  | "Three weeks ago, I started fixing the fence and now the goats are safe." | "The goats are safe." | written 2023-05-25 |

  Control: "I started a new job last week and my back is sore." gives `state_start` on both.
- Impact: a borrowed date is written as the state's `valid_at`. This is the PR's target defect (round-1
  M2), now for any "and now" / "and then" / name-subject coordination. Corpus check
  (`corpus_shapes.py`): 13 of 179 replay turns contain a start word plus "and/but/so" before a non-subject
  word. None of them produced a write change in the replay, because the items did not line up, so the
  live frequency is unknown (OQ1).
- Fix: keep the N10 fix but invert the test. Cut at " and/but/so " unless the next token makes a
  coordinated object or verb: a bare noun or gerund, followed by the expression or the end of the clause.
  Alternatively, add `now|then|also|still|honestly|[A-Z]\w+` (on the unfolded turn) to the lookahead. The
  smallest change is to extend the `_COORD` lookahead alternation with
  `now|then|also|so|still|even|luckily|honestly|everything|nothing` and to cut before a capitalized word
  in `turn`. Add the eight rows above as `state_start` tests.

### R3-2 (Medium) - Broadened `_RUNS_TO_NOW` exempts forward and bounded "for" phrases; a future date is written on a state

- Location: `src/menhir/infrastructure/anchored_time.py:727-729` (`^\s*for\s+(?:[\w.'-]+\s+){0,4}?(?:years?|
  months?|weeks?|days?|...)`), used at 818.
- Mechanism: up to four arbitrary tokens may sit between "for" and the unit, so "for the next two weeks",
  "for next week", "for the coming weeks", "for the first two weeks" and "for this week" all count as
  durations running up to now. The state check is skipped and `plan_overlay` writes the window midpoint.
  f8bf270 allowed only listed qualifiers ("the past|last", "about", ...), so these were `state_start`.
- Reproduced (state item, turn "I'm staying with my sister <expr>.", speech 2023-06-15; `c_r3.py` U,
  `c_r3b.py` Y):

  | Expression | Item | f8bf270 | 2d5a311 |
  |---|---|---|---|
  | "for the next two weeks" | offset +2 week after | state_start | written 2023-06-29 |
  | "for next week" | calendar next week | state_start | written 2023-06-22 |
  | "for the coming weeks" | calendar next week | state_start | written 2023-06-22 |
  | "for the rest of the year" | calendar this year | state_start | graphiti_inside_window (window rendered) |
  | "for the week" | calendar this week | state_start | graphiti_inside_window (window rendered) |
  | "for this week" | calendar this week | state_start | graphiti_inside_window (window rendered) |
  | "for the first two weeks" | calendar this month | state_start | graphiti_inside_window (window rendered) |

  Pre-existing on both trees: "for two more weeks" (offset after) is written 2023-06-29, and "for a few
  days next month" is written 2023-07-16.
- Impact: a state gets a `valid_at` in the future, the end of the period rather than its start, when the
  resolver labels a forward duration as `kind: state` with a dated basis. The frozen checkpoints contain
  no "for the next/for next/for the coming" expressions (grep of `expression` values containing "for " in
  `ckpt_*.jsonl`: all backward, e.g. "for the past five years", "for 9 years", "for a while now"), so
  frequency depends on resolver labelling (OQ2).
- Fix: exclude forward qualifiers. Add a negative lookahead after `for\s+`:
  `(?!(?:the\s+)?(?:next|coming|following|rest|first|upcoming)\b|this\b|next\b)`, or require the offset
  direction to be "before" before applying the exemption. Also never write a state `valid_at` after
  `speech_date`. That guard would close the pre-existing "for two more weeks" case as well.

### R3-3 (Medium) - The N1 fix is partial: -ed adjectives before an `_AFTER_VERB` word, and the newly added irregulars "broken"/"read", still make a causal since temporal

- Location: `src/menhir/infrastructure/anchored_time.py:714-725` (`_IRREGULAR`, `_AFTER_VERB` includes
  `[a-z]+ly`, `and`, `up`, punctuation lookahead `\s*[.,;:!?)]`)
- Mechanism: after "have", an -ed word is accepted as a participle when the next token matches
  `_AFTER_VERB`. `[a-z]+ly` matches nouns such as "family" and adjectives such as "weekly" and "daily". A
  comma after the adjective in an adjective list also matches. "and" and "up" are listed. Separately,
  "broken" and "read" were added to `_IRREGULAR`, and both are common pre-noun adjectives.
- Reproduced (item 1 = event_anchored state "Since the meeting on 3/4 went badly", anchor fact 0;
  turn "Since the meeting on 3/4 went badly, <main>."; `c_r3.py` T):

  | main | f8bf270 | 2d5a311 |
  |---|---|---|
  | "I have limited family time" | written 2023-03-04 | written 2023-03-04 |
  | "I have tired, sore legs" | written | written |
  | "I have mixed and confusing feelings" | written | written |
  | "I have assigned weekly chores" | written | written |
  | "I have mixed up feelings" | written | written |
  | "I have set hours at work" | written | written |
  | "I have broken ribs" | causal_since | **written 2023-03-04** (regression) |
  | "I have read-only access" | causal_since | **written 2023-03-04** (regression) |

  The control "I have mixed feelings about the job" gives causal_since on 2d5a311 (the test case).
- Impact: a causal "since" date is written on a state. The CHANGELOG says "not ... an -ed adjective
  before its noun". That holds only when the noun follows the adjective directly and is not on the list.
- Fix: in the -ed branch drop `[a-z]+ly` from the follower set (keep the explicit adverbs), and require the
  punctuation branch to be clause-final (`\s*(?:[.;:!?)]|$)`, not a comma). Remove "broken" from
  `_IRREGULAR`, or accept it only when a determiner or pronoun follows ("broken my"). "read" followed by
  "-" can be excluded with `(?!-)`.

### R3-4 (Medium; partial fix of N4) - The guard is still quadratic in sentence length; 3.6-9.6 s on a single 4000-char sentence

- Location: `src/menhir/infrastructure/anchored_time.py:810-828` (`_start_reason` per occurrence: `_main_clause`,
  `_clause_start_and_text` 643-656, `_segment` 785-794 run `finditer` and `_content` over the whole
  sentence), loop 856-858.
- Mechanism: 2d5a311 caches sentence ends, but each occurrence still re-scans and re-tokenizes its
  sentence. When the turn is one sentence (no `.!?;` or newline), the cost is
  O(items x occurrences x turn length), as before.
- Reproduced (`r3rev/c_cost3.py`, 40 state items, expr "2023", 4000-char turn):

  | Turn shape | Occurrences | 2d5a311 | f8bf270 |
  |---|---|---|---|
  | `"2023-05-01 ok. " * 400` (the new test's shape) | 267 | 0.12 s | 8.83 s |
  | `"2023-05-01 ok, " * 400` | 267 | 3.61 s | 9.71 s |
  | `"2023 and more " * 400` | 286 | 9.57 s | 9.67 s |
  | `"since 2023 ok " * 400` | 286 | 0.59 s | 16.07 s |

- Impact: synchronous CPU inside `async _apply_anchored_time`, as in round 2. A comma-separated paste
  or a run-on message stalls the event loop for seconds. Results stay correct. The new
  `test_many_repeated_occurrences_stay_cheap` covers only the period-separated shape.
- Fix: cap the occurrences judged per item (for example 8, failing closed to `state_start` past the
  cap), or memoize `_start_reason` inputs per (sentence, clause). Add the comma-separated shape to the
  timing test.

### R3-5 (Low by the brief's fail-safe rule; likely the most frequent loss) - -ed participle followed by a bare noun, gerund, name or number is no longer a perfect; temporal since becomes causal_since

- Location: `src/menhir/infrastructure/anchored_time.py:719-725` (the -ed branch needs an `_AFTER_VERB`
  follower or punctuation).
- Reproduced (`c_r3.py` R, R2). Each row is written on f8bf270 and `guard causal_since` (basis set to
  none) on 2d5a311:
  - event_anchored state "Since the meeting on 3/4", turn "Since the meeting on 3/4, <main>.":
    "I've started running every day", "I've learned Spanish", "I've gained weight", "I've visited Paris
    twice", "I've stopped drinking coffee", "I've worked nights", "I've changed jobs", "I've walked 5 miles
    daily", "I've tried yoga", "I've moved twice". f8bf270 wrote 2023-03-04 for each.
  - explicit_date state, mid-sentence since: "I've practiced yoga since March." (f8 2023-03-16), "I've
    played tennis since 2019." (f8 2019-07-02), "I've studied French since January." (f8 2023-01-16),
    "I've used Linux since 2019.", "I have coached soccer since March.".
  - Unaffected: "I've lived in Denver", "I've owned my house since March", "I've lived here since March"
    (the follower is on the list).
- Impact: the correct start is lost, and because `causal_since` sets `basis: none`, items anchored on it
  become unplaceable. "have + regular verb + bare object + since <date>" is a core shape of the temporal
  "since". Not seen in the 22 runs: `corpus_shapes.py` finds 2 turns with "since" plus have/'ve + -ed +
  word, and in both the follower is in `_AFTER_VERB`.
- Fix: accept `[a-z]{2,}ed` followed by any word unless "have" is possessive. Reject only when the -ed
  word is directly followed by a plural or mass noun that is not a determiner or pronoun and the -ed word
  is on a short adjective list (mixed, limited, tired, used, assigned, reserved, ...). In other words,
  invert from an allow-list of followers to a deny-list of adjectives. Add tests for "I've practiced yoga
  since March" and "I've started running".

### R3-6 (Low; regression vs f8bf270, accepted in the new docstring) - A leading since-clause without a comma swallows the main clause, so a temporal since is causal

- Location: `src/menhir/infrastructure/anchored_time.py:772-782` (`_main_clause`: without a later comma,
  `sent[:a]` is empty for a sentence-initial "since" and nothing after it is searched).
- Reproduced (`c_r3.py` S, `c_r3b.py` Z, `c_new.py` C1b). Each is written on f8bf270 and causal_since on
  2d5a311:
  - "Since March I've been vegan." (explicit_date state "March"): f8 written 2023-03-16
  - "Since 2019 I have lived in Denver.": f8 written 2019-07-02
  - "Since the meeting on 3/4 I've been running daily." (event_anchored): f8 written 2023-03-04
  - "Since my surgery on 3/4 I have been on crutches.": f8 written 2023-03-04
  - "Since I moved to Denver in May I've tried every taco place and I love it.": f8 written 2023-05-16
  - With the comma ("Since March, I've been vegan.") it is written on both.
- Impact: casual messages often omit this comma. The write and its dependents are lost.
  `corpus_shapes.py` finds 0 replay turns with this shape.
- Fix: when no comma follows, end the since-clause at the first finite subject + perfect
  (`\b(?:i|we|you|he|she|they)(?:'ve|'d|\s+have|\s+has|\s+had)\b` after the expression) and search from
  there. That subject-led perfect cannot sit inside a causal since-clause ("Since the meeting had ended
  early I'm ..." keeps "had" inside the clause because no subject pronoun precedes it).

### R3-7 (Low; regression) - The `_BACK_REF_START` lookahead also blocks relative and "it all" back-references followed by a gerund or "to"

- Location: `src/menhir/infrastructure/anchored_time.py:733-735` (`(?!\s+(?:to\b|[a-z]+ing\b))`)
- Mechanism: the lookahead was added to reject the dummy "it" ("it started raining"). It applies to
  "which", "that" and "it all" as well. The relative clause's `_COORD` segment (cut at the commas) does
  not contain the antecedent noun, so `about_fact` is empty too.
- Reproduced (state "last week", speech 2023-06-15; `c_r3b.py` X). Each is written 2023-06-08 on
  f8bf270 and `state_start` on 2d5a311:
  - "My knee, which started hurting last week, is still sore." / "The user's knee is sore."
  - "My back went out. It all started going downhill last week." / "The user's back hurts."
  - "My cough, which started to get worse last week, is bad." / "The user has a bad cough."
- Fix: apply the lookahead only to bare `it`. `which|that|this|it\s+all` should stay back-references
  regardless of what follows.

### R3-8 (Low; test quality) - The every-occurrence loop is no longer pinned; four new behaviours are unpinned

Mutation run on a private extract (`r3rev/mutate3.py`, `r3rev/mut`, clone untouched) against
`tests/test_anchored_time_state_start.py`. Baseline: 58 passed.

| Mutant | Result | Killing test |
|---|---|---|
| no fronted extension in `_segment` | KILLED | test_state_start_markers_keep_the_write |
| no `c_text = turn[a:b]` replacement | KILLED | same |
| `_COORD` back to f8bf270 | KILLED | same (yoga and meditation) |
| no relative-clause comma skip in `_main_clause` | KILLED | test_a_perfect_inside_the_since_clause_never_makes_it_temporal |
| no whole-word matching | KILLED | test_an_expression_inside_a_longer_word_is_not_an_occurrence |
| no substring fallback in `_occurrences` | **SURVIVED** | none |
| `_BACK_REF_START` without lookahead | KILLED | test_a_start_word_needs_to_be_about_the_fact |
| "ever" lookback without the `isalnum` check (I-a fix) | **SURVIVED** | none |
| no clamp `a = at - s0` (N13 fix) | **SURVIVED** | none |
| adverb slot `{0,1}` | KILLED | test_irregular_participles_and_two_adverbs_are_perfect |
| -ed accepted before anything | KILLED | test_possession_and_ed_adjectives_are_not_perfect |
| "got" back in `_IRREGULAR` | KILLED | same |
| positive-evidence check removed | KILLED | two tests |
| **first occurrence only** (`_occurrences(...)[:1]`) | **SURVIVED** | none |
| last occurrence wins | KILLED | test_the_strictest_occurrence_wins_whatever_its_position |
| `_RUNS_TO_NOW` narrowed again | KILLED | test_durations_running_to_now_keep_the_write |
| expression through `_norm` again (N5) | KILLED | test_a_dotted_capital_i_does_not_hide_the_expression |
| render without the `borrowed` check | KILLED | test_render_never_shows_the_borrowed_time |
| render state_start branch removed | KILLED | same |

- The first-occurrence mutant was killed in round 2 by `test_a_repeated_expression_is_judged_at_every_occurrence`
  (tests/test_anchored_time_state_start.py:282-291). Under the positive-evidence rule its first
  occurrence ("My knee started hurting two weeks ago.") already gives `state_start` for "The hens are
  laying again" (no shared word), so the second occurrence is never needed. The new
  `test_the_strictest_occurrence_wins_whatever_its_position` (399-409) puts the strict occurrence first.
  No test now requires a later occurrence to be judged, so the test named for it is vacuous for that
  behaviour.
- The I-a and N13 fixes have no test.
- Fix: add a case where only a later occurrence is strict, for example "My knee started hurting two
  weeks ago, and my knee is sore. I fixed the coop two weeks ago." with fact "The user's knee is sore",
  where the second occurrence has no start word. Also add one test each for "whatever    since" and a
  newline between "since" and the date.

### R3-9 (Low; test quality) - A new test pins a correct-write loss as desired behaviour

- Location: `tests/test_anchored_time_state_start.py:368` (param of
  `test_a_start_word_needs_to_be_about_the_fact`, 370-374)
- `("I started a new job last week.", "The user is employed.")` asserts `marked == [0] and not
  results[0].written`. The user has been employed since last week, so last week is the correct start. It
  is lost only because the fact paraphrases "job" as "employed". The test records a known false negative
  of the shared-word heuristic as required behaviour, so a future improvement (stemming or synonyms) would
  fail this test.
- Fix: drop this param, or move it to a test named for the known limitation.

### R3-10 (Low) - Render compares the UTC date with the borrowed window; an evening Graphiti date at the window edge renders as Graphiti's "own" date

- Location: `src/menhir/domain/event_time.py:67-74` (`recorded = _utc_date_text(valid_at)`, 54). By
  contrast, `plan_overlay` uses `_graphiti_dates` (anchored_time.py:939-947), which adds the
  speech-timezone date because "Graphiti stores UTC, so speech at 23:00-05:00 comes back dated the next
  day in UTC".
- Reproduced (`c_r3c.py`): row state_start, window 2023-06-05..2023-06-11, speech 2023-06-15,
  `valid_at` "2023-06-11T21:00:00-06:00" (stored as 2023-06-12T03:00Z) -> `2023-06-12`. Graphiti's
  borrowed Sunday is shown as a date of its own, one day off.
- Impact: only when Graphiti's copy falls on the window's last day in the evening, local time. Rare.
- Fix: widen `borrowed` by one day at each end, or persist the speech tz date. The smallest change is
  `window_start - 1 day <= recorded <= window_end + 1 day` for `state_start`.

## Residuals (not regressions; same output on f8bf270 and 2d5a311)

- RS-1 (Medium by the brief's rule, N3b). Positive evidence is satisfied by any shared content word,
  including generic ones. "I started a new job last week, and I bought a new car." with fact "The user has
  a new car." -> item 1 written 2023-06-08, because "new" is shared. "I started a new medication for my
  back last week and my back feels better." -> written 2023-06-08.
  `_STOP` (anchored_time.py:598-603) does not include "new", "get", "got", "back", "good", "more" or
  "feel".
- RS-2 (Medium by the brief's rule). A perfect anywhere outside the since-clause makes a causal since
  temporal, including parentheticals and trailing relative clauses. On both trees:
  - "Since the meeting on 3/4 went badly, as I've said, I'm job hunting." -> written 2023-03-04
  - "..., and I've been stressed, I'm job hunting." -> written 2023-03-04
  - "..., I'm job hunting, which I've done before." -> written 2023-03-04
  (`c_r3c.py`)
- RS-3 (Low). The N9 unrelated-repeat cases are still fail-safe losses: "I've lived in Denver since 2019.
  In 2019 my dad retired." -> state_start, and "I've been vegan since March. Since March was so busy, ..."
  -> causal_since.
- RS-4 (Low). "she's moved to the night shift" ('s + participle) -> causal_since on both. "for the better
  part of a year" -> state_start on both (five tokens before the unit, the cap is four).
- RS-5 (Low). N6 C1 "Since I moved to Denver in May I've tried every taco place, and I love it." and C6
  -> causal_since on both.
- RS-6 (Medium by the brief's rule, pre-existing). A forward "for" duration with a number is exempt on
  both trees: "for two more weeks" -> written 2023-06-29, "for a few days next month" -> written
  2023-07-16. See the R3-2 fix.
- RS-7 (Info). The L1 ("since about February 20th" -> state_start) and L2 ("I work from home since March."
  -> causal_since) outputs are unchanged.

## Info

- CHANGELOG.md diff: no non-ASCII characters in the added lines (Python scan of `git diff
  f8bf270..2d5a311` "+" lines: 0).
- `state_start_guard` now does `raw.strip().strip('"')` only when `isinstance(raw, str)`. Before, a
  non-str truthy expression (for example an int) raised `AttributeError` in `_norm`. That is a
  robustness gain. `clause_guard` (674) still calls `_norm` on the raw value, so a non-str truthy
  expression would still raise there. Pre-existing and not reachable from `parse_items` if it enforces
  strings; not checked.
- The `_occurrences` substring fallback (797-807) keeps f8bf270's substring semantics when no whole-word
  match exists. So an expression found only inside a longer word ("last week" in "last weekend" with no
  standalone "last week") is still judged at that substring.
- event_time.py `state_start` branch: `window_start <= recorded <= window_end` compares ISO strings, which
  is correct for `YYYY-MM-DD` values written by `_iso`. If `_utc_date_text` falls back to `text[:10]`
  on unparseable input, the comparison is still string-safe, with no exception.

## Open questions

- OQ1: live frequency of R3-1 shapes ("and now", "and then", a name after "and/but/so" in a turn with a
  start word). 13 of 179 replay turns have the surface shape. None produced an item where it mattered.
  To settle it, run the replay with the R3-1 fix as a mutant and sample live episodes.
- OQ2: whether the resolver emits `kind: state` with a dated basis for "for the next two weeks" or "for
  next week" (R3-2). The frozen checkpoints have no such expressions. To settle it, check live resolver
  output for forward "for" phrases.
- OQ3: whether R3-5 should be Medium. It is a fail-safe loss by the brief's rule, but "have + regular
  participle + bare object + since <date>" is the temporal-since shape this guard exists to keep. Its
  frequency in live traffic is unknown, and in the 22 runs it is zero.

## Coverage table

Scope enumerated with `git diff --stat f8bf270..2d5a311`: 4 files. Current line counts (`wc -l`):
1103 + 172 + 493 + 100 = 1868.

| File | Lines | Status |
|---|---|---|
| src/menhir/infrastructure/anchored_time.py | 1103 | read-deep 590-880 (guards, all changed code) and 1040-1103 (plan_overlay tail); 939-947 `_graphiti_dates`; rest unchanged since rounds 1-2 |
| src/menhir/domain/event_time.py | 172 | read-deep, whole file |
| tests/test_anchored_time_state_start.py | 493 | read-deep: full diff hunk (171-191, 317-443); 282-291 and test list via grep; mutation-tested |
| CHANGELOG.md | 100 | diff hunk read; ASCII check by script |

Rows: 4 = 4 enumerated files. Context read outside the diff: anchored_time_persist.py 112-146
(`contract_rows`), ../xs_replay.py 1-84, round-2 harness files.

## Verification notes

- Every repro ran through the clone's `.venv` Python, importing `menhir` from git-archive extracts of
  f8bf270 and 2d5a311 in scratch `r3rev/`. The clone was never written to; the only in-clone run was the
  focused pytest the brief prescribes. Production order in every run: `clause_guard`, then
  `state_start_guard`, then `plan_overlay`, with Graphiti baseline = speech date unless stated.
- Mutation testing wrote only to `r3rev/mut` and restored the file afterwards.
- Timing numbers are CPU wall time on a local machine, single run each.
- Not checked: live Neo4j persistence and end-to-end recall rendering. Render was checked by calling
  `rendered_event_time` on constructed rows. Resolver behaviour on forward durations (OQ2) was not
  checked, because no paid calls were made.

## Review confidence: 80/100

All round-2 inputs were re-run on both commits, and the replay was diffed per fact. `_sentence` was
checked exhaustively. Every new finding has a reproduced input and output on both commits, so regression
vs residual is stated per row. Confidence is held back because severity for R3-1 and R3-2 depends on live
input and resolver frequency (OQ1, OQ2), which the frozen corpus does not exercise, and because the probe
inputs are hand-built rather than sampled from live traffic.


---

<!-- combined source: menhir-pr274-round4-review.md (prefix r4) -->

# menhir PR #274 round 4 review - review-fix commit c0dbdb0

- Branch: fix/anchored-time-cross-sentence. HEAD c0dbdb0 (`c0dbdb0f42b864844c181a581594ebd340f4a8ba`), parent 2d5a311
- Clone: a local scratch clone
  (not modified: `git status --short` was empty before and after the run)
- Round 3: .agent/reviews/menhir-pr274-round3-review.md
- Reviewer: Claude (read-only lane; nothing pushed, committed, or commented; no paid calls; no containers)
- Date: 2026-10-12
- Work dir: r4rev. It holds git-archive extracts `c0/` (c0dbdb0) and `mut/` (for mutation) and reuses
  `r3rev/d5` (2d5a311). Probe scripts: `c_r4.py`, `c_r4b.py`, `c_r4c.py`, `c_cost4.py`, `corpus4.py`,
  `mutate4.py`. Outputs: `out_<script>_d5.txt` and `out_<script>_c0.txt`. The round-3 harness outputs for both
  trees, regenerated here, are `out_c_*_{d5,c0}.txt`.

## Executive summary

c0dbdb0 does what it claims on the round-3 inputs:

- R3-1: all eight M2 rows are `state_start` again.
- R3-2: all seven forward or bounded "for" phrases are `state_start`.
- R3-3: all eight -ed-adjective rows and the three side-clause rows are `causal_since`.
- R3-4: every timing shape runs in at most 0.013 s, down from up to 8.1 s.
- R3-5 and R3-6, explicit-date half: "I've practiced yoga since March." and "Since March I've been vegan." write
  again.
- R3-7: fixed for "which".
- R3-8: the first-occurrence mutant is killed again.
- R3-9: the vacuous param was removed.
- R3-10: the render now allows the one-day margin.

The 22-run replay matches 2d5a311 on every fact: 0 changed rows out of 2374, with 2263 ok, 140 writes, 0 wrong
writes and 0 harmful.

Three of the new accepting rules write wrong dates that 2d5a311 did not:

- **R4-1 (High).** The new prepositional-since rule calls "since" + time temporal whenever the next word is a
  subject pronoun, "my/our", or "and/but/so/or/when/until/till". Causal since-clauses that put their time first
  now write that time on the fact. Examples: "Since last week I told you my sister is a nurse, ..." and "Since
  March and April were slow, I'm cutting costs." All 15 probes write on c0dbdb0, and all 15 are `causal_since`
  on 2d5a311. The rule applies to every kind, including point events. It also bypasses the state-start check,
  so a fact from another clause takes the date: "I'm off caffeine since Monday, and my back is sore." writes
  Monday on "back is sore".
- **R4-2 (Medium).** Two rules combine: `_segment` now cuts at every "and/but/so", and its fronted-time
  extension can then reach into the next clause. As a result, the start word comes from one coordinate clause
  and the shared word from the next. "I started a new job and last week, my back was sore." writes last week on
  "back is sore".
- **R4-3 (Medium).** `_BACK_REF_START` now accepts "which/that/this/everything started" + a gerund, and it never
  checks the antecedent. "I joined a gym, which started offering yoga last week, and my back is sore." writes
  last week on "back is sore". So does "My knee, which started hurting last week, is still sore." with an
  unrelated fact "The user's back is sore."

Lower-severity items:

- R4-4: two forward "for" forms are newly exempt, and the explicit `more` slot keeps "for two more weeks"
  exempt.
- R4-5: the prepositional rule does not fire when the resolver puts "since" inside the expression.
- R4-6: past the cap, a "since" occurrence (even "ever since") drops the basis.
- R4-7: the render margin hides Graphiti's own date one day outside the window.
- R4-8: a few correct writes are lost.
- R4-9: test gaps. Five mutants survive, including the mutant that removes the conjunctions behind R4-1.

There are also two residuals. RS4-1: a sentence-initial side clause ("Like I've said, since ...") still makes a
causal since temporal. RS4-2: unchanged residuals from round 3.

Counts: High 1, Medium 2, Low 6, residual 2.

## Test run

```
MENHIR_ANCHORED_TIME_EVAL_DIR=<eval-dir> \
  .venv/Scripts/python.exe -m pytest tests/test_anchored_time_state_start.py tests/test_anchored_time_replay.py \
  tests/test_anchored_time_guard.py tests/test_anchored_time_render.py -q -p no:cacheprovider
FAILED tests/test_anchored_time_replay.py::test_heldout_replay_matches_the_prototype[heldout3:frozen]
1 failed, 143 passed, 2 warnings in 2.06s
```

The one failure is the known heldout3:frozen parity. Round 3 had 119 passed; the 24 extra are the new tests and
params in `tests/test_anchored_time_state_start.py`.

## 22-run replay (priority 2)

```
.venv/Scripts/python.exe -I -B xs_replay.py r4rev/c0 r4rev/c0.json
runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0
.venv/Scripts/python.exe -I -B r2rev/cmp.py r3rev/d5.json r4rev/c0.json
(no output: 0 rows differ in written, value, reason or guard)
```

A fresh run of 2d5a311 (`r4rev/d5_fresh.json`) also differs from `r3rev/d5.json` in 0 rows. No correct write is
lost in the 22 runs.

`corpus4.py` reports which replay items the new rules decide. Over 172 distinct turns and 276 distinct dated
items, there are 20 "since" occurrences. The prepositional rule decides 0 of them (no perfect, no "ever"), and
0 items exceed the occurrence cap. The frozen runs therefore do not exercise R4-1 or the cap at all.

## Findings

### R4-1 (High) - The prepositional-since rule makes a causal "since" temporal when a subject pronoun, "my/our" or a conjunction follows the time

- **Location:** `src/menhir/infrastructure/anchored_time.py:749-750` (`_NO_VERB_AFTER`), used at 837-840 in
  `_start_reason`.
- **Mechanism.** `prepositional = at + len(since) <= pos and _NO_VERB_AFTER.match(low, pos + n)`. When it holds,
  the item skips both the perfect test and the state-start test, so `_start_reason` returns `None` and the date
  is written. `_NO_VERB_AFTER` accepts `i|we|you|he|she|they|i've|i'm|we've|my|our|and|but|so|when|until|till|or`
  after the time. In a causal clause, though, the time is often a fronted adverbial followed by the clause's own
  subject ("since [last week] [I] told you ..."). It can also be the first of two coordinated subjects ("since
  [March] and April were slow"). In both cases the next word is on the list, while the verb comes later. The
  rule cannot tell "Since March I've been vegan" (temporal) from "Since last week I told you ..." (causal),
  because only the first word after the time is checked.
- **Reproduced** (`r4rev/c_r4.py` section A). Speech date 2023-06-15 unless the expression is explicit. A1 has
  two items with the same expression; the rest have one item.

  | # | Turn | Fact (kind) | Expr | 2d5a311 | c0dbdb0 |
  |---|---|---|---|---|---|
  | A1 | "Since last week I told you my sister is a nurse, you know why I worry." | "The user's sister is a nurse." (state) and item 0 "The user told the assistant ..." (point_event) | last week | causal_since, causal_since | written 2023-06-08, written 2023-06-08 |
  | A2 | "Since last week I was out sick, I'm behind at work." | "The user is behind at work." (state) | last week | causal_since | written 2023-06-08 |
  | A3 | "Since yesterday I mentioned my diabetes, I'm sharing my numbers." | "The user has diabetes." (state) | yesterday | causal_since | written 2023-06-14 |
  | A4 | "Since March and April were slow, I'm cutting costs." | "The user is cutting costs." (state) | March | causal_since | written 2023-03-16 |
  | A5 | "Since Monday or Tuesday is my day off, I'm free to help." | state | Monday | causal_since | written 2023-06-12 |
  | A6 | "Since 2019 my landlord keeps raising the rent, I'm moving out." | "The user is moving out." | 2019 | causal_since | written 2019-07-02 |
  | A7 | "Since last month we adopted a dog, my allergies are bad." | "The user's allergies are bad." | last month | causal_since | written 2023-05-16 |
  | A8 | "Since yesterday, as you know, was my last day, I'm unemployed." | state | yesterday | causal_since | written 2023-06-14 |
  | A11 | "Since last week I was sick, I missed the meeting." | "The user missed the meeting." (point_event) | last week | causal_since | written 2023-06-08 |
  | A12 | "Since yesterday so many people called, I'm exhausted." | state | yesterday | causal_since | written 2023-06-14 |
  | A13 | "Since Monday when I got fired was awful, I'm job hunting." | state | Monday | causal_since | written 2023-06-12 |
  | A14 | "Since last week we talked about my divorce, I'm single now." | "The user is single." | last week | causal_since | written 2023-06-08 |
  | A15 | "Since yesterday my dad is a lawyer, he can help." | "The user's dad is a lawyer." | yesterday | causal_since | written 2023-06-14 |

  The temporal "since" does not check whose time it is either (A9, A10, `c_r4b.py` F3):

  | # | Turn | Fact | Expr | 2d5a311 | c0dbdb0 |
  |---|---|---|---|---|---|
  | A9 | "I'm vegan since March, and my back is sore." | "The user's back is sore." | March | causal_since | written 2023-03-16 |
  | A10 | "I'm off caffeine since Monday and my knee hurts." | "The user's knee hurts." | Monday | causal_since | written 2023-06-12 |
  | F3 | "I'm off caffeine since Monday, and my back is sore." | "The user's back is sore." | Monday | causal_since | written 2023-06-12 |

  Controls:
  - With a verb right after the time, "Since last week was rough, I'm behind at work." gives causal_since on
    both trees.
  - With a perfect, "I've been off caffeine since Monday, and my back is sore." (F1) and "ever since" (F2) write
    on both. That cross-clause write is pre-existing for perfect and "ever since" mains. c0dbdb0 extends it to
    every non-perfect main whose "since" phrase is followed by punctuation or a listed word.
- **Impact.** These are the PR's own defect classes. A causal "since" writes a date that dates another event
  (K04.3 class), and a state takes a co-mentioned time (M2 class). The time is written as `valid_at` for states
  and point events. A1 and A3 put a "told you"/"mentioned" date on a fact that started long before. A9, A10 and
  F3 put another clause's start on a state. The 22 runs contain 0 items decided by this rule (`corpus4.py`), so
  frequency is unmeasured (OQ1). The resolver gives the bare time ("last week", "March") in many checkpoint
  items. 30 of 819 checkpoint expressions start with "since" (`exprs.txt`, from a grep of `ckpt_*.jsonl`), and
  R4-1 is limited to the rest.
- **Fix** (smallest change, anchored to existing symbols):
  1. Restrict `_NO_VERB_AFTER` to clause-final punctuation or the end
     (`r"\s*(?:[.,;:!?)]|$)"`), plus a subject that is directly followed by a perfect:
     `(?:i|we|you|he|she|they)(?:'ve|'d|\s+(?:have|has|had)(?:n't)?)\b`. Drop `and|but|so|or|when|until|till`
     and bare `my|our`. This was checked on the private copy (`r4rev/fixcheck.py`, which applies the change to
     `mut/`, runs `c_r4.py` and the test file, then restores). All 82 tests in
     `tests/test_anchored_time_state_start.py` pass, including the three
     `test_since_right_before_a_time_is_temporal` params. A1-A7 and A10-A15 become `causal_since`. A8 (comma
     after the time) and A9 (another clause's state, comma after the time) still write, so step 2 is needed.
  2. When "since" is temporal only because of this rule (no perfect, no "ever"), still run the state checks
     (fall through to the `elif` branch for states). Alternatively, require `_content(fact) &
     _content(low[a:b])` over the `_segment` of the since phrase. Either closes A9, A10 and F3.
  3. Add A1, A4, A9 and A11 as `causal_since` or `state_start` tests. The mutant that removes the conjunctions
     from `_NO_VERB_AFTER` survives today (R4-9).

### R4-2 (Medium) - Every-"and" cut plus the fronted-time extension: the start word comes from one coordinate clause and the shared word from the next

- **Location:** `src/menhir/infrastructure/anchored_time.py:746` (`_COORD` cuts at every `\s(?:and|but|so)\s`),
  `_segment` 808-817 (extension at 815-816), `_start_reason` 844-851 (`c_text` from `_clause_start_and_text`,
  `about_fact` from `low[a:b]`).
- **Mechanism.** `_STARTED` is searched in `c_text`. That is the comma clause from `_clause_start_and_text`
  (643-656), which does not cut at "and". `about_fact` is searched in the `_segment` span, which does cut at
  "and" and, when the span holds only the time, extends to the next cut. For "X started ... and <time>, Y ...",
  then:
  - `c_text` is "X started ... and <time>". It has content besides the time, so it is not replaced, and it
    contains "started".
  - The segment "<time>" is empty, so it extends past the comma to "<time>, Y ...".
  - `about_fact` matches Y's words.

  The two tests now look at different clauses. In 2d5a311 "and" before a non-subject word did not cut. The
  segment was the whole comma clause, so it had content and was not extended, and `about_fact` was empty.
- **Reproduced** (`c_r4.py` section C; item 0 point_event, item 1 state, same expression):

  | Turn | Fact 1 | 2d5a311 item 1 | c0dbdb0 item 1 |
  |---|---|---|---|
  | "I started physical therapy and three weeks ago, my knee was still sore." | "The user's knee is sore." | state_start | written 2023-05-25 |
  | "I started a new job and last week, my back was sore." | "The user's back is sore." | state_start | written 2023-06-08 |
  | "I started a new diet but last week, I was still sick." | "The user is sick." | state_start | written 2023-06-08 |
  | control "I started a new job, and last week, my back was sore." | "The user's back is sore." | state_start | state_start |

- **Impact.** A borrowed time is written as a state's start (M2 class). The shape needs a start word in an
  earlier "and/but/so" coordinate, then the time, then a comma. That is less common than R3-1's shapes, but it
  is a new write path.
- **Fix.** Search the start word in the same span as the evidence. In `_start_reason`, when `_segment`
  extended `b` (or always), use `c_text = turn[a:b]` instead of the comma clause. The alternative is to require
  `_STARTED.search(low[a:b]) or _STARTED.search(expr)`. Then add the three rows as `state_start` tests.

### R4-3 (Medium) - `_BACK_REF_START` now takes "which/that/this/everything started" + a gerund as a back-reference without checking that the antecedent is the fact

- **Location:** `src/menhir/infrastructure/anchored_time.py:740-743`, used at 851.
- **Mechanism.** The lookahead `(?!\s+(?:to\b|[a-z]+ing\b))` now guards only the "it" branch. The second branch
  (`this|that|which|everything`) accepts any following word. `about_fact` is empty in these turns, so the
  back-reference alone decides, and nothing links the relative pronoun's antecedent (included in `c_text` by
  `_clause_start_and_text`, 654-655) to the fact.
- **Reproduced** (`c_r4.py` section D and `c_r4c.py`; state item "last week", speech 2023-06-15):

  | Turn | Fact | 2d5a311 | c0dbdb0 |
  |---|---|---|---|
  | "I joined a gym, which started offering yoga last week, and my back is sore." | "The user's back is sore." | state_start | written 2023-06-08 |
  | "Everything started going wrong last week, and my back is sore." | "The user's back is sore." | state_start | written 2023-06-08 |
  | "My sister got a puppy, and that started making noise last week, so I'm tired." | "The user is tired." | state_start | written 2023-06-08 |
  | "My class, which started meeting last week, is in Denver, and I'm tired." | "The user is tired." | state_start | written 2023-06-08 |
  | "My knee, which started hurting last week, is still sore." | "The user's back is sore." (unrelated) | state_start | written 2023-06-08 |

  Pre-existing on both trees: "I joined a gym, which started last week, and my back is sore." writes 2023-06-08
  for "back is sore" and for "knee is sore". The antecedent was never checked. c0dbdb0 extends the gap to every
  gerund or "to" form.
- **Impact.** A co-mentioned event's start is written on an unrelated state. This is the round-3 R3-7 fix
  overshooting. Round 3 recommended exactly this change ("apply the lookahead only to bare it"), so this finding
  also corrects that recommendation.
- **Fix.** Accept a relative back-reference only if the antecedent words share a content word with the fact.
  `c_text` already holds the antecedent's last four words for a `_REL` clause, so the check is
  `_content(fact, exclude=_content(expr)) & _content(c_text)`. Keep "it all"/"everything" as unconditional only
  when it is sentence-initial and follows a sentence about the fact, or not at all. The minimum is to require
  `about_fact or (_BACK_REF_START.search(c_text) and _content(fact, exclude=_content(expr)) & _content(c_text))`.
  Add rows 1 and 5 as `state_start` tests.

### R4-4 (Low) - `_RUNS_TO_NOW` newly exempts long forward forms, and its explicit `more` slot keeps "for two more weeks" exempt

- **Location:** `src/menhir/infrastructure/anchored_time.py:730-736` (`_QTY`, `_RUNS_TO_NOW`, the slot
  `(?:more\s+|long\s+|good\s+)?`).
- **Reproduced** (`c_r4.py` section E; state, turn "I'm staying with my sister <expr>.", offset direction "after"):

  | Expression | 2d5a311 | c0dbdb0 |
  |---|---|---|
  | "for about two and a half more weeks" | state_start | written 2023-06-20 (future) |
  | "for just over a couple of more weeks" | state_start | written 2023-06-29 (future) |
  | "for two more weeks", "for a couple more weeks", "for a few more days" | written 2023-06-29 / 06-29 / 06-18 | same (pre-existing RS-6) |
  | "for a week", "for a few days starting Monday" | written 2023-06-22 / 06-18 | same (pre-existing) |
  | "for another two weeks" | written 2023-06-29 | state_start (fixed) |

  2d5a311 allowed at most four tokens between "for" and the unit. c0dbdb0 allows a qualifier, a `_QTY` with
  "and a half", and "more", which is up to nine tokens. The first two rows are contrived. The `more` slot is the
  substantive point: "for N more <unit>" is almost always forward, and the new regex blesses it explicitly. No
  test pins it either way (the `runs_more_slot_removed` mutant survives, R4-9).
- **Fix.** Drop `more\s+` from the slot. As round 3 recommended, also never write a state's `valid_at` after
  `speech_date` in `plan_overlay`, which closes every forward form at once.

### R4-5 (Low; fail-safe) - The prepositional rule never fires when the resolver's expression starts with "since"

- **Location:** `anchored_time.py:837` (`at + len(since) <= pos`). When the expression itself starts with
  "since", `_since_operator` returns `at = pos` (781), so the condition is false.
- **Reproduced** (`c_r4.py` A-ctl): "I work from home since March." with expression "March" is written
  2023-03-16 on c0dbdb0. With expression "since March" it gives causal_since on both trees.
- **Impact.** The same text is judged differently depending on how the resolver cut the expression. 30 of 819
  checkpoint expressions start with "since" (9 x "since February 20th"). If R4-1 is fixed by narrowing, the fix
  should treat both forms the same.
- **Fix.** Compute `prepositional` from the end of "since" whether or not "since" is inside the expression:
  check the expression text after the operator plus `_NO_VERB_AFTER` at `pos + n`.

### R4-6 (Low; fail-safe) - Past the occurrence cap, any "since" occurrence, even "ever since", drops the basis

- **Location:** `anchored_time.py:882-885`.
- **Reproduced** (`c_r4b.py` G, H; state, explicit "2019"/"2019"):
  - "I've lived in Denver since 2019." followed by 8 sentences "In 2019 item k happened." (9 occurrences) gives
    `state_start` on 2d5a311 and `causal_since` on c0dbdb0. The basis is set to none, so facts anchored on the
    item become unplaceable.
  - "Ever since 2019 I've been vegan." followed by 9 notes "Note k is from 2019." gives `state_start` on
    2d5a311 and `causal_since` on c0dbdb0. That contradicts "'ever since' is always temporal".
  - With 8 occurrences in total, both trees give `state_start` (G2).
- **Safety.** The cap cannot let a wrong date through. When any occurrence follows "since", the result is the
  maximum rank. Otherwise a state gets `state_start`, the most a state can get without "since", and a non-state
  gets `None`. That is what `_start_reason` returns for a non-state with no "since" at any occurrence (lines
  836-842). Timing: every R3-4 shape now runs in 0.004-0.013 s. 40 distinct expressions at 3-7 occurrences each
  in one 4000-character sentence run in 0.003-0.047 s (`c_cost4.py`).
- **Fix.** Past the cap, return `state_start` for a state and `None` otherwise. Use `causal_since` only if no
  occurrence has "ever since" and the item would be causal on its first since-occurrence. Or simply judge the
  since-occurrences (usually few) in full and cap only the rest.

### R4-7 (Low) - The render margin hides Graphiti's own date one day outside the borrowed window

- **Location:** `src/menhir/domain/event_time.py:128-137` (`_near_window`), used at 70.
- **Reproduced** (`c_r4b.py` J; `state_start`, window 2023-06-05..2023-06-11, speech 2023-06-15):
  - `valid_at` 2023-06-04T12:00Z renders `2023-06-04` on 2d5a311 and `event time unknown (said 2023-06-15)` on
    c0dbdb0.
  - 2023-06-12T12:00Z renders the same way on both.
  - 06-03 and 06-13 still render.
- **Impact.** A genuine Graphiti date on the day before or after the window is no longer shown. The margin is
  applied to every timestamp, including midday UTC values that cannot be a time-zone shift. This trades a rare
  wrong render (R3-10) for a rare missing one. The start-side margin is unpinned: the
  `near_window_end_only` mutant survives.
- **Fix.** Optional. Apply the margin only when the stored `valid_at` has a non-UTC offset or a time within the
  speech-zone shift of midnight, or persist the speech-zone date.

### R4-8 (Low; fail-safe) - Correct writes lost in c0dbdb0, and claims that hold only in part

Each row was written (or was the claimed behaviour) on 2d5a311 and is not on c0dbdb0, or the claim does not
cover it:

- "for most of the year" and "for this long" give `state_start` on c0dbdb0. On 2d5a311 they wrote 2022-12-15
  and 2022-06-15 (`out_c_new2`, `c_r4.py` E). "For most of the year" was a round-2 N7 fix.
- "Since the meeting on 3/4, that's all I've thought about." gives `causal_since`; 2d5a311 wrote 2023-03-04.
  `_SIDE_CLAUSE` drops a main clause that begins with "that", "as", "like" or "so" (`c_r4.py` B6).
- R3-5, event-anchored half, is unchanged: "Since the meeting on 3/4, I've started running every day / learned
  Spanish / gained weight / tried yoga / moved twice" is still `causal_since` on both trees (`out_c_r3` R rows).
  The prepositional rule helps only when "since" sits right before the time.
- R3-7 is only partly fixed. "My back went out. It all started going downhill last week." is still
  `state_start` (`out_c_r3b` X2). The "it" branch's lookahead also covers "it all/really/first", so the claim
  "only dummy 'it' excludes" does not hold for "it all".
- The -ed branch no longer accepts a comma after the participle (`[.;:!?)]`), so "As I've mentioned, since
  ..." now gives `causal_since` (`c_r4.py` B2). That is the correct outcome, reached by a different route: the
  head segment is still kept (see RS4-1).

### R4-9 (Low; test quality) - Mutation results for the new behaviour

`r4rev/mutate4.py` ran on a private extract (`r4rev/mut`) against `tests/test_anchored_time_state_start.py`.
Baseline: 82 passed. The file was restored afterwards.

| Mutant | Result | Killing test |
|---|---|---|
| `prepositional = False` | KILLED | test_since_right_before_a_time_is_temporal (3 params) |
| `_NO_VERB_AFTER` punctuation/end only | KILLED | same, two no-comma params |
| **`_NO_VERB_AFTER` without and/but/so/when/until/till/or** | **SURVIVED** | none |
| `_NO_VERB_AFTER` any word | KILLED | test_a_date_right_after_since_is_a_since_phrase, test_since_before_a_time_that_is_the_subject_is_causal |
| no `at + len(since) <= pos` check | KILLED | 19 tests |
| no cap | KILLED | test_too_many_occurrences... param 2 only |
| cap ignores since | KILLED | same |
| cap: state gets None | KILLED | param 1 |
| `before = [head] + tail` (no `_SUBORDINATE` filter) | KILLED | test_a_perfect_in_a_side_clause_before_since_is_not_the_main_clause |
| `before = [head]` | KILLED | test_a_coordinated_main_clause_before_since_is_the_main_clause |
| `after` keeps every segment | KILLED | two side-clause params |
| **`after` filter = `_SUBORDINATE` (and/but/so/or kept)** | **SURVIVED** | none (the ", and I've been stressed," row of round 3 is not a test) |
| `_COORD` back to 2d5a311 | KILLED | three R3-1 params |
| lookahead on every `_BACK_REF_START` branch | KILLED | "which started hurting" param |
| **`_RUNS_TO_NOW` without the `more` slot** | **SURVIVED** | none |
| `_RUNS_TO_NOW` back to 2d5a311 | KILLED | test_forward_spans_are_not_a_start (5) |
| **adverb slot back to `[a-z]+ly`** | **SURVIVED** | none |
| -ed followed by a comma accepted | KILLED | "I have tired, sore legs" |
| broken/set/read back in `_IRREGULAR` | KILLED | two params |
| first occurrence only | KILLED | test_a_repeated_expression_is_judged_at_every_occurrence |
| `_near_window` exact | KILLED | render "2023-05-28T21:00:00-06:00" param |
| **`_near_window` without the start-side day** | **SURVIVED** | none |

Other test notes:

- The param `test_too_many_occurrences_are_treated_conservatively[Note 0 ... (state)]` (tests 451-459) also
  passes with no cap at all, because every occurrence already yields `state_start`. It pins the cap branch's
  state rule, not the cap.
- `test_since_before_a_time_that_is_the_subject_is_causal` (443-449) uses "2/15th", so only the substring
  fallback matches. The follower is "th", which `_NO_VERB_AFTER` rejects by accident of tokenization. The plain
  "Since 2/15 is my deadline" would test the stated rule more directly. Not vacuous: it kills the "any word"
  mutant.
- The I-a ("whatever    since") and N13 (newline after "since") fixes still have no test (grep of the test file
  for "whatever" returns nothing).

## Residuals (same output on 2d5a311 and c0dbdb0)

- **RS4-1 (Medium by the brief's rule; c0dbdb0 claim gap).** The CHANGELOG says side clauses "such as ', as I've
  said'" are read out of the main clause. A sentence-initial side clause is the head segment, and `_main_clause`
  always keeps it (`head, *tail = sent[:a].split(",")`, 802-803). These are written 2023-03-04 on both trees
  (`c_r4.py` B):
  - "Like I've said, since the meeting on 3/4 went badly, I'm job hunting."
  - "Because I've been stressed, since the meeting on 3/4 went badly, I'm job hunting."
  - "So I've thought about it, since the meeting on 3/4 went badly, I'm job hunting."
  - "I've told my boss already, and since the meeting on 3/4 went badly, I'm job hunting." The coordinated
    perfect belongs to a different event.
  - "Since the meeting on 3/4 went badly, I'm job hunting and I've updated my CV." (no comma before "and")

  Fix: apply `_SUBORDINATE` to the head too, after stripping a leading `so|and|but|well` (the `_LEAD` set, 610).
  For a coordinated clause before "since", consider only the segment that holds "since" plus the segments
  after it.
- **RS4-2 (round-3 residuals, unchanged).**
  - RS-1: a shared generic word. "I started a new job last week, and I bought a new car." still writes
    2023-06-08 for "has a new car" (`out_c_r3` Q1, Q2).
  - "for two weeks last month" writes 2023-05-16 (`out_c_r3` U).
  - "for a few days next month" writes 2023-07-16 (`out_c_r3b` Y).
  - "I have left knee pain / lost luggage claims / found objects" after a causal since still write 2023-03-04
    (`c_r4b.py` I). "left", "lost" and "found" are adjectives as often as the removed "broken" and "set".

## Round-3 claim verification (priority 3)

All round-3 harness files were re-run on both trees (`out_c_{r3,r3b,r3c,new,new2,new3,got,l12,round1}_{d5,c0}.txt`).

| Claim | Round-3 input | 2d5a311 | c0dbdb0 | Holds? |
|---|---|---|---|---|
| R3-1 | P1-P8 (and now / John / so Mom / and then / but honestly / and also / fronted) | written | state_start (all 8) | yes; cost: F1/F2 coordinated objects now state_start (accepted) |
| R3-2 | for the next two weeks / next week / the coming weeks / this week / the week / the rest of the year / the first two weeks | written or inside-window | state_start (all) | yes; R4-4 for "more" |
| R3-3 | T rows (limited family time, tired, sore legs, mixed and confusing, assigned weekly, mixed up, broken ribs, set hours, read-only) | written | causal_since (all 8) | yes; RS4-2 for left/lost/found |
| R3-3 side clauses | ", as I've said," / ", and I've been stressed," / ", which I've done before." | written | causal_since (all 3) | yes after since; no for a head clause (RS4-1) |
| R3-4 | `c_cost4.py`: periods / commas / no commas / since | 0.100 / 2.715 / 8.110 / 0.602 s | 0.013 / 0.013 / 0.012 / 0.004 s | yes |
| R3-5 | R2 explicit (practiced yoga, played tennis, studied French, used Linux, coached soccer) | causal_since | written | yes |
| R3-5 | R event-anchored (started running, learned Spanish, ...) | causal_since | causal_since | no (R4-8; not claimed) |
| R3-6 | "Since March I've been vegan." / "Since 2019 I have lived in Denver." | causal_since | written | yes |
| R3-6 | event-anchored no comma (S since_pair, Z1) | causal_since | causal_since | accepted miss (CHANGELOG) |
| L2 | "I work from home since March." | causal_since | written 2023-03-16 | changed (bare expr); see R4-5 |
| R3-7 | X1 "which started hurting", X3 "which started to" | state_start | written | yes; X2 "It all started going" still state_start (R4-8); overshoot R4-3 |
| R3-8 | first-occurrence mutant | survived | killed | yes |
| R3-9 | "The user is employed" param | present | removed | yes |
| R3-10 | valid_at 2023-06-11T21:00-06:00 / 2023-06-12T03:00Z | 2023-06-12 | event time unknown | yes; R4-7 cost |
| N13 | `_main_clause("Since\nFebruary 20th, I'm hoping, ok", ...)` | `"  i'm hoping, ok"` | `", i'm hoping, ok"` | no perfect either way; fine |
| N7 | "for most of the year" | written | state_start | lost (R4-8) |

## Info

- CHANGELOG and diff: 0 added lines with non-ASCII characters (Python scan of the `git diff 2d5a311..c0dbdb0`
  "+" lines).
- `_main_clause` now joins segments with ",". `_PERFECT` needs `\s+` between the auxiliary and the participle,
  and its -ed lookahead no longer accepts ",". A dropped middle segment therefore cannot glue "have" to a
  participle. The returned text is a subset of 2d5a311's (2d5a311 kept all of `sent[:a]` and everything after
  the first comma not followed by a `_REL` word). So `_main_clause` cannot make a causal "since" temporal where
  2d5a311 did not. Checked by reading lines 796-805 against the 2d5a311 hunk.
- `_near_window` receives `str` or `None` only. `event_time_from_row` maps every field through `text()`, which
  returns `str(value)`. A `TypeError` from `date.fromisoformat` is therefore unreachable, and a `ValueError`
  falls back to string comparison.

## Open questions

- OQ1: How often live turns contain "Since <time> <pronoun|my|our|and|or|when> ..." as a causal clause, and how
  often the resolver then attaches the bare time to a state or point event (R4-1). The 22 runs have 0 such items.
  To settle it, run a live sample with the R4-1 narrowing as a mutant and count `written -> causal_since`
  changes.
- OQ2: Whether the resolver emits `kind: state` with an "after" offset for "for N more <unit>" (R4-4). This was
  round-3 OQ2 and is still unchecked, because no paid calls were made.

## Coverage table

Scope from `git diff --stat 2d5a311..c0dbdb0`: 4 files, 153 insertions, 36 deletions. Current line counts
(`wc -l` on the c0dbdb0 extract): 1133 + 183 + 569 + 100 = 1985.

| File | Lines | Status |
|---|---|---|
| src/menhir/infrastructure/anchored_time.py | 1133 | read-deep 590-919 (guards, every changed line); the rest is unchanged since rounds 1-3 |
| src/menhir/domain/event_time.py | 183 | read-deep 1-140 (render, `event_time_from_row`, `_near_window`) |
| tests/test_anchored_time_state_start.py | 569 | read-deep: full diff hunk (182-188, 282-291, 325-334, 375-459, 506-513); 1-128 header and since tests; mutation-tested |
| CHANGELOG.md | 100 | diff hunk read; ASCII check by script |

Rows: 4 = 4 enumerated files. Context read outside the diff: `xs_replay.py`, `r2rev/cmp.py`,
`r3rev/mutate3.py` and `r3rev/h.py`, and checkpoint expression values (grep).

## Verification notes

- Every repro ran through the clone's `.venv` Python and imported `menhir` from the git-archive extracts
  `r4rev/c0` and `r3rev/d5`. Production order: `clause_guard`, then `state_start_guard`, then `plan_overlay`,
  with the Graphiti baseline equal to the speech date. The only run inside the clone was the focused pytest the
  brief prescribes.
- Mutation wrote only to `r4rev/mut` and restored the file. The clone was never written to.
- Timings are wall time on a local machine, one run each.
- Not checked: live resolver output for the R4-1 shapes (OQ1) or for forward "for" phrases (OQ2), since no paid
  calls were made. Neo4j persistence and end-to-end recall rendering were not checked; render was checked by
  calling `rendered_event_time` on constructed rows.

## Review confidence: 82/100

- Every finding has a reproduced input with outputs on both commits.
- The replay was diffed per fact.
- Every round-3 harness was re-run on both trees.
- The new rules were mutation-tested.

Confidence is held back for three reasons:

- R4-1's severity depends on how often causal "Since <time> <subject> ..." appears in live traffic and whether
  the resolver attaches the bare time there. The frozen corpus has none.
- The probes are hand-built.
- `anchored_time.py` outside 590-919 was not re-read this round, because it is unchanged in the diff.


---

<!-- combined source: menhir-pr274-round5-review.md (prefix r5) -->

# menhir PR #274 round 5 review - review-fix commit d81901a

- Branch: fix/anchored-time-cross-sentence. HEAD d81901a (`d81901a762ea5c4994454d24160003e5f7d2d019`), parent c0dbdb0.
  Reference tree: 2d5a311 (round 3), extracted at `r3rev/d5`.
- Clone: a local scratch clone (not modified: `git status --short` was empty after every run).
- Prior rounds: `.agent/reviews/menhir-pr274-round3-review.md`, `.agent/reviews/menhir-pr274-round4-review.md`.
- Reviewer: Claude (read-only lane; nothing pushed, committed, or commented; no paid calls; no containers).
- Date: 2026-10-12.
- Work dir: `r5rev`. Probes: `c_r5a.py` (Q1, since path), `c_r5b.py` (Q2, round-4 variants),
  `c_r5c.py` (Q3, absolute writes), `fz_since.py` / `fz_since_s2.py` (random since-path differential, 12,000 and
  60,000 turns), `fz_state.py` / `fz_state_s2.py` (random state-path differential, 6,000 and 30,000 turns).
  Each takes the menhir repo root as argv[1] and prints `<label>: marked=<list> -> <results>`. `cmp5.py` runs them on
  both trees and lists every write that is new or has a different date. `side.py <stem>` prints both trees side by
  side. Outputs are `out_<script>_new.txt` (d81901a) and `out_<script>_d5.txt` (2d5a311). `mutate5.py` and
  `fixcheck5.py` work only on the private extract `r5rev/mut` and restore it.

## Executive summary

**Q1. Does d81901a write where 2d5a311 does not, or write a different date?** Yes, in two places. Neither is in
`_perfect_in_main_clause`. Both are in the state-start path.

- **R5-1 (Medium, looser than 2d5a311).** R4-2 is only partly closed. The start word must now appear in both the
  comma clause (`c_text`) and the coordinate segment (`low[a:b]`), but each test can still be satisfied by a
  different occurrence. In "I started a new job and last week, I started seeing a doctor about my back.", the first
  "started" satisfies `c_text`. The fronted-time extension, which still runs past the new and/but/so cut, carries the
  segment into the next clause, and that clause supplies the second "started" and the fact word "back". d81901a
  writes 2023-06-08 on "The user's back is sore." 2d5a311 gives `state_start`.
  - Hand-built cases: 5 of 5 in `c_r5c` X1 and 6 in `c_r5b` R2.
  - Fuzzer: 12 and 60 new-only fact writes (`fz_state`, `fz_state_s2`), all with this one mechanism.
- **R5-2 (Medium by the brief's rule, Low by likely frequency; looser than 2d5a311).** Dropping the `more` slot
  closed R4-4. However, `_RUNS_TO_NOW` still exempts five-or-more-token forms that 2d5a311's four-token budget did
  not, for example "for about two and a half weeks", "for more than a couple of weeks" and "for almost two and a half
  weeks". With a forward offset, d81901a writes a date after the speech date (2023-07-02 for speech 2023-06-15).
  2d5a311 gives `state_start` for all 9 X2 rows.

`_perfect_in_main_clause` (the since path) is never looser than 2d5a311:

- By argument (section "Q1: why the since path cannot be looser").
- By test: 0 looser lines over 72,000 random since-path turns and 115 targeted lines (`c_r5a`).

The specific risks the brief named all came out the same on both trees or stricter:

- Blanking with spaces versus removing the text.
- The span bounds check.
- `;` handling.
- Multiple commas.
- A since-clause at sentence start with no comma.
- Expressions that span the since-clause.

**Q2. Are the round-4 findings closed?**

- R4-1 and R4-5: closed. The prepositional rule is gone. The bare and "since"-prefixed expression forms now agree on
  every row.
- R4-2: partly closed. The three original rows are fixed, but a variant reopens it (R5-1).
- R4-3: closed.
- RS4-1: closed for the listed heads and for nine of the side words.
- R4-4: closed for every "more/extra/further/whole" form, but not for the long forms (R5-2).

Remaining RS4-1 shapes write on both trees (RS5-1).

**Q3. Absolute wrong writes on d81901a.** Five residual classes write a wrong date on d81901a. 2d5a311 writes the
same date in every case (RS5-1 to RS5-5):

- RS5-1: a causal "since" read as temporal through a perfect in a clause that is not the main one ("plus", "after",
  "now that", "I mean", list continuations, "who" relatives, "or"/"yet"/dash coordination).
- RS5-2: once "since" is judged temporal, the date is written on any fact.
- RS5-3: the back-reference antecedent is never checked.
- RS5-4: the `CUES["duration"]` exemption ignores forward markers ("for two weeks from now").
- RS5-5: a "since" at the end of a line.

Both candidate fixes were checked on a private copy:

- Tests: 89/89 in `tests/test_anchored_time_state_start.py` pass.
- Replay: 0 rows differ from 2d5a311 over the 22-run replay.
- Probes: 0 lines looser than 2d5a311 across every probe and fuzzer.

Counts: Medium 2 (both looser than 2d5a311), Low 3, residual 5 (same on both trees).

## Test run and replay

```
MENHIR_ANCHORED_TIME_EVAL_DIR=<eval-dir> \
  .venv/Scripts/python.exe -B -m pytest tests/test_anchored_time_state_start.py tests/test_anchored_time_replay.py \
  tests/test_anchored_time_guard.py tests/test_anchored_time_render.py -q -p no:cacheprovider
FAILED tests/test_anchored_time_replay.py::test_heldout_replay_matches_the_prototype[heldout3:frozen]
1 failed, 150 passed, 2 warnings in 2.07s
```

The one failure is the known heldout3:frozen parity failure, unchanged from rounds 3 and 4.

```
.venv/Scripts/python.exe -B xs_replay.py <clone> r5rev/d8.json
runs 22 facts 2374 ok 2263 writes 140 wrong_writes 0 harmful 0
.venv/Scripts/python.exe -B r2rev/cmp.py r3rev/d5.json r5rev/d8.json
(no output: 0 rows differ from 2d5a311)
```

Prior-round probes (`subset_check.py <clone> r3rev/d5`, which runs every `r2rev`, `r3rev` and `r4rev` `c_*.py`)
compared 302 probe lines. 0 are looser than 2d5a311 and 42 are tighter (listed in the run; all are rows that
rounds 3 and 4 asked to stop writing, or accepted misses).

Round-5 differential (`cmp5.py <clone> r3rev/d5 ...`, output in `r5rev/cmp_all.txt` and `cmp_fz_*.txt`):

| Script | Lines | Looser than 2d5a311 (fact writes) | Mechanism of every looser write |
|---|---|---|---|
| c_r5a.py (Q1 since path) | 115 | 0 | - |
| fz_since.py (seed 7) | 12,000 | 0 | - |
| fz_since_s2.py (seed 1234) | 60,000 | 0 | - |
| c_r5b.py (round-4 variants) | 108 | 10 | 6 R5-1, 4 R5-2 |
| c_r5c.py (Q3) | 45 | 14 | 5 R5-1, 9 R5-2 |
| fz_state.py (seed 5) | 6,000 | 2,106 | 12 R5-1, rest R5-2 ("for about two and a half weeks" is one of the 8 times) |
| fz_state_s2.py (seed 4321) | 30,000 | 10,764 | 60 R5-1, rest R5-2 |

## Findings

### R5-1 (Medium; looser than 2d5a311) - The start-word check can pass on two different "started": one in the earlier coordinate, one in the clause the fronted-time extension reaches

- **Location:** `src/menhir/infrastructure/anchored_time.py`:
  - `_COORD`, 746: cuts at every and/but/so.
  - `_segment`, 823-832: the fronted-time extension at 830-831.
  - `_start_reason`, 857-866: `c_text` comes from `_clause_start_and_text`, and the new check is at 862.
- **Mechanism.** The round-4 fix added `_STARTED.search(low[a:b])` next to `_STARTED.search(c_text)` (862). The two
  searches cover different spans:
  - `c_text` is the comma clause, which does not cut at "and". For "I started a new job and last week", it holds the
    first "started".
  - `low[a:b]` is the coordinate segment. It starts after the new " and " cut, so its first part is the time alone.
    `_segment` therefore extends `b` past the comma (830-831) to "last week, I started seeing a doctor about my
    back". That supplies the second "started" and the shared word "back" (`about_fact`, 864).

  In 2d5a311, "and" before a non-subject word did not cut. The segment was "I started a new job and last week", which
  has content, so it was not extended, and `about_fact` was empty.
- **Reproducers** (state item, expression as listed, speech 2023-06-15 unless noted; `c_r5c.py` X1 and `c_r5b.py`
  R2):

  | Turn | Fact | Expr | 2d5a311 | d81901a |
  |---|---|---|---|---|
  | "I started a new job and last week, I started seeing a doctor about my back." | "The user's back is sore." | last week | state_start | written 2023-06-08 |
  | "I started a new job and last week, I started physical therapy for my back." | "The user's back is sore." | last week | state_start | written 2023-06-08 |
  | "Everything started going wrong so last week, I started seeing a doctor about my back." | "The user's back is sore." | last week | state_start | written 2023-06-08 |
  | "The beginning was rough but three weeks ago, I started seeing a doctor about my back." | "The user's back is sore." | three weeks ago | state_start | written 2023-05-25 |
  | "I began running so in March, the gym started offering yoga." (speech 2023-06-01) | "The user does yoga." | in March | state_start | written 2023-03-16 |
  | "I started a new job so last week, my back started acting up again." | "The user's back is sore." | last week | state_start | written 2023-06-08 |
  | "Starting a new job and last week, my back started hurting." | "The user's back is sore." | last week | state_start | written 2023-06-08 |
  | control "I got a new job and last week, I started seeing a doctor about my back." | "The user's back is sore." | last week | state_start | state_start |

  The fuzzer rows (`cmp_fz_state.txt`, `cmp_fz_state_s2.txt`, the lines without "two and a half") all have the
  shape "<clause with start/starting/beginning> and|but|so <time>, <clause with start + fact word>". Examples:
  S711 "The beginning was rough but three weeks ago, I started seeing a doctor about my back, but I'm at the gym."
  and S1185 "... but in March, the gym started offering yoga." on "The user does yoga."
- **Impact.** This is the M2 class: a co-mentioned event's time is written as a state's `valid_at`. Some looser
  writes happen to be right ("Starting a diet so three weeks ago, I started yoga." for "does yoga"). Others are
  wrong: the doctor and PT rows put a treatment start on an older condition, and "the gym started offering yoga"
  puts the gym's date on the user. This breaks the stated goal ("never write where 2d5a311 would not"). The
  standalone form "Last week, I started physical therapy for my back." writes on both trees (RS-1, shared generic
  word), so this widens a pre-existing class to new shapes rather than opening a new class. The replay contains no
  such row (0 rows differ).
- **Fix** (checked). Extend the segment only when the time really opens its comma clause. In `_segment`, before
  `b = min(...)` at 831:

  ```python
  opened = s0 + max([m.end() for m in re.finditer(r",|\s[-\u2013\u2014]+\s", low[s0:pos])], default=0)
  if not _content(low[opened:pos]):  # the time opens its comma clause: a fronted time
      b = min([c for c in cuts if c > b] or [e0])
  ```

  `r5rev/fixcheck5.py` applied this to `r5rev/mut` and restored it afterwards:
  - Tests: 89/89 pass.
  - Replay: unchanged (0 rows differ from 2d5a311).
  - Probes: 0 non-duration looser lines in `c_r5b`, `c_r5c`, `fz_state` and `fz_state_s2`.
  - "I joined a gym, and last week, I started yoga." still writes, as on 2d5a311.

  Add the first and fifth rows of the table as `state_start` tests.

### R5-2 (Medium by the brief's rule; Low by likely frequency; looser than 2d5a311) - `_RUNS_TO_NOW` exempts 5+-token "for" spans that 2d5a311 did not, including forward stays

- **Location:** `anchored_time.py:730-736` (`_QTY`, `_RUNS_TO_NOW`), used at 856.
- **Mechanism.** 2d5a311 allowed at most four tokens between "for" and the unit (`(?:[\w.'-]+\s+){0,4}?`). The
  d81901a pattern chains an optional qualifier (`about|almost|nearly|over|around|roughly|just over|more than|the
  past|quite`), a `_QTY` that may be "a couple of", "a good few" or "<n> and a half", and an optional
  `long|good`. Round 4's R4-4 fix removed only the `more` slot. Any qualifier plus "two and a half" or "a couple of"
  is now exempt from the state-start test, so the item is written.
- **Reproducers** (`c_r5c.py` X2; turn "My mom is staying with me <expr>.", fact "The user's mom stays with the user."
  (state), offset 17 days "after", speech 2023-06-15):

  | Expression | 2d5a311 | d81901a |
  |---|---|---|
  | for about two and a half weeks | state_start | written 2023-07-02 |
  | for almost two and a half weeks | state_start | written 2023-07-02 |
  | for more than a couple of weeks | state_start | written 2023-07-02 |
  | for over a couple of long weeks | state_start | written 2023-07-02 |
  | for about a couple of good weeks | state_start | written 2023-07-02 |
  | for nearly two and a half months | state_start | written 2023-07-02 |
  | for roughly two and a half days | state_start | written 2023-07-02 |
  | for just over two and a half weeks | state_start | written 2023-07-02 |
  | for the past two and a half weeks | state_start | written 2023-07-02 (backward phrase; a real resolver gives "before", so this row is a correct exemption) |

  The same holds on `c_r5b.py` R4 with "I'm staying with my sister ..." (4 rows).
- **Impact.** A forward stay ("staying with me for about two and a half weeks") gets a future `valid_at`. It happens
  only if the resolver emits `kind: state` with an "after" offset for such phrases. That is round-4 OQ2, still
  unmeasured. The replay has no such row.
- **Fix** (checked). Keep 2d5a311's token budget as a lookahead on the "for" branch:
  `^\s*for\s+(?=(?:[\w.'-]+\s+){{0,4}}?(?:years?|months?|weeks?|days?|decades?|hours?|while|ages|long\s+time)\b)(?:...`.
  With R5-1's fix, `fixcheck5.py` (FIX=both) gives:
  - Tests: 89/89 pass, including `test_durations_running_to_now_keep_the_write[for two and a half years]`.
  - Replay: 0 rows differ.
  - Probes: 0 lines looser than 2d5a311 in every r5 probe and fuzzer, `fz_since` included (`cmpfix.py fix2`).

  The fix also drops 237 `fz_state` writes that both trees made. Each of these is a fail-safe miss caused by
  d81901a's stricter state rules once the exemption no longer applies. One example is S829 "It began so yesterday,
  I started physical therapy for my back." The alternative fix is to never write a state's `valid_at` after
  `speech_date` in `plan_overlay`, which also closes RS5-4.

### R5-3 (Low; test quality) - Mutants of the new code that the test file does not kill

`r5rev/mutate5.py` ran on the private extract with `PYTHONPATH=src`. Without that variable, the venv's editable
install imports the clone instead and every mutant "survives". Baseline: 89 passed. The file was restored.

| Mutant | Result | Killing test |
|---|---|---|
| no `_AND` cut in the leading main clause | KILLED | `test_possession_and_ed_adjectives_are_not_perfect[I'm job hunting and I've updated my resume]` |
| leading main ignores `_SIDE_CLAUSE` | KILLED | same test, `[as I've said, I'm job hunting]` |
| **leading main = last non-side segment instead of first** | **SURVIVED** | none |
| trailing keeps subordinate segments | KILLED | `test_a_trailing_since_after_a_subordinate_perfect_is_not_the_fact` |
| trailing span = all of `sent[:a]` | KILLED | same |
| always leading / always trailing | KILLED | `test_a_date_right_after_since_is_a_since_phrase` / `test_since_with_a_perfect_main_clause_is_temporal` |
| no span check (any perfect in `rest`) | KILLED | `test_possession_and_ed_adjectives_are_not_perfect[as I've said, ...]` |
| **`opened` ignores `;`** | **SURVIVED** (equivalent; see R5-4) | none |
| **no `_REL` comma skip when building `rest`** | **SURVIVED** | none |
| start word only in `c_text` (round-4 code) | KILLED | `test_a_start_word_needs_to_be_about_the_fact[I started a new job and last week, ...]` |
| **start word only in the segment** | **SURVIVED** | none (the `c_text` half of 862 is unpinned) |
| **`_RUNS_TO_NOW` without the `long/good` slot** | **SURVIVED** | none |
| `_RUNS_TO_NOW` without "and a half" | KILLED | `test_durations_running_to_now_keep_the_write[for two and a half years]` |

No test covers the R5-1 or R5-2 shapes.

### R5-4 (Low; dead code / claim precision) - The `;` handling in `_perfect_in_main_clause` cannot run

- **Location:** `anchored_time.py:800` (`sent.rfind(";", 0, a)`) and 810 (`[^,;]+`), plus the `;` alternative of
  `_COORD` at 746.
- **Mechanism.** `_sentence_ends` (633-634) ends a sentence at every `[;\n]`, and `_sentence` (637-640) starts the
  next sentence after it. So `sent[:a]` never contains `;`, and these branches never fire. The mutant that drops the
  `;` lookup is equivalent (R5-3).
- **Effect.** "Since March, I'm vegan; I've lost weight." gives `causal_since` on both trees (`c_r5a` L13), because
  the perfect sits in the next "sentence". That is a safe outcome, but `;` coordination is handled by the sentence
  splitter, not by the code that looks like it handles `;`.
- **Fix.** Optional: remove the `;` branches, or note in the docstring that `;` is a sentence end.

### R5-5 (Low; fail-safe; CHANGELOG gap) - Correct temporal writes lost relative to 2d5a311 that the accepted-miss list does not name

Each row is written by 2d5a311 and given `causal_since` by d81901a (`c_r5a`; state, explicit "March", speech
2023-06-01, 2d5a311 writes 2023-03-16):

- "I've been vegan, since March." and "I've lived here, as you know, since March." The CHANGELOG says a trailing
  "since" attaches to "the last non-subordinate clause before it". But when "since" directly follows a comma, the
  words between `opened` and `a` (800-802) are an empty list, `all([])` is True, and the clause is read as a leading
  since-clause with no main clause after it.
- "Since March, 2019, I've been vegan." with expression "March". The first segment after the comma, " 2019", becomes
  the main clause. With the expression "March, 2019" both trees write.
- "I have since March, lived here." (T13).

The CHANGELOG lists as accepted misses "no-comma leading since-clauses", "'since March' with no perfect" and "which
started hurting". It does not list a comma before a trailing "since". The replay loses no write (0 rows differ), so
the cost is not measured. Fix: treat a since-clause as leading only when `opened == 0` or the text before `opened`
ends a clause the since-clause can attach to. At minimum, add these shapes to the accepted-miss list.

## Residuals (same date written on 2d5a311 and d81901a)

- **RS5-1 (Medium) - A causal leading "since" becomes temporal through a perfect in a clause that the span rules
  read as the main clause.** All rows are written 2023-03-04 on the state "The user is job hunting" (or the given
  fact) on both trees (`c_r5c` X5, `c_r5b` R5, `c_r5a` E). The item is `since the meeting on 3/4`, event-anchored,
  speech 2023-03-20.
  - "Since the meeting on 3/4 went badly, my boss has left, and the pay is low, I'm job hunting." The first segment
    after the comma is a continuation of the reason list.
  - "..., I think I've told you I'm job hunting."
  - "..., I'm calling a friend who has helped me." A relative clause with no comma.
  - "..., plus I've been stressed, I'm job hunting." Also "also": `_SIDE_CLAUSE` (750) has no "plus" or "also".
  - "..., after I've thought it over, ...". Also "until", "now that", "given what", "I mean", "to be honest",
    "whereas", "whether": none of these is in `_SIDE_CLAUSE`.
  - "..., since I've been stressed, I'm job hunting." A second since-clause.
  - "..., I'm job hunting - I've told my boss." / "... or I've left already." / "... yet I've stayed." `_AND` (752)
    cuts only and/but/so.
  - "I've said that since the meeting on 3/4 went badly, I'm job hunting." Here "that" closes the head, so
    `opened` sees non-lead words, and the head is read as a trailing since's clause.
  - "Since the meeting on 3/4 went badly, plus I've been stressed, I quit." (point_event "The user quit.") is
    written too.

  These are the K04.3 class: a causal "since" writes the reason's date on the fact.
- **RS5-2 (Medium) - Once "since" is judged temporal, the date goes on whatever fact the resolver attached it to.**
  `_start_reason` returns `None` straight after the since check (851-853) and never runs the state tests (`c_r5c`
  X4):
  - "Since last week I told you my sister is a nurse, I've worried." writes 2023-06-08 on "The user's sister is a
    nurse."
  - "I've been off caffeine since Monday, and my back is sore." writes 2023-06-12 on "back is sore" (round-4 F1).
  - "I've updated my CV and I'm job hunting since the meeting on 3/4 went badly." and "I'm job hunting because I've
    been stressed since the meeting on 3/4 went badly." write 2023-03-04 on "job hunting". The trailing branch
    (810-812) cuts only at commas, unlike the leading branch's `_AND` cut, so a coordinated or "because" perfect
    with no comma counts.
- **RS5-3 (Medium) - A relative or pronoun back-reference start never checks its antecedent.** `_BACK_REF_START`
  (741-743) alone satisfies 865. Each row writes 2023-06-08 on both trees (`c_r5c` X6, `c_r5b` R3):
  - "My sister moved in. It all started last week, and I'm tired." on "The user is tired."
  - "We hired a nanny, and that began last week, so I'm tired."
  - "I joined a gym, which started last week, and my back is sore." on "back is sore".
  - "This started with my sister last week, and I'm tired."
  - "My class, which started in Denver last week, is fun, and I'm tired."
- **RS5-4 (Medium) - `CUES["duration"]` exempts a state even when the expression is forward.** The exemption at 856
  is `CUES["duration"].search(expr) or (_RUNS_TO_NOW... and not _FROM_NOW...)`. `_FROM_NOW` guards only the second
  half, and `CUES["duration"]` (44-46) is an unanchored search. With an "after" offset, each of these writes
  2023-06-29 on both trees (`c_r5c` X3; speech 2023-06-15):
  - "for two weeks from now"
  - "for two weeks starting Monday"
  - "next week for two weeks"
  - "for two weeks next month"
  - "in a month for two weeks"
  - "for a few days from tomorrow"

  Round 4 recorded "for a few days starting Monday" and "for a week" (RS-6). These rows show that even an explicit
  "from now" passes. Fix: apply `_FROM_NOW` (and a "next|starting|from" check) to both halves, or never write a
  state's `valid_at` after `speech_date`.
- **RS5-5 (Low) - "since" at the end of a line.** "I'm job hunting since\nMarch, I've told you." writes 2023-03-16 on
  "The user is job hunting" on both trees (`c_r5a` L21). `\n` ends the sentence, so `at < s0`. `a` clamps to 0
  (799), the clause is read as a leading since-clause, and the main clause is "I've told you".

## Q1: why the since path cannot be looser than 2d5a311

2d5a311 returned `_PERFECT.search(sent[:a] + " " + sent[comma2 + 1:])`, where `comma2` is the first comma after `a`
that is not followed by a `_REL` word. d81901a searches
`rest = sent[:a] + " " * (end - a) + sent[end:]` with `end = comma2 + 1`, and counts a match only if it lies inside
`span`. This was checked by reading 788-820 against the 2d5a311 hunk (`git diff 2d5a311..d81901a`):

1. `sent` (blanked expression) and `comma2` are computed identically in both trees.
2. `span` lies inside `[0, a)` (trailing case: a segment of `sent[:a]`) or starts after the first comma after `a`
   (leading case). A match cannot start inside the blanked run, because every `_PERFECT` alternative starts with a
   letter or an apostrophe. So a counted match lies wholly in `[0, a)` or wholly in `sent[end:]`.
3. The two strings differ only in the number of spaces between `sent[:a]` and `sent[end:]` (one or more versus
   exactly one). Every `_PERFECT` construct that can reach across that gap is `\s+`, `\s*` or `$` inside a lookahead,
   and none of them depends on the count. `\b` at a match start sees a space in both. So any match counted in `rest`
   matches at the corresponding position of 2d5a311's string.
4. d81901a's `_PERFECT` is a sub-language of 2d5a311's. `_IRREGULAR` and `_AFTER_VERB` are subsets, the adverbs
   `really|finally|actually|definitely|recently` were covered by 2d5a311's `[a-z]+ly`, and the -ed lookahead dropped
   ",".
5. `finditer` can let a straddling, uncounted match hide an in-span match ("I have, since ..., had been"). That only
   causes misses.

Hence, if d81901a calls a since temporal, 2d5a311 does too. "ever since" is unchanged. The occurrence loop takes the
strictest result, and the cap (896-899) only returns `causal_since` or `state_start` where per-occurrence judging
could return less. Empirically: 0 looser lines over 72,115 since-path lines.

The brief's specific risks:

| Risk | Probe | Result |
|---|---|---|
| leading vs trailing span selection | c_r5a L1-L27, T1-T18 | 0 looser; stricter on L9-L11, L15, L18, L19, T2, T3, T5-T7, T9, T13 |
| `opened`/`_LEAD` | L3 So, L4 Well, L5 ", and since", L6 ", since", L22 "Actually,", L23 "Also" | same on both (written) |
| blanking by spaces (lookahead) | T14 "I've practiced since March." / T15 "I have mixed since March, feelings." | same on both (written / causal) |
| span bounds / straddle | T11 "I have, since March, been vegan." / T12 no comma | causal on both |
| `;` | L13 | causal on both (R5-4) |
| multiple commas | L18, L19, E4 / X5-1 | L18, L19 stricter (R5-5); E4 written on both (RS5-1) |
| leading, no comma | L2 "Since March I've been vegan.", L26 | causal on both |
| expression spanning the clause | L19b "March, 2019", L19c "since March, 2019" | written 2019-03-16 on both |
| newline | L20, L21 | same on both (RS5-5) |

## Round-4 closure (Q2)

| Round-4 item | Inputs re-run | 2d5a311 | d81901a | Closed? |
|---|---|---|---|---|
| R4-1 | A1, A2, A4, A9, A11 (`c_r5b` R1) | causal_since | causal_since | yes |
| R4-1 variants | R1-v1 "..., I've fallen behind", R1-v2, R1-v4, R1-v6 | written | written (same date) | residual RS5-2 |
| R4-5 | every `c_r5a` row, bare vs "since" expression | - | identical pairs | yes (rule removed) |
| R4-2 | C1, C2, C3 | state_start | state_start | yes |
| R4-2 variant | start word in both coordinates (R2-v1-v4, v10, v11; X1) | state_start | written | **no: R5-1** |
| R4-3 | D1, D2, D3, D5 | state_start | state_start | yes |
| R4-3 pre-existing | which/that/it all + bare start (X6) | written | written | residual RS5-3 |
| RS4-1 | like / so / because / coordinated head, and-no-comma tail, 'cause, then; side as/like/and/if/once/though/even though/because | written | causal_since | yes |
| RS4-1 variants | plus/also/yet/or/dash/after/until/now that/given/I mean/to be honest/whereas/whether/since/who/that-head | written | written | residual RS5-1 |
| R4-4 | for two more weeks / a couple more / a few more / one more / an extra / a further / two whole | written | state_start | yes |
| R4-4 long forms | about/almost/more than + "two and a half"/"a couple of" | state_start | written | **no: R5-2** |
| R4-6, R4-7 | not in this delta (cap code and `event_time.py` unchanged since c0dbdb0) | - | - | not re-checked |

## Open questions

- OQ1: How often the resolver attaches the bare fronted time in "X started ... and <time>, Y started ..." to a state
  in Y's clause (R5-1). The replay has no such row. To settle it, run a live sample with R5-1's fix as a mutant and
  count `written -> state_start` changes.
- OQ2 (carried from round 4): whether the resolver emits `kind: state` with an "after" offset for "for about two and
  a half weeks" or "for two weeks from now" (R5-2, RS5-4). This needs a paid call, so it was not checked.

## Coverage table

Scope from `git diff --numstat c0dbdb0..d81901a -- <3 paths>`: 3 files, 93 insertions, 39 deletions
(1+1, 41+27, 51+11). Current line counts (`wc -l` at d81901a): 1147 + 609 + 100 = 1856.

| File | Lines | Status |
|---|---|---|
| src/menhir/infrastructure/anchored_time.py | 1147 | read-deep 596-910: helpers, `_sentence`, `clause_guard`, every regex 710-757, `_since_operator`, `_perfect_in_main_clause`, `_segment`, `_occurrences`, `_start_reason`, `state_start_guard`; 14-49 (`CUES`); rest unchanged in this delta, not re-read |
| tests/test_anchored_time_state_start.py | 609 | delta hunks read (182-188, 330-334, 377-381, 398-457); mutation-tested (R5-3); rest not re-read this round |
| CHANGELOG.md | 100 | delta hunk read (line 3); 0 non-ASCII characters in the 96 added diff lines (Python scan of `r5rev/delta.diff`) |

Rows: 3, which equals the 3 enumerated files.

## Verification notes

- Every probe ran through the clone's `.venv` Python without `-I`. `h.py` puts `<root>/src` first on `sys.path`. A
  check (`r5rev/whichmod.py ../r3rev/d5`) printed `r3rev/d5/src/menhir/infrastructure/anchored_time.py`, so each tree
  loads its own module. Production order: `clause_guard`, then `state_start_guard`, then `plan_overlay`, with the
  Graphiti baseline equal to the speech date.
- The venv has menhir installed in editable mode from the clone. Pytest on a copied tree therefore needs
  `PYTHONPATH=src`. The first mutation run lacked it and reported every mutant as surviving; that run was discarded
  and repeated.
- Candidate fixes and mutants were applied only to `r5rev/mut` (a git-archive of d81901a) and restored in `finally`.
- The fuzzers combine fixed clause pools at random. They are built to find rule differences, not to resemble real
  traffic, so their counts measure how many shapes trigger a difference, not how often it happens.
- Not checked: live resolver behaviour (OQ1, OQ2); `event_time.py` and the occurrence cap (outside this delta);
  Neo4j persistence.

## Review confidence: 84/100

- Q1 is answered by a proof for the since path, backed by 72k random since-path turns and 115 targeted lines, and
  by reproduced counterexamples for the state path.
- Every finding has outputs from both trees.
- Both proposed fixes were run against the tests, the replay and every probe.

Confidence is held back for three reasons:

- The fuzzers explore only the clause pools I wrote, so other state-path shapes that are looser than 2d5a311 could
  exist outside them.
- The severity of R5-1 and R5-2 depends on resolver behaviour that cannot be measured offline.
- The rest of the test file and the unchanged parts of `anchored_time.py` were not re-read this round.
