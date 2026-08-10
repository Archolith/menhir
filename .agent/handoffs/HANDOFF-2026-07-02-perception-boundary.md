# HANDOFF → Perception boundary: episodes → typed Events (precision-first, abstaining)

**Date:** 2026-07-02 · **For:** a future menhir session · **Type:** design + build handoff
**Thread:** the last missing half of move-2 — the LLM boundary that produces the `Event`s the
(already-built, deterministic) fold consumes.
**Related:** `.agent/plans/fold-algebra.md` (the deterministic core, DONE),
`archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md` (Arm B: the demand + the FP risk).

## The one invariant (governs every decision below)

> **Perception may be probabilistic. Folds and Views must stay deterministic.**

The model helps decide *"is this a trustworthy event to write?"*. Once an Event is written the system
behaves like a database/compiler, not another LLM guess. All statistics live at THIS boundary; the
output is typed `Event`s (+ a commit/abstain decision). ρ/δ never see a probability.

## The rule (the spine — precision-first abstention)

> **When uncertain, do not write the View. Keep the raw episode. Let recall fall back to normal memory.**

A missed View is annoying; a **wrong** current-state View is dangerous — it ranks well and looks
authoritative (Arm B: FP ≫ FN). So Views are promoted only when the perception boundary is confident.

**Why this is safe for free (no fallback code to write):** raw episodes always ingest as normal
memory; Views are purely additive; recall already returns whichever exists (a View is just a stamped
`:Entity` that ranks — see `view_repository.py`). So **the absence of a View IS the fallback** —
recall returns the raw episode and ranks it normally. Abstention is safe by construction. Raw events
stay ground truth; Views are a confident-only projection on top.

## What exists to build on

- **`domain/fold_algebra.py`** — `Event(when, kind, value?, identity?, what?, episode_uuid)` + the
  four reducers. This is the target output type.
- **`services/event_fold.py`** — `fold_events_to_counter(..., reducer=sum|count|distinct_count)`.
  The deterministic sink. Perception's only job is to produce the `Event` list this consumes.
- **`services/quantstate_consolidator.py`** — the PRECEDENT: `extract_events(episodes,
  llm_complete) -> events` (injected LLM, prose→typed events→fold→record_counter). The perception
  extractor is this shape, emitting the new `Event` dataclass for the event-log fold, wrapped by the
  abstention gate.
- **`services/view_embedder.py`** — the sync embedder seam (reuse for the dedup embeddings, §3).

## The confidence layer — a conjunctive veto-gate (three signals, no fitted weights)

Commit an Event/View only if ALL applicable checks clear. Any single red flag → abstain. Missing
signals don't veto (e.g. no item events → triangulation simply doesn't apply). **No weighted score —
that's the calibration trap; we have ~14 labeled questions, nowhere near enough to fit weights.**

**1. Self-consistency entropy (the primary gate).**
Run the extractor k times (temperature > 0). Measure dispersion of the extracted value:
- concentrated (e.g. k/k agree on `$185`) → low entropy → **commit**.
- scattered (`{185, 200, 50, …}`) → high entropy → **abstain**.
The agreement fraction is computed DETERMINISTICALLY from the samples — stochastic input, deterministic
decision. This is the write-time sibling of D0 retrieval entropy (distance-from-*certain*-state at the
perception boundary vs distance-from-*sufficient*-state at recall). Used ONLY as a gate, never stored,
so it does not re-import LLM variance into the deterministic core.

**2. Fold triangulation.**
When perception emits BOTH a stated total and item events, they are two independent derivations:
`SUM(item events) ≈ stated_total`? Agreement raises confidence; disagreement flags for review /
abstains. Cheap, deterministic, uses redundancy already in the data — the fold grading the perception.
(This is also the Law-3 RESET corner from the fold-algebra design; triangulation is its guard.)

**3. Embedding dedup for DISTINCT identity resolution.**
DISTINCT-COUNT is correct only if "5-gallon tank" and "the 5 gallon one" resolve to one item. Exact
-string `identity` over-counts real prose. Cluster mention embeddings (cosine + a conservative
threshold biased toward keeping items SEPARATE unless clearly the same — precision-first), count
clusters. Reuse `view_embedder`. Ambiguous cluster boundary → abstain on the count.

## The one tunable knob, and how to set it

The self-consistency threshold is the single knob. **Tune it on Arm B's held-out NON-counting slice**
(the precision probe: `d0_arm_b.py`, the 12 non-counting namespaces) to a **precision target** — e.g.
"zero wrong current-state Views on the held-out slice" — and accept whatever write-rate falls out.
Recall is the free variable; precision is the constraint.

## Observability (abstention is a signal, not a silent skip)

Log the abstain-rate per (subject, measure). A high rate means either a weak extractor or genuinely
non-View-shaped content — both worth seeing. **Nice recursion: perception abstention is itself a
counter** — instrument the boundary with our own QuantState primitive (`perception_abstained`), so the
write-rate is a first-class, recallable fact. Composes with D0: an abstained subject has no View, so
the reachability probe correctly has nothing to measure for it (you can't regress on state you
deliberately didn't materialize).

## Build status (2026-07-02) — steps 1-4 BUILT, step 5 is live-only

`services/perception.py` + `tests/test_perception.py` (12 unit tests, all green; no live LLM/graph —
fake `llm_complete` + fake `embed`). Adjacent deterministic suites (`test_fold_algebra`,
`test_view_repository_lww`) still green.

- **1 extractor** — `extract_once(episodes, llm_complete) -> [PerceivedGroup]`, mirrors
  `quantstate_consolidator.extract_events`; groups by `(subject, measure)`, infers the scalar reducer
  from event kinds, peels `kind=assertion` into `stated_total` (never folded), attributes provenance
  by episode number. DONE.
- **2 self-consistency gate** — `gate(samples, threshold=1.0, ...)`; modal-value share over k samples;
  ABSENT (a sample that didn't perceive the measure) counts as disagreement so a minority-seen value
  can't win. Primary veto. DONE.
- **3 wire** — `perceive_and_fold(...)`: commit -> `fold_events_to_counter`; abstain -> no-op (the
  raw episode is the fallback). Optional `record_abstentions` writes the `perception_abstained`
  counter (sec 5 observability). DONE.
- **4 triangulation + dedup** — both are conjunctive vetoes inside `gate`: `SUM(items)` vs
  `stated_total` (tolerance-checked); DISTINCT identity resolution via conservative cosine clustering
  (`dedup_threshold=0.92`, bias toward SEPARATE), rewriting identities so the deterministic
  `distinct_count` matches the gated value. DONE.
- **5 tune + observe** — RUN (2026-07-02, live gpt-4o-mini k=5 over the 14 counting + 12 held-out
  namespaces; tool `archolith-bench/scripts/longmemeval/analysis/perception_tune.py`; full result in
  `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`). **Finding: the single
  `threshold` knob does NOT reach the precision target — self-consistency catches variance, not
  bias** (the residual danger is a unanimous-but-wrong SUM, `bike_spend`=225 vs gold 185, at
  agreement 1.0). Two things changed as a result:
  (a) a **deterministic `value>1` count-floor** veto was added to `gate` (`min_count=2`, SUM exempt) —
      it kills the dominant `distinct_count`=1 over-extraction at zero recall cost (wrong 4→1,
      heldout_FP 4→2 at thresh 1.0). This confirms the design's "add orthogonal vetoes, don't fit a
      score" stance — **no confidence value was added** (it would be anti-correlated with correctness
      on the confident-bias cases).
  (b) gate evidence now persists as a provenance **receipt** on the committed View (`view_audit_*`
      props, kept out of signature + embedding). Remaining levers (not knobs): aggregate keying in
      the extractor prompt, and broader triangulation coverage (the only constraint on SUM bias).

## Build order (demand-anchored, each shippable alone)

1. **The extractor** — `episodes -> list[Event]` (injected `llm_complete`, mirroring
   `quantstate_consolidator.extract_events`), typed to the `fold_algebra.Event` dataclass. Emits
   purchase/item/assertion events with `when` (date-grounded — episodes are backfilled), `value`/
   `identity`, and `episode_uuid` provenance.
2. **Self-consistency gate (§1)** — k-sample, dispersion, abstain-or-commit. The precision spine.
3. **Wire to the fold** — commit path calls `fold_events_to_counter`; abstain path is a no-op (the
   raw episode already carries the fallback).
4. **Triangulation (§2)** and **embedding dedup (§3)** — layer onto the gate as the sum/dedup demand
   is exercised.
5. **Tune + observe** — threshold on the precision probe; abstain-rate counter.

## Anti-goals
- **No probability inside ρ/δ/Views.** Non-negotiable — the deterministic core is the whole point.
- **No fitted calibration / Bayesian count priors** — insufficient labeled data; would fit noise.
- **No engineered fallback path** — the fallback already exists (absence of a View = raw-episode recall).
- Recall-first for uncertainty: prefer a missed View over a wrong one, always.

## Environment
Deterministic pieces need no live graph; the k-sample extraction + threshold tuning need the LLM
(gpt-4o-mini was the Arm B model) and, for end-to-end, the benchmark graph. STOP on 429 per protocol.
