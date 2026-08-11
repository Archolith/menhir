# QuantState — a supersedable counter/register for recurring agent events

> **ARCHIVED 2026-08-10.** D1 shipped and no longer owns active work. The original follow-ups have
> been reconciled against current code: the typed telemetry bridges run via
> `sync_experience_counters`; the generic `view_key`/`view_kind`/`view_current` indexes shipped;
> D0's before/after grade is recorded in
> `archolith-bench/.agent/plans/d0-entropy-delta-counting-slice.md`; and scheduled bridges receive
> the production embedder. `services/quantstate_consolidator.py` remains an explicit/manual,
> dependency-injected predecessor path—it is not the scheduled bridge job. Current
> runtime ownership lives in [`.agent/architecture.md`](../../architecture.md), with execution
> status in [Track W](../../plans/menhir-research-execution-ladder.md). The body below is retained
> as the D1 rationale and implementation record.

> **RELOCATED 2026-08-07 (curator audit, ctharvey-approved): moved back from
> `docs/research/direction/` to `.agent/plans/backlog/`.** This doc describes the SHIPPED,
> LIVE primitive — `services/quantstate_consolidator.py` and `services/view_entropy.py` are
> folded into prod by the hourly `sync_experience_counters` job (off under benchmark mode).
> Per corpus convention, `.agent/` is the operational surface for the shipped system;
> `docs/research/` is forward research. This doc no longer fits the research corpus now that
> its content is fully realized, not speculative. It was originally relocated here-to-there
> on 2026-07-11 (commit `998305f`) when it was closer to open design rationale; that call
> has been reversed now that "shipped and live" is unambiguous. Reunited with its companions
> `aggregation-as-consolidation.md` (thesis, stayed in this directory throughout) and
> `event-fold-view-architecture.md` (architecture, also moved back).

**Status: ACTIVE (2026-07-02).** The concrete first primitive of the query-sufficient-state
direction (`aggregation-as-consolidation.md`). Reframed after the LongMemEval probes: QuantState
is **not** a benchmark hack — it is an **agent-memory primitive.**

## What it is

> **QuantState:** a supersedable, provenance-backed counter/register for recurring agent/code
> events. Updated by structured `+1 / -1 / =N` events folded deterministically; the current value
> is a query-sufficient view for "how many times has X happened?"

## Why agent/code memory, not life memory

For **life** memories, counting is fuzzy — and the LME probes proved it:
- Was this the same wedding? (distinct-instance dedup) — regex detector 5% recall, LLM fold 8%.
- Is "5-gallon tank" a tank *type* or a *count*? (measure fragmentation)
- Does "last month" constrain the fold? (temporal windowing)

For **agent/code** memory, counting is crisp and Menhir-native:
```
test_failed        += 1
approach_failed    += 1
retry_count        += 1
dependency_broke   += 1
same_error_seen    += 1
fix_succeeded      += 1
```

The events are structured and unambiguous ("test X failed" is a fact, not a judgment call), and
the count is **directly actionable** — unlike a fuzzy life-event tally.

## Primary targets

- failed attempts
- repeated errors
- flaky tests
- retries
- recurring regressions
- repeated successful fixes
- stale assumptions corrected multiple times

## LongMemEval

**Stress test only — not the design target.** It's the hard, fuzzy end of the primitive; it will
never hit high accuracy because the underlying dedup is genuinely ambiguous. Useful as a ceiling
probe, not a goal.

## The real payoff

An agent should behave differently after
```
"this failed once"
```
versus
```
"this has failed 4 times"
```
**That is not retrieval. That is experience.** A counter the agent can read is the difference
between re-deriving a dead end every session and knowing it's a dead end.

## Design (unchanged from the fold prototype, retargeted)

- **Perception (LLM, write-time):** read agent episodes, emit counter events
  `{counter: <stable snake_case key>, op: delta|set, value, subject, evidence}`. The LLM must
  **canonicalize** semantically-equivalent events to the SAME counter key (crisper in the code
  domain than the life domain).
- **Fold (deterministic):** `set` resets, `delta` accumulates, per counter key. No LLM arithmetic.
- **Node (in-graph, additive):** a supersedable QuantState node `(subject, counter) -> value` with
  `episodes[]` provenance, sitting ON TOP of the preserved raw episodes (never replacing them).
- **Grade:** the D0 entropy instrument — "how many times has X failed?" should collapse to a
  single-node lookup (floor -> 1).

## First prototype — DONE, validates the reframe

Ran perception+fold (gpt-4o-mini) on this session's own work log (12 episodes). Result:
```
read_time_intervention_failed = 5   (fact-edges, oracle stack, temporal oracle, brief-replace, ...)
correctness_fix_succeeded     = 4   (scope fix, date backfill, tiktoken, ...)
failed_attempt                = 2   (regex detector, LLM quant-state probe)
```
**Canonicalization worked on the first try** — five distinct read-time experiments folded into ONE
counter instead of fragmenting (the exact failure mode that sank the fuzzy life-event version at
8%). Crisp code-events canonicalize cleanly; that IS the reframe, demonstrated. The primitive's
first output is literally this session's own lesson: `read_time_intervention_failed = 5 -> stop`.

Contrast that decided the reframe: LME object-counting 8% (fuzzy dedup) vs agent-log
canonicalization clean on first try. Prototype: `.claude/jobs/.../quant_state_agent.py`.

## Integration — BUILT (2026-07-02), all 3 increments

1. **✅ In-graph primitive** (`infrastructure/quantstate_repository.py`, adapter-wired) —
   supersedable `(subject, counter) -> value` :Entity, PERSISTENT scope, versioned by value
   (SUPERSEDES + qs_current + expired_at, old kept), `(:Episodic)-[:MENTIONS]->` provenance.
   Commit `000dc93`.
2. **✅ Consolidation writer** (`services/quantstate_consolidator.py`) — episodes -> injected-LLM
   perception -> deterministic fold -> record_counter. Verified on this session's own log
   (read_time_intervention_failed=5, correctness_fix_succeeded=3). Commit `af4ce51`. Fixed a real
   parser bug: `"value": +1` is invalid JSON, was zeroing every extraction.
3. **✅ Recall surfacing** (commit `65119e8`) — retrieval-shaped name (BM25) + name_embedding
   (cosine); a "how often did X fail?" query returns the counter as a first-class fact.
   Fixed the surfacing leak: recall's namespace metadata filter drops nodes with an unset stamped
   `namespace` property (same silent-filter class as the SESSION-scope bug).

**Verified end-to-end:** two paraphrased queries (no literal counter words) both surface the
counters via /api/recall (BM25 2.29, cosine 0.813).

## Remaining (follow-ups, not blocking)

- Schedule the consolidator in the real (non-benchmark) consolidation loop; run explicitly in
  benchmark mode (like promote/backfill).
- `qs_key` schema index (deferred — adding to the required-index list can flip the existing
  graph to schema-not-ready; do it with a migration).
- D0 entropy grade on a counting slice (floor -> 1) as the deterministic win metric.
- Wire a real embedder (settings seam) into the consolidator's `embed` for production use.
