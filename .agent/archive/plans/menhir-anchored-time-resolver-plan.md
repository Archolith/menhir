---
artifact_schema: 1
artifact_uuid: 3885d557-564d-4a83-a0cf-47f632a66218
artifact_type: plan
artifact_status: IMPLEMENTED
---

# Menhir: anchored-time (Reichenbach) resolver

> Moved from the maintainer workspace on 2026-11-07 with local paths and host details removed. Some cited reviews and plans were not moved; those names are historical.

Disposition: IMPLEMENTED (P0-P3; P4 cross-session anchors deferred and tracked in Archolith/menhir#368) (was: ACTIVE, partial)
Last verified: 2026-10-21 - DONE for P0-P3 (medium); anchors checked: PRs 244/245/247 merged (P1-P3), no anchored-time consolidation module on main (P4). The integration-point decision (intercept vs post-episode) was not checked.
Code baseline: menhir 29bcbd9 (smoke/gpt6-flex), graphiti_core as then installed.

## Problem

A fact's event time is often stated relative to something else: the conversation date
("a month ago") or another event ("a week after I bought the charger", "booked three
months in advance of the trip"). Menhir loses most of that:

1. Event time is computed by Graphiti's batch timestamp call, which sees only the
   paraphrased `fact` plus the episode date, never the source text
   (graphiti_core combined_extraction.py:234-276, extract_edges.py:279-300). If the
   paraphrase drops the phrase, the time is gone.
2. A defaulted date ("ongoing -> REFERENCE TIME", extract_edges.py:287) is stored exactly
   like a resolved one. Recall renders both as "happened <date>"
   (context_builder.py:168-186). No basis, granularity, or original phrase is kept.
3. No anchor representation: no field, no BEFORE/AFTER relation; Graphiti's rule 7
   (extract_edges.py:161) tells the model not to infer from other events.
4. Planned/future times ("next month") are not stored; valid_at = mention date.
5. Any edge given `invalid_at` is expired at ingest (edge_operations.py:822), so a
   finished past event ("trip Mar 27 - Apr 1") is treated as a superseded belief and
   hidden by current-belief recall (recall_policies.py:215).
6. Timeline order uses valid_at (memory_queries.py:638), so defaults mis-order events.

Evidence (b10, 25 LME items, 1346 edges; read-only): 74% of edges carry the session
date, 18% null. Facts whose text keeps a precise past cue resolve 40/47; event-anchored
facts resolve 4/17 (only when the anchor is a calendar date). 16 of 75 user turns with
a precise past cue got no shifted edge (cue paraphrased away, timestamp pass missed, or
the dated event was never extracted). All 8 edges with invalid_at were expired at ingest.

## Design

Two stages, replacing (not adding to) the starved timestamp call.

1. **Detect (deterministic, cheap).** A cue detector over the raw source turn, built from
   a general taxonomy (TimeML/TIMEX3-style), not from any benchmark's phrasing:
   absolute date; relative to speech time (ago / last / next / yesterday / this past);
   anchored to another event (before / after / since / prior to / in advance / N units
   later); duration ("for two weeks"); frequency ("every Tuesday"); vague ("recently",
   "a while back"). Turns without cues keep today's path.
2. **Resolve (LLM, own prompt).** Input: source turn, speech time, and that turn's
   extracted facts. Output per fact:
   - `expression`: the verbatim time phrase (or null),
   - `basis`: explicit_date | speech_relative | event_anchored | default_speech_time | unknown,
   - `offset` (signed amount + unit) and `granularity` (day/week/month/year/season),
   - `anchor`: index of the anchor fact in this turn, or a short description of an
     unlisted anchor event,
   - `kind`: point_event | state | plan,
   - `planned_time` for plans.
   Optionally `missing_events`: dated events in the turn with no fact (the
   "never extracted" class).
3. **Compute (deterministic).** Code turns basis+offset+granularity into a date window;
   the LLM never does calendar arithmetic. Same-turn anchors resolve immediately;
   cross-turn/session anchors resolve at consolidation by graph lookup, else stay
   `event_anchored` with an unresolved anchor (not a fake date).

### Storage (RELATES_TO edge props, additive)
`time_expression`, `time_basis`, `time_granularity`, `time_window_start/end`,
`time_anchor_ref`, `time_anchor_offset`, `event_kind`, `planned_time`.
Existing `valid_at` stays the window start for compatibility.

### Integration point (open decision)
- (A) Intercept Graphiti's `extract_edges.extract_timestamps_batch` via Menhir's
  existing per-call LLM proxy (graphiti_extraction_policy.py `_PayloadSanitizingLLMClient`):
  times are right BEFORE Graphiti's dedupe/contradiction logic runs. Needs source text
  plumbed into the proxy; couples to Graphiti prompt names.
- (B) Menhir post-episode step rewrites times after save: decoupled, but Graphiti's
  invalidation already ran on wrong times.
Prefer A; decide after P0.

### Recall / timeline
- Render by basis: "happened ~2023-04 (from 'a month ago')", "date unknown (said
  'recently')"; never show a defaulted date as an occurrence date.
- Timeline orders by window, ties/unknowns flagged rather than placed at speech time.

### Expiry
`invalid_at` on a `point_event` or bounded past `state` is world-time end, not belief
supersession: do not set `expired_at` for it. Only contradiction sets `expired_at`.

## Not fitting to a benchmark

- Detector and prompt are written from the taxonomy above, with invented examples from
  varied domains (work/software, health, family, travel, finance). No benchmark phrases.
- Prompt iteration happens on a hand-written dev set only (max 3 iterations in P0).
- LME b10 is held out: gold labeled blind before any model run, scored once with the
  frozen prompt. A large dev-vs-held-out gap is reported as a generalization problem,
  not fixed by editing the prompt toward held-out cases.
- Precision is measured: no-cue facts must not gain invented dates.

## Phases

- P0 offline replay (local prototype, no Menhir edits): dev + held-out scores vs
  current-graph baseline; detector recall on a random turn sample.
- P1 resolver + deterministic calendar in Menhir behind a flag; integration A or B.
- P2 storage fields, recall rendering, timeline ordering.
- P3 expiry rule change (point events / bounded states).
- P4 cross-session anchor resolution at consolidation.
Each phase: focused tests, then one LME re-ingest only when P1-P3 land.

## Acceptance (P0 -> go/no-go for P1)
- Held-out: date-within-window rate on cue facts clearly above baseline
  (baseline from the current graph), basis accuracy reported.
- False-date rate on no-cue facts <= baseline.
- Dev vs held-out gap reported.

## P0 results (2026-10-06, one pass, small n)

Local prototype (reich_lib.py prompt+calendar, run_replay.py).
gpt-6-luna flex, total cost $0.013. Held-out gold labeled blind before any model run;
prompt frozen after dev iteration 2 (reich_lib sha256 7c8ed779...), held-out run once.

| set | basis acc | date in window (resolver / baseline) | false date on no-date facts (resolver / baseline) |
|---|---|---|---|
| dev it1 (13 turns, hand-written) | 33/36 | 18/28 / n/a | 0/8 / n/a |
| dev it2 | 36/36 | 28/28 / n/a | 0/8 / n/a |
| held-out (30 b10 turns, 109 facts) | 96/105 | **29/32 / 18/32** | **2/73 / 0/73** |

Held-out dated facts by class (resolver / baseline): speech_relative 12/12 vs 6/12,
event_anchored 6/8 vs 2/8, explicit_date 11/12 vs 10/12.
Detector (frozen, blind 40-turn random sample): fires on 19/20 precise-time turns,
2/3 vague-only, 3/17 no-time turns (causal "since", "afterwards").

Dev it1 -> it2 changes (all from dev failures, generic): month/day-of-month without
year (`--MM`, `---DD`), bare weekday by tense (`past`/`upcoming`), `quarter` unit,
season `count` ("two summers ago"), recurring schedules -> `none`.

Acceptance: date accuracy clearly above baseline (met). False-date rate <= baseline
(NOT met: 2/73 vs 0/73). Dev 100% vs held-out 91% is the expected overfit gap on a
13-turn dev set. Baseline's 0 false dates is partly an artifact: it stamps the speech
date on everything, which the false-date metric does not count; its date-default problem
is the 18/32.

Held-out errors to address in P1 (on a fresh dev set, not by editing toward these):
- Causal "since" read as temporal: a time from another event borrowed onto an
  interest fact (false date).
- Time of a requested topic ("events in the next few months") attached to the
  request fact (false date).
- Time already in the fact text (from another turn) ignored when the turn only says
  "recently": regression vs baseline (1 fact). Resolver must keep fact-text dates.
- "later that week" relative to an anchor: no anchored-calendar form; open interval
  only.
- Anchor not in the fact list ("when she borrowed it") stays unresolved, by design.
- Detector: durations ("subscribed for about six months now") not implemented though
  listed in the taxonomy.
- missing_events: 8 reported on held-out, ~5 real (unextracted dated events), unscored.

### Round 2 (same day): fixes for the five items above

Changes, all derived from a NEW hand-written dev set (dev_set2.json, 12 turns, built to
exercise the rules with controls, e.g. temporal vs causal "since", plan vs request):
causal "since"/"as" is not time; requests/wishes/interests get `none`, own plans keep the
time; times stated in the fact text win; durations-to-now place a state's start;
anchor-relative `calendar` with `later` ("later that week", "the following month");
fixed-date holidays = explicit; detector `duration` class; parser tolerates any dash
prefix on MM-DD. Frozen at reich_lib sha a7797891. New held-out = 30 fresh b10 cue turns
(103 facts) excluding every round-1 turn, gold labeled blind after the freeze.

| set | basis acc | date in window (resolver / baseline) | false date (resolver / baseline) |
|---|---|---|---|
| dev (orig) regression | 36/36 | 28/28 | 0/8 |
| dev2 it2 | 34/35 | 22/22 | 0/13 |
| **held-out 2 (blind, once)** | 91/99 | **22/23 / 12/23** | **6/76 / 0/76** |
| held-out 1 rerun (NOT blind: its errors drove the fixes) | 98/105 | 31/32 / 18/32 | 0/73 / 0/73 |

Detector, new blind 40-turn sample: precise 8/10, vague 3/7, false fire 1/23. Misses:
an age ("computer is around 5 years old") and "back in mid-February" (no early/mid/late
month form). Round-1 sample with the duration class: 20/20.

Held-out 2 false dates (6): two clearly wrong, two debatable, one gold judgment.
- Time borrowed across a list (2): "finished three novels ... the last one last
  weekend" put last weekend on all three. The rule against borrowing exists; the model
  still distributes a list-final time.
- Model year in a product name read as the fact's time ("owns a 2018 <car>") (1).
- Plan time spread onto attribute facts of the planned thing ("recipe would feature
  peppers" got "this weekend") (2), debatable.
- A state started by a dated event got that event's window ("goats safe behind the
  repaired fence" -> three weeks ago) (1). That is the state's start, consistent with
  how duration states are scored, so arguably correct; gold kept as labeled.
Acceptance still NOT met on false dates (6/76 vs 0/76; about 3/76 clearly wrong). The
date gain holds on fresh data (22/23 vs 12/23). Prompt not edited after held-out 2.

Next round (fresh dev + fresh held-out again; b10 has ~85 unused cue turns left):
a deterministic post-check that the attached `expression` is in the same clause as the
fact's event (list distribution), a product/model-year guard, attribute facts inherit no
time, early/mid/late month and age expressions in the detector. Consider moving
held-out sampling to a second non-LME corpus, since b10's pool is nearly used up.

### Round 3: deterministic list-distribution guard (post-check only)

Done: the first item only. Model-year guard, attribute rule and detector forms are still open.
`clause_guard.py` (frozen sha ec4472e9) runs on resolver output before compute; it can only
remove a time. Rule: when one expression is attached to 2+ facts and is not sentence-initial,
a fact loses it only if the fact's distinctive words occur earlier in the same sentence and
nowhere from the expression's clause onward. That only happens when another attached fact
owns the clause. The clause is split on commas, dashes and relative pronouns; a relative
clause includes its antecedent. A time does not scope backward over earlier list members,
but it does carry forward to elaborations ("on May 1st, where I danced for 6 hours").
Kept: fact text containing the expression; the anchor event named inside the expression.
Prompt unchanged (reich_lib a7797891), so earlier outputs were rescored from cache.

The first version (distinctive overlap with the expression's own clause only) was wrong. On
the old held-outs it dropped 4 correct dates (elaborations, an anchor fact) and missed the
target: an empty segment between ", which" lost the antecedent. Both old held-outs were
already non-blind, so they served as guard dev data. Offline checks are in `check_guard.py`.

| set | false date: no guard / guard | date in window: no guard / guard | guard drops |
|---|---|---|---|
| dev, dev2 (cached) | 0/8, 0/13 / same | 28/28, 22/22 / same | none |
| dev3 (8 new turns: list-final times + controls) | 0/8 / 0/8 | 14/15 / 14/15 | none (model did not spread) |
| held-out 1 (cached, non-blind) | 0/73 / 0/73 | 31/32 / 31/32 | none |
| held-out 2 (cached, non-blind) | 6/76 / **4/76** | 22/23 / 22/23 | G21 two list members (the target) |
| **held-out 3 (29 fresh b10 turns, blind, once)** | 0/93 / 0/93 (baseline 0/93) | 26/28 / 26/28 (baseline 18/28) | none |

Held-out 3 also: basis 120/121. It has a legitimate list spread ("finished three novels last
weekend - A, B, and C"), which the guard correctly kept. Misses: the fact text says "last
month" but the turn's "on the 15th" was used (fact-text rule not followed; 1); "just started"
seeds from a dated arrival left vague (1); "currently" given the speech date (basis only).
Spend: dev3 $0.0014 + held-out 3 $0.0068; P0 total about $0.045.

Read: on fresh data the guard changes nothing. The list-final pattern is rare (1 of 89
held-out turns), and it removes the one observed case with no lost dates across all sets.
Evidence for benefit is n=1; evidence for no harm is 5 sets. False-date acceptance is met on
held-out 3 (0/93) and not on held-out 2 (4/76: model year 1, attribute facts 2, state start 1).
Remaining open items: model-year guard, attribute facts, detector early/mid/late month and
age, fact-text precedence over a turn date.

Regression check: did round 2 cause held-out 2's false dates? The round-1 lib was rebuilt
from the transcript by replaying every edit; the replay reproduces both sha 7c8ed779 and the
current a7797891. Running it once on held-out 2 ($0.0074): false dates 2/76 (both G15
attribute facts, which both prompts get), dates 21/23, basis 89/99. Round-2 prompt on the
same set: 6/76, 22/23, 91/99. The 4 extra false dates appear only with the round-2 prompt:
- G28.0 model year: plausibly the new rule "a time in the FACT text wins" (fact says "owns a
  2018 ...").
- G17.1 state start: plausibly the new "duration places a state's start" rule.
- G21.4/5 list spread: round 1 said vague "just"; no single rule explains it; the guard
  removes it.
One run per prompt, so sampling noise is not separated from the prompt effect. Round 2
removed held-out 1's 2 false dates and added 2 dates there, but held-out 1 was not blind for it.

Second run of each prompt on held-out 2 (new tags r1prompt_b / frozen_b, $0.0143):

| prompt | false dates run1 / run2 | dates run1 / run2 | basis run1 / run2 |
|---|---|---|---|
| round 1 (7c8ed779) | 2/76 / 2/76 | 21/23 / 20/23 | 89/99 / 87/99 |
| round 2 (a7797891) | 6/76 / 2/76 | 22/23 / 21/23 | 91/99 / 94/99 |

The 4 disputed false dates (G28.0, G17.1, G21.4/5) did not recur in round 2's second run.
The only stable false dates are G15.1/2, in all 4 runs of both prompts. So there is no
evidence that round 2 introduced them: they are run-to-run noise at this n. No rollback.
Round 2 is equal or slightly better on dates and basis. Consequence for the method:
single-run differences of a few facts are within noise. Future go/no-go comparisons should
use 2-3 runs per prompt (or per-fact recurrence), not one. Next held-out needs a non-LME corpus (b10 has
~47 unused cue turns, few durations).
