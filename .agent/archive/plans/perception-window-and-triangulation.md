# Plan: the two non-knob perception levers — σ WINDOW + broadened triangulation

> **Status note 2026-08-08 (curator audit, corrected).** The line below and the "Sequencing &
> scope" §4 were stale — Lever C (C1 heterogeneous keying + C2 cross-episode dedup) is BUILT
> (commits `2bad574`, `8353df9`; `dedup_events`/`Event.category` confirmed in
> `src/menhir/domain/fold_algebra.py`), per this doc's own "RESULT — C2 + C1 BUILT 2026-07-03"
> section below. **Correction to an earlier version of this note:** the "semantic event
> coreference" gap this doc calls out as deferred is ALSO built — Lever C3 (commit `f933115`,
> same day, never folded back into this doc): `fold_algebra.coreference_candidates` +
> `perception.resolve_coreference` (determinism finds the ambiguous cluster, an LLM judge
> resolves it, confidence-gated), both confirmed present in current `src/menhir`. All of Lever C
> (C1/C2/C3) is built; nothing in this plan is outstanding. Candidate for archival — not archived
> here since that wasn't the scope of this pass.

**Status: Levers A + B + Law-3 + A6 all DONE 2026-07-03. Lever C (heterogeneous keying + cross-episode
event dedup) PLANNED — surfaced by the full-graph `perception-lme` consolidation (bike case $185).**
The two structural gaps the step-5 live tuning left (see
`archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md` → "Perception gate tuning" +
"Move-1 restore"). Both are deterministic fold/perception features, NOT threshold tuning. Builds land
in **menhir-frontier**; measurement uses the existing `perception_tune.py` harness (dataset+LLM, no
graph). One project per session.

## Why these two (the D0 residuals, grounded)

After the count-floor + move-1 work, the counting slice has exactly two residual failures at the
precision-conservative threshold (1.0), and neither is a knob:

| qid | gold | what the gate does now | the missing machinery |
|---|---|---|---|
| `3a704032` | 3 plants | **abstains** (count-floor drops the split count=1 measures) | **σ WINDOW** — "acquired in the last month" over dated acquisition events, aggregate-keyed |
| `gpt4_d84a3211` | $185 | **commits WRONG** — unanimous `bike_spend`=225 (SUM bias, agreement 1.0) | **broadened triangulation** — an independent re-derivation to veto the confident-wrong write |

The count-floor already makes `3a704032` *safe* (abstain, not wrong); Lever A is a **recall** gain.
`gpt4_d84a3211` is a *dangerous* wrong write; Lever B is a **precision** gain. **Do Lever B first**
(stops a dangerous write, smaller) then Lever A (larger, write+read halves).

## Shared invariant (governs both)

> **Perception may be probabilistic. Folds and Views must stay deterministic.** No probability crosses
> into ρ/δ/Views. New signals (model-stated totals, window bounds) are GATE INPUTS or READ-TIME δ —
> never stored on a View, never a ranking signal, never the committed value.

## Shared measurement

`archolith-bench/scripts/longmemeval/analysis/perception_tune.py`, before/after at threshold 1.0.
Per-lever target is a specific qid transition at **zero precision cost on the 12 held-out namespaces**.
Live sweeps cost gpt-4o-mini calls — **stop on 429 per protocol**. Unit tests need no graph/LLM (fakes).

---

# Lever B — broadened triangulation (do first)

**Case:** `gpt4_d84a3211` gold $185. Perception unanimously sums dated bike purchases to **225**
(agreement 1.0). No *user*-stated total exists, so the current triangulation veto (items vs
`stated_total`) never fires; self-consistency can't help — it's confident **bias**, not variance.

**Principle:** triangulation = require **two independent derivations of the same scalar to agree**.
Today the second derivation is a rare USER-stated total. Generalize it: when none exists, obtain a
**model-stated total by a DIFFERENT method** (a holistic "what is the total X?" ask) as an independent
cross-check. Agreement → keep; disagreement → **abstain**. The itemized SUM and a holistic estimate
are independent error channels — a double-count/spurious-item that inflates the itemized path (225)
won't be reproduced by the holistic ask, so they disagree and we refuse the write.

**Honest success metric:** Lever B turns **WRONG → ABSTAIN** (removes the dangerous write), NOT
wrong → correct. Committing $185 needs *accurate itemization* (a perception-accuracy problem);
triangulation's job is only to STOP the confident-wrong 225. Frame the target as `gpt4_d84a3211`:
commit(225) → abstain, held-out unchanged.

### Anchors
- `perception.gate` — add veto-4 after triangulation (`perception.py`, the veto chain ~L360).
- `perception.extract_stated_total(...)` — NEW holistic second-derivation extractor (sibling of the
  move-1 assertion path; it is essentially the move-1 stated-total detector run as a cross-check).
- `PerceivedGroup.stated_total` / `stated_event` — the existing triangulation slot Lever B extends.

### Build phases
- **B1 — DONE (2026-07-03).** `extract_stated_total(episodes, measure, llm_complete) -> float | None`
  in `perception.py` (§1b): one holistic, query-blind call (`STATED_TOTAL_PROMPT`, measure
  interpolated), deterministic `{"total": <number|null>}` parse via `_parse_json_object`. `null`/
  unparseable → `None` (= "no cross-check", never `0.0`). 3 unit tests (parse, null-is-none, measure
  interpolated).
- **B2 — DONE (2026-07-03).** `gate` gained `cross_check: Callable[[str], float|None] | None`; a new
  **veto-4** runs on the commit path only (after veto-3): for a move-2 fold group with **no** user
  `stated_total`, `cross_check(measure)` supplies a total and `abs(value - cross) <= triangulation_tol`
  or the write is vetoed. On agreement the commit records `view_audit_triangulated=True` (a
  deterministic corroboration bool); the model-stated total itself is a gate input only, held in-memory
  on `GateDecision.cross_total` and **never persisted on the View** (shared invariant / anti-goal).
  **Abstain-only** (runs post-commit-path, never rescues); `None` cross-check or `None` total → no veto
  (precision unchanged). `perceive_and_fold` gained `cross_check` passthrough + `enable_cross_check`
  (builds the default holistic closure). 5 unit tests: 225-vs-185 → abstain; agreement → commit;
  None → commit; user-total present → cross-check not consulted; scattered → not consulted (no rescue);
  end-to-end abstain. **Full perception suite 24/24 green** (was 12); fold_algebra + view_repository_lww
  still green.
- **B3 (measure) — RUN 2026-07-03 (live, `perception-tune-leverb.json`, 687s, no 429). CONFIRMED,
  with one twist and one new veto:**
  - **Veto-4 corroborated every committed fold**: at threshold 1.0 every surviving SUM/DISTINCT commit
    carries an AGREEING cross_total (luxury_spend 2500↔2500, budget 20↔20, car_accessories 3↔3).
  - **The 225 case confirmed by direct probe** (it didn't reproduce in-sweep — sampling variance; the
    extractor emitted `bike_mileage=347 'stated'` instead): the real holistic cross-check for
    `bike_spend` returns **65**, and replaying the recorded unanimous SUM=225 through `gate` with it →
    **ABSTAINED** ("cross-check failed: sum=225.0 vs holistic 65.0"). Both derivations are wrong vs
    gold 185 — exactly why the honest metric is wrong→ABSTAIN, never wrong→correct: disagreement
    between independent channels refuses the write.
  - **New finding → stated-floor**: the wrongs migrated to the `stated` path (exempt from floor +
    cross-check). Live case: `fish_tanks_owned=1 'stated'` FABRICATED from the "1" in "1-gallon tank"
    (a unit misread as a stated count), unanimous across samples — bias again. Fix: the count-floor
    now also floors `stated` values `< min_count` (a stated total of 1 adds nothing over the raw
    episode; FP≫FN). Rescore: wrong 2→1, FP unchanged, recovered cases untouched (playlists=20,
    mileage=347 clear the floor).
  - **Net at threshold 1.0 after stated-floor: ZERO fabricated/wrong-state Views across all 26
    namespaces.** The residual "wrong" (`bike_mileage=347`) is genuinely user-stated (verified in the
    turns) — a TRUE fact scored against a different measure's gold, the same true-but-irrelevant
    category as the two held-out survivors. Measure-relevance is a recall/routing concern, not a
    perception-precision one. **Lever B is DONE.**

### Anti-goals
- Model-stated total is NEVER stored / NEVER ranks / NEVER the committed value — gate input only.
- No fitted weighting; strict conjunctive veto. Abstain-only (no rescue).

---

# Lever A — σ WINDOW: windowed acquisition counts as a read-time δ over a timeline View

**Case:** `3a704032` gold 3. Three *acquisitions* (peace lily + succulent "two weeks ago", snake
plant "from my sister last month") among owned-but-not-acquired distractors (fern, spider, basil),
answering "how many plants did I **acquire in the last month**?" (relative to `question_date`).

**The binding invariant (from the fold-algebra design):** **relative windows must NEVER be
materialized into a View** — "last month" changes meaning as time passes. So the write-time artifact
is a **lossless, dated, aggregate-keyed TIMELINE**; the window is a **read-time δ** (resolve
relative→absolute at query time, σ `window`, ρ `count`/`distinct_count`). This is exactly the
fold-algebra doc's "LIST is the escape hatch: an unanticipated query is a read-time δ over the
timeline payload." Reuses `TimelineKind` + `fold_algebra.window`/`timeline` — no new node type.

### Anchors
- `fold_algebra.window(events, since, until)` (σ SHAPE, exists) + `count`/`distinct_count` (ρ, exist).
- `ViewRepository.TimelineKind` / `record_timeline` / `fetch_timeline` (`view_repository.py:144/410/426`).
- `perception.extract_once` + `_KIND_REDUCER` + `SYSTEM_PROMPT` (acquisition kind + aggregate keying).
- NEW `services/windowed_fold.py` (read-time δ) + a small relative-window resolver.

### Design — two halves
**WRITE (perception → timeline View):**
- **A1 acquisition-event detection + aggregate keying.** Perception emits `kind=acquire` for dated
  ACQUISITIONS ("bought", "got", "from <source>") — distinct from mere ownership/mention — and keys
  them to ONE category measure (`plants_acquired`), with the specific item as `identity`
  (peace lily / succulent / snake plant). This is the "keying belongs to perception" fix: category in
  `measure`, item in `identity`. It also fixes the count-of-1 split (3 identities under one measure,
  not 3 measures of 1). Prompt + `extract_once` kind handling.
- **A2 acquisition timeline sink.** Fold acquisition events → `record_timeline` (each a dated entry
  `{when, identity, episode_uuid}`). Lossless, supersedable, **no window baked in**. A
  `event_fold`-sibling path (`fold_events_to_timeline`) or a `perceive_and_fold` timeline branch.

**READ (windowed δ):**
- **A3 `windowed_fold.count_in_window(entries, since, until, distinct=True) -> int`** — pure: σ
  `window` then `distinct_count`/`count`. Reuses `fold_algebra`. Deterministic, unit-testable.
- **A4 relative-window resolver** — "last month"/"this year" → absolute `[since, until]` against a
  **reference date** (the QUERY time, not now-at-write). For the D0 eval, reference = `question_date`.
- **A5 (measure)** extend `perception_tune` (or a sibling) for the acquisition qids: build the timeline
  then `count_in_window` with `question_date`-resolved bounds; target `3a704032` → 3, distractors
  excluded (out of window / not acquisitions).

**A6 query-intent routing — CAPABILITY BUILT + live-proven 2026-07-03 (ranked-injection deferred).**
`services/windowed_recall.py`: `detect_windowed_count(query)` parses "how many <noun> ... <window>?"
intent; `answer_windowed_count(views, namespace, query, reference_date)` matches the noun to a timeline
View's subject (token overlap, plural-tolerant), resolves the window vs the query date, and returns the
deterministic windowed count — or None (not a count / no matching timeline) so the caller falls through
to normal recall. 5 unit tests (fake repo). **Live end-to-end against the graph**: wrote the plants
acquisition timeline into `lme-3a704032`, then the ACTUAL question "How many plants did I acquire in the
last month?" → query → graph timeline → windowed δ → **3 (gold)**. What remains deferred is only the
PRODUCT decision of whether/how to INJECT a synthesized count into `RecallService.recall`'s ranked
results (should a computed answer out-rank retrieved nodes?) — a result-shape/ranking choice, not
missing machinery. Callers invoke `answer_windowed_count` and present the answer as they see fit.

### Laws / anti-goals
- NEVER materialize a relative window (write the timeline; window at read).
- Timeline is the LIST monoid — lossless, dedup by `(when, identity)`; a windowed count is δ over it.
- Aggregate keying is perception's decision, not a runtime GROUP BY / stream engine.
- `distinct_count` over the window reuses the embedding-dedup identity guard (§3 of the perception
  boundary) so "5-gallon tank" / "the 5 gallon one" collapse.

### RESULT — Lever A A1–A5 BUILT + live-verified 2026-07-03 (`3a704032` → 3)

Built end-to-end and unit-tested (no graph/LLM): **A1** prompt+`extract_once` emit `kind=acquire`
(date in `when`, item in `identity`, CATEGORY in `measure` — aggregate keying); **A2**
`event_fold.fold_events_to_timeline` (coerces item→`what`, folds via `fold_algebra.timeline`, upserts
a `TimelineKind` View — no window baked in); **A3** `windowed_fold.count_in_window` (σ `window` +
distinct/`count`, pure); **A4** `windowed_fold.resolve_window` (relative phrase → absolute bounds vs
the QUERY date; rolling default + `calendar=True` sense). Robustness: `fold_algebra._parse` now
tolerates slash dates (LME/LLM echo `2023/05/07`) so events never silently drop from a window.
54 unit tests green (perception 26 + windowed 9 + fold + LWW + counter-embedding).

Live (bench `analysis/acquisition_window.py`, real gpt-4o-mini k=5, dataset-only):
```
3a704032  gold 3  "last month"   acquisitions: (04-23 snake plant)(05-07 peace lily)(05-07 succulent)
   rolling  [04-28..05-28] = 2,2,2,2,2   (mode 2)   strict 30-day: snake plant is 5 days early
   calendar [04-01..05-28] = 3,3,3,3,3   (mode 3)   natural "last month" -> CORRECT, 5/5 stable
```
The three acquisitions are extracted exactly and aggregate-keyed; the owned-but-not-acquired
distractors (fern/spider/basil) are correctly NOT emitted. The count is **perfectly self-consistent**
(5/5 either way). The censored→abstain case is now a concrete, deterministic, inspectable windowed δ.
**The only residual is window SEMANTICS** — a genuine NL ambiguity, not perception/fold error: the
gold's "in the last month" is the calendar sense (an item ~5 weeks back, described as "last month",
counts). Both readings are surfaced; the natural (calendar) one matches gold. This is the honest close
of Lever A: precision held, and the remaining freedom is a documented one-parameter window definition,
not model fuzz.

### Law-3 RESET — BUILT 2026-07-03 (real menhir demand, not a D0 question)

Reassessed on challenge: Law-3 is NOT benchmark-only — the anchor+delta shape (a stated total then
later deltas without a re-statement) is the normal cadence of personal-memory self-tracking menhir
targets: balances ("$5k saved" → "deposited $500"), collections ("3 tanks" → "bought another"),
tallies ("read 40 books" → "finished another"). The 14-question D0 slice missing it is a SAMPLING
accident. And there was a demonstrated correctness gap: the gate *abstained* (count-floor / redundant
triangulation) on anchor+delta when the correct current value is `anchor + post-anchor deltas`.

Implemented as the general rule that UNIFIES the four cases already built: `_reduce` now returns
`(value, events, law3_reconciled)` and, when a group carries a stated `anchor` AND events dated AFTER
it, computes `CURRENT = anchor.value + reduce(post-anchor events)` (fold-algebra Law-3). The gate skips
the redundant-triangulation veto for a reconcile (anchor + deltas are additive, not two readings of
one quantity; the reconcile IS the corroboration → `triangulated=True`). **Strictly additive**: only
the post-anchor-deltas-present case changes; move-1, pure move-2, and the redundant-triangulation /
Lever-B paths are byte-identical (all events at/before the anchor still take the existing cross-check).
`_after` is conservative (unparseable/None → not-a-delta). 4 unit tests (anchor+distinct→4,
anchor+SUM balance→5700, pre-anchor stays redundant-triangulated, demo). 62 suite green.

---

# Lever C — heterogeneous aggregate-keying + cross-episode event dedup (PLANNED, next lever)

**Surfaced by the full-graph consolidation run (2026-07-03, `perception-lme`), from the bike case
`gpt4_d84a3211` (gold $185 = lights $40 + helmet $120 + chain $25 — a move-2 SUM across dated
episodes).** Over the real 23-episode graph the model did NOT produce the tuning-run's confident
`bike_spend=225`; it **fragmented** the spend into per-item measures (`bike_lights_spend`,
`helmet_spend`, `chain_repair_spend`, `bike_tune_up_spend`…), and that fragmentation is unstable
across the k=5 samples, so **self-consistency (veto-1) abstained on every fragment** — a *different*
guard than the tuning run's cross-check, both correct. **Precision held (zero wrong Views); this is a
RECALL residual, now precisely characterized as two distinct hard problems:**

### C1 — heterogeneous aggregate-keying (the "bike_spend" grouping)
Lever A's aggregate-keying groups HOMOGENEOUS items (3 plants → `plants_acquired`). "Bike-related
expenses" is HETEROGENEOUS — lights + helmet + chain must key to ONE `bike_spend` despite being
different things. The extractor instead keyed per-item, so no single View can sum to $185. This is a
category-membership judgment ("is a helmet a bike expense?") that belongs to perception (keying is
perception's job — fold-algebra doc), NOT a fold change. Likely a prompt/keying-guidance lever:
teach the extractor to key by the QUESTION-RELEVANT CATEGORY (spend-on-X) when episodes share a theme,
not by the specific item. Risk: over-grouping (is a water bottle a "bike expense"?) — precision-first,
so when the category boundary is fuzzy, fragment-and-abstain (the current safe outcome) beats a
confident wrong grouping. Measure against `gpt4_d84a3211` → $185 without inflating held-out FP.

### C2 — cross-episode event dedup (a Law-2 hazard the current ledger doesn't cover)
The one modal fragment, `bike_lights_spend=80`, is the **same $40 lights purchase double-counted**
because two episodes both narrate it ("recently got a new set of bike lights installed, which were
$40" appears twice). Today's Law-2 guard (`exclude_folded` / the View's MENTIONS ledger) dedups by
EPISODE-already-folded — it does not catch ONE real-world event described in TWO episodes. This is the
harder identity problem: cross-episode event coreference. Candidate: dedup purchase/acquire events by
`(identity, value, ~date)` signature before the SUM (a deterministic key), OR the embedding-dedup §3
guard extended from item-identity to event-identity. Precision-first: when two mentions MIGHT be one
event, the conservative merge (dedup) is the safe bias for a SUM (avoids inflation), the opposite of
the DISTINCT-count bias — note the asymmetry. This is the genuinely novel piece; C1 is prompt work.

**Sequencing:** C1 first (prompt/keying; unblocks the $185 grouping, cheap), then C2 (cross-episode
dedup; the real new mechanism). Both measured on `gpt4_d84a3211` at zero held-out FP cost. Neither is
a knob; both keep the deterministic core pure (C2's dedup is a deterministic pre-SUM σ, not a model
call). **The conjunctive gate already makes the UNbuilt state SAFE** — fragmentation self-abstains, so
Lever C is pure recall upside with no precision downside, and can wait for demand without risk.

### RESULT — C2 + C1 BUILT 2026-07-03 (bike case: precision holds; an honest coreference limit remains)

- **C2 cross-episode dedup — BUILT** (`fold_algebra.dedup_events`, applied to sum/count in `_reduce`).
  Collapses one occurrence narrated across episodes by an occurrence signature. Unit-proven; a
  non-LME scenario ($12 magazine sub twice → $20). Commit `8353df9`.
- **C1 heterogeneous keying — REDESIGNED after the prompt approach FAILED, then BUILT** (commit
  `2bad574`). C1-as-prompt (ask the extractor to key lights+helmet+chain to one measure) did NOT work
  — the model itemizes and refuses the global grouping (verified live). The fix is the **user's
  decomposition**: a stable LOCAL per-item category tag (helmet→'biking', **5/5 stable** where global
  grouping scattered) + a DETERMINISTIC group-by (`_category_spend_groups` synthesizes `<category>_
  spend` SUM groups). `Event.category` added; generalizes Lever A's category=measure/item=identity
  shape to HETEROGENEOUS categories. `_event_signature` now prefers the stable `category` over the
  noisy `what` quote, so same-day re-mentions collapse (recurring-purchase boundary kept: different
  days stay separate — tested). Proven general (camping→$350 unit test; the 3 correct cases still
  commit, one now cleanly as `luxury_spend`).
- **The honest limit (bike case still safely ABSTAINS):** the $40 lights are re-narrated across
  episodes with DIFFERENT inferred dates (04-20 vs 05-05 — the model dates each mention by its episode),
  so `biking_spend` folds to 225 (lights doubled). Deterministic dedup can't collapse different-date
  re-mentions without dropping the day, which would catastrophically merge recurring purchases (daily
  coffee). This is genuine **semantic coreference** ("I also got bike lights" = the same purchase — an
  embedding/LLM judgment), NOT a deterministic key. Left to the cross-check backstop, which vetoes the
  inflated 225 → abstain. **Precision holds; the recall miss is real ambiguity, and forcing $185 by
  weakening the signature would be overfitting the benchmark at the cost of recurring-purchase SUMs.**
- **Next (deferred, demand-gated): semantic event coreference** — cluster candidate same-purchase
  mentions by embedding(what) + value proximity, independent of date, to dedup re-narrations. The one
  remaining mechanism; the gate keeps the unbuilt state safe.

## Sequencing & scope
1. **Lever B** (broadened triangulation) — ✅ DONE 2026-07-03 (B3 live-confirmed; stated-floor added).
2. **Lever A** (σ WINDOW, A1–A5) — ✅ DONE 2026-07-03 (`3a704032`→3). A6 resolver ✅ built + graph-proven.
3. **Law-3 RESET** (anchor+delta) — ✅ DONE 2026-07-03 (real menhir demand; unifies the four value cases).
4. **Lever C** (heterogeneous keying C1 + cross-episode event dedup C2 + semantic coreference C3) —
   ✅ **BUILT 2026-07-03** (commits `2bad574`, `8353df9`, `f933115`; see "RESULT" above + the
   2026-08-08 status note). Nothing outstanding in this plan.
Deferred (product decisions, not machinery): A6 ranked-result injection; cross-batch incremental Law-3
(batch re-fold chosen — see `perception-consolidation-prod-wiring.md`). Both menhir-frontier; unit
tests need no graph/LLM; live measurements cost API (stop on 429). Keep the deterministic core pure —
every new signal is a gate input or a read-time δ, never a stored View field.
