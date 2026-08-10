# Plan: production wiring for the personal-memory perception consolidation

> **Status note 2026-08-08 (curator audit).** The line below is stale — the wiring is BUILT:
> `consolidate_personal_memory` (`services/scheduler_tasks.py:468`) is a registered nightly job in
> `MaintenanceScheduler` (`services/maintenance_scheduler.py:150-153`), dirty-namespace-scoped,
> batch re-fold, gated behind `personal_memory_enabled` (default **False** —
> `personal_memory_consolidation_enabled` setting) — the same default-off-feature pattern as the
> rest of the frontier stack, not literally "blocked until benchmark mode ends." All four locked
> decisions in this doc (cadence/scope/eval-mode + the fourth below) are implemented as designed.

**Status: DESIGNED 2026-07-03 (decisions locked; build gated on leaving benchmark mode).**
The perception boundary (`services/perception.perceive_and_fold`) is BUILT, tested, and proven
end-to-end (the Arm-C capstone wrote real Views into the graph; the stratified-90 consolidation ran
via `archolith-bench/scripts/longmemeval/analysis/perception_write.py`). What is missing is a PROD
INVOCATION: today nothing runs it automatically — only benchmark scripts and explicit calls do. The
agent-experience QuantState folds (`sync_experience_counters`) run nightly in the maintenance
scheduler; the personal-memory perception pass has no equivalent. This plan is that equivalent.

## The four decisions (locked)

### 1. Cadence — **nightly, in the maintenance scheduler** (NOT on-ingest, NOT per-N-episodes)
On-ingest is the wrong layer: perception is k-sample LLM per subject, so inlining it would put ~6 LLM
calls on every episode write and block the ingest path. Per-N-episodes is fiddly and consolidation is
cheap to re-run wholesale. Nightly mirrors `sync_experience_counters`, which already runs on the
maintenance loop and is **disabled in benchmark mode with the rest of the scheduler** — which is why
this is gated: in benchmark mode (where all the eval work lives) it stays an EXPLICIT pass
(`perception_write.py`); the scheduler task matters only once this runs outside benchmark.

### 2. Scope — **dirty namespaces only; subject = the perceived measure**
Consolidate a namespace only if it has Episodic nodes newer than that namespace's Views'
`created_at` (a cheap dirty-namespace query), skipping the unchanged majority each night. The subject
is whatever the extractor keys (`bike_spend`, `plants_acquired`) — no separate scope config. This is
what makes the batch-re-fold cost acceptable (next).

### 3. Evaluation mode — **BATCH re-fold** (the load-bearing decision; Law-3 settles it)
Recompute each dirty subject from ALL its episodes every pass, rather than incrementally accumulating
new events. Reasons, in order:
- **Correct-by-construction** — the fold-algebra doc's endorsed path; replay-safe, no dedup ledger.
- **Law-3 requires it.** The anchor+delta reconcile computes `CURRENT = anchor + reduce(events after
  anchor)` and ASSUMES the whole event set is present in one fold. Incremental splits anchor (Monday)
  and delta (Friday) into separate batches, forcing a fragile reconstruction of the anchor from the
  stored View value. Batch sees anchor+delta together every pass, so Law-3 just works. **This is a
  direct consequence of the Law-3 build — it is not a coin-flip.**
- **Bounded cost** — batch over DIRTY namespaces only (decision 2) means "recompute everything" is
  cheap in practice; the only waste (re-LLM a subject whose episodes didn't change) is killed by the
  dirty filter.

### 4. Cost — **budget cap + k as the dial; cross-check already lazy**
Hard per-run LLM-call cap (resumable across nights) + 429 hard-stop (both already in the harness). `k`
is the cost/precision dial: k=5 for eval-grade precision, k=1–3 acceptable in steady state once the
thresholds are trusted. The Lever-B cross-check is already lazy (one holistic call per would-commit
measure, not per sample), so it is not a multiplier over abstained namespaces.

### 5. Which guards to pin — **ALL bias guards on** (gap analysis #3, corrected)
The gate's bias guards (`enable_cross_check`, `enable_coref`, `enable_verify`) all default **False**;
the only always-on bias catcher is user-stated triangulation, which fires only when the user stated a
total. **Under raw defaults the doc's own motivating failure — a unanimous-but-wrong itemized SUM —
commits.** The doc's precision-monotonicity argument (an added abstain-only veto can only *remove*
wrong writes) means the prod wrapper must pin **all three on**, not just cross-check. Cost is bounded:
cross-check and coref are lazy/memoized (one call per would-commit cluster), verify runs only on
would-commit move-2 folds. Pin: `enable_cross_check=enable_coref=enable_verify=True`.

### 6. Law-3 bias blind spot — **must be closed before pinning** (gap analysis #1, correctness)
A Law-3 anchor+delta candidate (`anchor + reduce(events after anchor)`) currently passes with **no
bias guard**: veto-3 is skipped by design ("the reconcile is the corroboration") AND veto-4's
condition is `stated is None`, which is false for every Law-3 candidate (Law-3 needs a stated anchor).
So a *post-anchor re-mention of an already-anchored item* ("my 5-gal tank is dirty" after "I have 3
tanks") is folded as a phantom +1 delta → stores 4 when the truth is 3, uncorroborated. The C4 verify
prompt can't catch it either — the overlap is between a listed item and the anchor's *unenumerated*
base. **Two fixes, land before wiring:** (a) extend veto-4 to also fire on Law-3 outputs (drop the
`stated is None` gate for the reconcile branch — the holistic derivation reads the same episodes and
naturally answers 3 vs 4, the independent second opinion the doc demands); (b) add an anchor-overlap
question to `VERIFY_PROMPT` ("could any listed post-anchor item already be part of the stated base?").
Needs live re-validation on an anchor+delta case.

## Build (when it leaves benchmark mode)

Anchor: `scheduler_tasks.sync_experience_counters` (the existing nightly View-writing task shape).
- **`scheduler_tasks.consolidate_personal_memory(graph_adapter, *, llm_complete, embed=None,
  namespaces=None, k=3, threshold=1.0, call_budget=None)`** — for each dirty namespace, load its
  Episodic user turns, `perceive_and_fold(...)` (batch; cross-check on; Law-3 active), accumulate
  committed/abstained counts, stop at `call_budget`. Returns `{namespaces, views_written, abstained,
  calls}`. The write path (`perceive_and_fold → fold_events_to_counter/_timeline → ViewRepository`)
  is unchanged — this is only the scheduling+scope+budget wrapper.
- **Dirty query** — `MATCH (e:Episodic {group_id:$ns}) WITH max(e.created_at) AS newest
  MATCH (v:Entity {is_view:true, group_id:$ns}) ...` or, simpler, "namespaces with any Episodic
  newer than the namespace's newest View `created_at`; all namespaces with no Views yet." Put it on
  the scheduler graph adapter next to `list_structure_projects`.
- **Register** on the maintenance loop with the other nightly jobs; honor `MENHIR_BENCHMARK_MODE`
  (off in benchmark, like `sync_experience_counters`).
- **LLM/embed seams** — inject `llm_complete` (the prod chat model) + `make_view_embedder(settings)`,
  exactly as the bench harness injects gpt-4o-mini + the sync embedder.

## Explicitly NOT in scope (decided, not forgotten)
- **Cross-batch incremental Law-3** — only needed if prod ever chooses incremental over batch; batch
  is chosen, so this stays unbuilt (and the Law-3 code needn't change).
- **A6 ranked-result injection** — whether a synthesized windowed count out-ranks retrieved nodes in
  `RecallService.recall` is a separate product decision (`windowed_recall.answer_windowed_count`
  exists and is callable today).
- **More reducers / event kinds / window phrases** — on demand only.

## Verification (when built)
1. Unit: `consolidate_personal_memory` with a fake adapter + fake LLM — dirty filter selects the
   right namespaces, budget cap stops mid-run, batch re-fold is idempotent on re-run.
2. Live (outside benchmark): one nightly pass over a small namespace set; confirm Views written,
   re-run writes nothing new (idempotent), a new episode re-dirties its namespace.
3. Regression: the answer A/B (recall with vs without the consolidated Views) — the end-to-end payoff.
