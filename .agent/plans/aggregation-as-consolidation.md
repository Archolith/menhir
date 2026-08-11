# Aggregation as a write-time consolidation problem — CURRENT DIRECTION

**Status: ACTIVE DIRECTION (2026-07-02).** Research docs incoming will refine this; the thesis
and the first test below are the frame they land on. Supersedes further read-time retrieval work
on LME (see the negative results in `.agent/plans/anecdotal-recall-oracle-ladder.md` and
`archolith-bench/.agent/benchmark-notes/lme-score-campaign.md`).

## The thesis

**LongMemEval's hardest slice — multi-session aggregation ("how many bikes do I own?" → 4,
"how much have I spent on bikes?" → $185) — is not a retrieval problem. It is a consolidation
problem.** A memory system that recomputes a running total on every query has already failed;
it should maintain the total as state, the way a person does (you don't recount your bikes).

Why this frame, not the orthodox one:

- **Every read-time lever we pulled this session landed neutral-to-negative** — fact-edges,
  the full oracle stack, the isolated TemporalOracle (0.367 vs node-only 0.400), and the
  BriefBuilder. You cannot re-rank or re-format your way to information candidate generation
  never assembled. The orthodox aggregation fix (query-classifier → wide/diversity retrieval →
  LLM counts over the set) is *more of the same layer*, and it is structurally doomed: counting
  needs completeness + dedup guarantees that fuzzy vector retrieval cannot provide.

- **The answer is usually already in the graph — just un-privileged.** In the `how many bikes`
  case (`lme-89941a93`, gold 4) the user literally **stated** "a total of four bikes." The graph
  has it. It failed because that stated total was filed into a low-salience entity
  (`'quiver of bikes'`) while the **superseded** `'three bikes'` stayed more prominent. Nothing
  needed to be counted; the current stated total needed to be recognized as authoritative.

> **Reframe (2026-07-02):** the first concrete primitive, **QuantState**, is an *agent-memory*
> counter (failed attempts, retries, recurring errors), NOT a LongMemEval hack. LME object-counting
> is a fuzzy stress test (8%); crisp agent/code events canonicalize cleanly (validated). See
> [`quantstate-agent-counter.md`](quantstate-agent-counter.md). LME is demoted to a ceiling probe.

## Organizing principle: query-sufficient state (the engineering filter)

Aggregation-as-consolidation is one instance of a larger principle that governs the whole
pipeline. **Framing for docs/papers (does NOT claim the formal property):**

> Inspired by the concept of sufficient statistics, Menhir seeks to construct **query-sufficient
> memory states**: compact representations that preserve all information required to answer a
> particular *class* of questions while omitting irrelevant detail.

**The filter every consolidation pass must pass:**
> *Does this pass move the memory closer to a query-sufficient state — additively, over preserved
> raw events, without destroying provenance another query class needs?*

Three things make this load-bearing, not a slogan:

1. **Selection vs. representation — the retroactive diagnosis of the campaign.** Everything that
   failed (fact-edges, oracle stack, temporal oracle, brief-as-reranker) operated on *selection*
   (pick better among fixed representations). The filter operates on *representation* (change what
   is stored). You cannot select your way to information the representation fragmented — which is
   why node-only was unbeatable. The filter would have rejected six of today's experiments on sight.

2. **Sufficiency is query-class-relative → a FAMILY of views, not one state.** "How many bikes" →
   `owned_bikes=4`; "how did I get four bikes" → the acquisition timeline. Same events, different
   sufficient state. So the goal is a small set of query-class-aligned projections. Honest,
   formal-enough mechanism name (no overclaim): **materialized views over an event log** — each
   pass maintains a view (counter, timeline, current-value register) for a query class.

3. **The guard (or the filter self-destructs): additive over preserved events.** A pass can reach
   sufficiency for class A while destroying class B (the counter is useless for "how did I get
   four"). So: **the raw episodes are the substrate/ground truth; consolidated states are views on
   top, never replacements.** This is also the second, independent defense against the Goodhart
   collapse (D0) — you cannot compress into a lie when the source events remain to check against.

D0 restated in this frame: **retrieval entropy measures distance-from-query-sufficient-state.**
Lower = closer. A pass is validated when it moves the memory measurably closer (floor drops)
without failing sufficiency (the precision guard).

## The move: push the work upstream to ingestion/consolidation

Three escalating moves, cheapest first.

1. **Materialize stated quantitative self-totals as canonical, supersedable state.**
   During consolidation, when the user asserts a quantitative self-fact — "now I have four
   bikes", "I've tried four Korean restaurants", "I'm on page 220" — record a first-class
   **quantitative-state fact** keyed by `(subject, measure) → value`, latest value supersedes
   prior. Then "how many X" is a **single-fact lookup**, not an aggregation. Covers the large
   slice where the user states the running total (bikes, restaurants, pages, to-watch list).

2. **Event-log fold for totals nobody stated.**
   "How many days did I spend camping" — individual trips logged, never summed. Use the
   episodic layer (now date-grounded post-backfill) as an **event stream**: model the question
   as a reduction over dated events (count acquisitions, sum durations). Event-sourcing, exact
   where retrieval is fuzzy.

3. **(Long game) Count in the graph.**
   With cleanly-typed entities/relations (`(:Bike)`, `[:OWNS]`), "how many bikes" compiles to a
   Cypher `count()` — complete + deduped by construction. Blocker is extraction quality (today
   you get `'three bikes'`/`'quiver of bikes'`, not typed nodes), which is itself the argument
   that leverage lives in extraction/consolidation, not retrieval.

## Failure-mode census (the 22 "unresolved", answer-anchored classifier)

Note: "unresolved" over-counts — numeric answers can't keyword-match entities, so some are
present-but-numeric or present-but-superseded, not absent. Real sub-types:

| sub-type | ~count | move that addresses it |
|---|---|---|
| Counting / summing ("how many/much") | ~14 | **1** (stated total) + **2** (fold) |
| Assistant-turn detail recall | ~4 | separate — extraction of assistant-stated facts |
| Temporal arithmetic (days between…) | ~2 | **2** (date fold) |
| Current-value lookup (Rachel→suburbs) | ~2 | currentness (belief-gate) |

## Cheapest first test (do this before scaling)

Prototype **move 1** only: a consolidation step that detects stated quantitative self-totals and
writes them as supersedable `(subject, measure) → value` state facts; then A/B on the ~14 counting
questions (bikes, restaurants, pages, to-watch, citrus, tanks, plants…). These are exactly the
cases where the user stated a total that should convert from "uncounted" to "single-fact lookup".
Measure: recall-hit on the state fact + end-answer accuracy vs node-only.

## Research reconciliation (2026-07-02 handoff — "Write-Time Cognition and Compiled Memory")

The research doc independently derived the same frame (its Direction 1/2/3 = our moves 1/2/3),
which is a good signal. Its 13 directions collapse to a small structure; decisions locked below.

**Collapse:**
- *Architecture* — D3 "memory as continuously-compiled artifact." Adopt as **vision, not build
  unit**. Our moves are its first passes; the compiler earns itself only if a pass works.
- *Passes (concrete, counting-relevant)* — D1 quantitative state (=move 1), D2 event fold
  (=move 2), D7 collection objects (Bike A/B/C → `BikeCollection{count:4}`). Build D1 first.
- *Runtime* — D5 replay + D9 pressure = generalizations of menhir's **existing** consolidation
  loop (`lifecycle_service`). Hook in; do not build parallel. NOTE: that loop is OFF in benchmark
  mode (scheduler disabled) — the pass must be run explicitly, like `lme.sh promote`.
- *Storage* — D4 registers: **rejected for now.** Keep quantitative state as an **in-graph
  supersedable fact** (reuse `expired_at` supersession + `episodes[]` provenance). A sidecar
  register is a second source of truth that drifts; only revisit if in-graph lookup is too slow.
- *Metric* — D10 retrieval entropy: **ELEVATED to Phase 1.** "How many memories must be retrieved
  before answerable." Intrinsic, OpenAI-free, deterministic — it fixes the noise-limited
  measurement that dogged the whole campaign (N=30 llm-judge, baseline swings ±1). A good state
  pass drops entropy on counting Qs from ~N→1; a bad (imprecise) pass *raises* it, so entropy
  also guards precision. Build alongside D1.
- *Speculative tail* — D6 counterfactual-Q, D8 reverse-retrieval, D12 cognitive-cache: Phase 3.
- *Read-time outliers* — D11 temporal wavefront, D13 provenance-brief: go back to the **exhausted
  read-time layer**; deprioritize (D13 = the already-measured-neutral BriefBuilder planner).

**The make-or-break risk: detection PRECISION, not recall.** "I now have four bikes" is clean, but
the pass will hit false positives — "I ran 4 miles" (not a persistent count), "there are 3
options" (not self-state), "we had 4 over" (not owned). A wrong current-state fact is **worse than
a missing one**: it out-ranks the truth. First test MUST measure false-positive rate on a held-out
slice, not just recall on the 14 counting Qs. (D10 entropy catches precision regressions for free.)

## D0 — Retrieval Entropy is the objective function (elevated from D10)

**Reordering (2026-07-02, second research pass): the entropy instrument is the FIRST build, not
the metric we tack onto D1.** The campaign's binding constraint was the absence of an objective
function not downstream of an LLM (small N, judge variance, prompt sensitivity). D0 measures the
*organization of memory itself* — deterministic, GPT-free — so it governs every consolidation
pass. Benchmark accuracy becomes downstream validation, not the primary target. All passes
(quantitative state, episode merge, collection objects, timeline) are unified as **entropy
reducers**.

**Definition — with the constraint that makes it valid:**
`Entropy(q) = size/spread of the MINIMAL evidence set that STILL ENTAILS the answer.`
The word *sufficient* is load-bearing. Unconstrained "minimize memories" has a degenerate optimum:
one fabricated fact per question (entropy→1, accuracy→0) — classic Goodhart. **Sufficiency (the
min set must still entail the gold answer) is the constraint that forbids the collapse — and it
doubles as the precision guard:** a wrong consolidated fact drops the gold support out of the
low-entropy set, so the sufficiency check fails and entropy does NOT drop. Compression and
correctness become one measurement.

**The make-or-break implementation detail:** the sufficiency check MUST be **deterministic**
(does the set contain the gold-answer's provenance — source episode / support entity / fact?),
NOT an LLM "can you answer this?" — otherwise it re-imports the very variance it exists to remove.
We already have this machinery: the answer-anchored provenance classifier. Entropy is its
generalization (classifier = rank of answer support; entropy = size+spread of the minimal support
set). Computable on the LME graph today, zero GPT.

**Metric family — build the deterministic four, defer the one that needs an LLM:**
| entropy | computed from | phase |
|---|---|---|
| Retrieval (count · tokens · episodes · sessions · entities · timespan) | graph + provenance | now |
| Temporal (evidence time-spread) | `valid_at` span of support | cheap next |
| Provenance (# distinct chains) | # source episodes | cheap next |
| Belief (# live supersession branches) | `expired_at` branch count | cheap next |
| Reasoning (inference steps post-retrieval) | needs a reasoning trace = LLM | DEFER (re-imports variance) |

## Locked build order (instrument first, pass second)

1. **D0 entropy instrument** — offline harness over the LME graph: for each question, greedily
   grow the recall set until the deterministic sufficiency check (gold-provenance present) passes;
   report the entropy vector (count/tokens/episodes/sessions/entities/timespan). No GPT. This is
   the fitness function every later pass reports against.
2. **D1 pass** — consolidation-layer detector: explicit quantitative self-state → supersedable
   in-graph fact `(subject, measure) → value` (latest expires prior). Run explicitly (scheduler
   off in benchmark mode).
3. **Entropy delta** — D0 before vs. after D1 on the ~14 counting Qs. Target: →1 memory / 1 fact /
   ~40 tokens for stated-total questions. A wrong-fact regression shows as entropy NOT dropping
   (or sufficiency failing) — the precision guard.
4. **Only then** an accuracy A/B (llm-judge) vs node-only — as downstream validation, not the
   primary signal.
