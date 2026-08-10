# HANDOFF → Applying D0 Retrieval Entropy to menhir

**Date:** 2026-07-02 · **For:** a future menhir session (design + build) · **Type:** design handoff
**Thread:** take the D0 entropy instrument from a benchmark harness → a first-class menhir objective function
**Related:** `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md` (the experiment),
`menhir-frontier/.agent/plans/fold-algebra.md` (folds — D0 is their acceptance test),
`menhir-frontier/.agent/plans/aggregation-as-consolidation.md` (the metric family).

## 1. What D0 is (recap, so this is self-contained)

**Retrieval Entropy — a deterministic, GPT-free measure of how far memory is from a
*query-sufficient state*: the smallest bundle of evidence that still answers the question.**
Instrument: `archolith-bench/scripts/longmemeval/analysis/entropy.py`. Two columns:

- **FLOOR** — the intrinsic dispersion of the answer's own evidence (episodes · sessions · tokens ·
  entities · timespan). Retriever-independent. **This is what consolidation compresses** (8 scattered
  facts → 1 state fact).
- **DELIVERED** — how far the *current retriever* walks (greedy set-cover by rank) before it reaches
  sufficiency. **DELIVERED − FLOOR = retriever inefficiency; FLOOR itself is the consolidation target.**

**Sufficiency is deterministic** (the property that makes it valid, not an LLM "can you answer?"): a
set is sufficient when it touches the gold provenance. In LME that's "an entity MENTIONED by a
`has_answer` episode" — the dataset supplies gold. Hold that thought; §4 is the whole problem.

## 2. Why it matters (why we built it)

The entire LME campaign was **noise-limited**: N=30 llm-judge, ±1 baseline swings, every read-time
lever landing inside the noise. **D0 is the objective function that is not downstream of an LLM** —
it measures the *organization of memory itself*, deterministically. It's what let tonight's
experiment produce hard numbers instead of judge variance. Any consolidation pass (fold) can now be
graded: *did it move memory closer to query-sufficient (floor drops) without failing sufficiency
(the precision guard)?* Compression and correctness become one measurement.

## 3. What we proved with it (2026-07-02, both arms — see the d0 plan for full tables)

- **Arm A (oracle representation):** a single state fact collapses DELIVERED to **rank 1 / 1 memory /
  ~21 tokens** on 12/14 counting Qs (median rank 2→1, tokens 133→21.5), recovering 2 previously
  unreachable. Representation ceiling is real.
- **Arm B (perception):** stated-total perception is reliable (5/5); the other ~9 need a deterministic
  **fold** (SUM/DISTINCT/DATEDIFF), not a better model.
- **The caveat that governs everything below (Finding 2):** LME sufficiency is *lenient* — "first
  gold-provenance touch," NOT the assembled, correct, **current** answer. So D0-retrieval measures
  whether the answer's evidence is **privileged in retrieval** — it does **not** measure end-answer
  correctness or current-vs-superseded. It is a *reachability/organization* metric, not an accuracy one.

## 4. The core problem of moving D0 into menhir

**In LME, "gold provenance" comes from the dataset (`has_answer` labels). Production menhir has no
labels.** The deterministic sufficiency check — the thing that makes D0 valid and un-Goodhart-able —
has no obvious source for an arbitrary live query. This is THE design problem; everything else is
mechanics. Three ways to source deterministic sufficiency without labels and without an LLM:

### (a) View-reachability — the Views ARE the oracle · **recommended first, cheapest**
When menhir maintains a View for `(subject, measure)`, the sufficient target for that query class is,
by construction, **"retrieval reaches that View."** So D0 for a maintained View = **the rank at which
its own canonical surface is recalled.** Zero labels, zero LLM, continuous. Rising rank = the recall
path buried the current state (exactly the bug the 2026-07-02 supersession-recall fix addressed, and
the Law-1 ordering hazard the fold-algebra design found). This makes D0 a **recall regression guard
keyed on the Views menhir already has** — the highest-leverage, lowest-cost integration.

### (b) Provenance-based sufficiency — generalizes beyond Views
For arbitrary recalled content, the sufficient set is the minimal **provenance-bearing** node set
carrying that content's chain — and menhir already has the provenance graph (`MENTIONS`,
`SUPPORTED_BY`/:Evidence, `episodes[]`). D0 = size/spread of the minimal provenance set the retriever
must reach. Deterministic, LLM-free, works without a stated View. More general than (a), more work.

### (c) Curated eval set — a memory "test suite"
A small, hand-or-once-LLM-curated set of `(query → known sufficient node/provenance)` pairs, versioned
in-repo, run as a **nightly D0 regression**. This is the LME pattern generalized to menhir's own
domains (code memory, agent-experience counters). Cheapest to stand up; needs curation upkeep.

## 5. Integration seams in menhir (where the code hooks in)

- **Recall trace (R0 already exists).** `recall(..., trace=True)` returns a `RetrievalTrace`. Add a
  deterministic **entropy field** (rank-to-sufficiency + footprint) computed from provenance/Views —
  observability first, no behavior change.
- **`menhir entropy` MCP tool / CLI verb.** Compute retrieval entropy for a query against the live
  graph, sufficiency via (a)/(b). Ad-hoc probe + the unit the regression gate calls.
- **Fold acceptance test (ties to `fold-algebra.md`).** Every ViewKind fold claims an entropy
  reduction for its query class; D0 verifies it deterministically on write, and the sufficiency guard
  is the precision check (a wrong fold *raises* entropy / fails sufficiency). Make D0 the fold's CI.
- **Nightly regression gate.** Run D0 over the maintained Views (a) + a curated eval set (c); alarm
  when entropy rises. This is where D0 pays rent continuously — it catches ranking/schema/supersession
  regressions that unit tests can't see.
- **The metric family (deferred siblings, all deterministic — see aggregation plan §metric family).**
  Retrieval (built) · Temporal (`valid_at` spread) · Provenance (# distinct chains) · Belief (# live
  supersession branches). Cheap next; Reasoning-entropy needs an LLM → keep deferred.

## 6. Caveats to carry into any build

1. **Reachability, not accuracy.** D0 says the answer's evidence is *privileged in retrieval*; it does
   not say the answer is *correct or current*. Pair it with the **belief-gate** for currency (the
   superseded-vs-current axis — the to-watch 25/20 case). Never report D0 as an accuracy metric.
2. **Sufficiency source is load-bearing.** Whatever supplies "gold provenance" in production (View,
   provenance chain, curated pair) IS the thing that keeps D0 honest. If that signal is itself an LLM
   judgment, you've re-imported the variance D0 exists to remove. Keep it deterministic.
3. **FLOOR vs DELIVERED are different jobs.** FLOOR grades *consolidation* (is the state compact?);
   DELIVERED grades *retrieval* (is it reachable?). A pass can win one and lose the other — report both.

## 7. Recommended first step (one thing, cheap, durable)

Build **(a) View-reachability** as an observability metric first: for each current View, recall its
canonical surface and record the rank + footprint; expose it in the recall trace and as a `menhir
entropy` probe. No labels, no LLM, no behavior change — and it immediately makes "did a change bury
current state?" a measurable number. Everything else (provenance sufficiency, the nightly gate, the
fold acceptance test) builds on that primitive.

## 8. Where things live
- Instrument: `archolith-bench/scripts/longmemeval/analysis/entropy.py` (+ `lme.sh entropy`).
- Experiment + results: `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`.
- Views (the production sufficiency oracle): `menhir-frontier/src/menhir/infrastructure/view_repository.py`.
- Recall trace / R0: `menhir-frontier/src/menhir/services/recall_service.py` (`trace=True` → `RetrievalTrace`).
- Provenance model: `MENTIONS`, `SUPPORTED_BY`/:Evidence, `episodes[]` (see `.agent/` architecture docs).
- Prior art on the metric framing: `aggregation-as-consolidation.md` §"D0 — Retrieval Entropy".

**Design needs no live graph.** (WSL/Docker/`menhir-lme-neo4j` are shut down; bring up only to
prototype the probe against real Views.)
