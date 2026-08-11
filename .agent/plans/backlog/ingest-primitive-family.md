# Write-time primitive family already hiding in ingest

> **Read this through the Event→Fold→View lens** ([`.agent/architecture.md`](../../architecture.md)):
> the items below are **Event sources and candidate Folds**, NOT ten node types to build. There is
> one View node shape; "primitives" are mostly folds we haven't written yet. Resist primitive explosion.

**Status: INVENTORY + build order (2026-07-02).** Survey of primitive-shaped things ingest ALREADY
records, framed by the question "what can we promote before we have to re-ingest?" Companion to
the archived [`quantstate-agent-counter.md`](../../archive/plans/quantstate-agent-counter.md)
(D1 implementation record) and
[`aggregation-as-consolidation.md`](../../archive/plans/aggregation-as-consolidation.md)
(historical query-sufficient-state thesis).

**Key finding:** most of these need NO re-ingest — ingest already writes the data, siloed in the
telemetry SQLite store (`failure_events`, `episode_task_events`, `lifecycle_events`,
`memory_revisions`, `conflict_resolutions`) or as node/edge stamps. Promoting a primitive =
surfacing existing data as recallable state, not re-ingesting.

## Already-real primitives (data exists today)

1. **ProcessingState** — episodes move PENDING→ENRICHING→READY→FAILED with leases, attempts,
   heartbeats, retry recovery. An Agent-Work-Item primitive. On the Episodic node + telemetry.
2. **ProcessingAttemptCounter** — `processing_attempts`, retry ceilings, context-window retries,
   budget requeues. A recurrence counter already in the pipeline — a natural QuantState seed.
3. **FailureEvent** — `telemetry.failure_events` table: `operation, episode_uuid, failure_stage,
   classification, retryable, processing_attempt, queue_depth, worker_id, error_type, error,
   details_json`, indexed on `(operation, recorded_at)`. Structured, foldable, provenance-linked.
   **Promote before a generic counter — counters fold over FailureEvents.**
4. **SourceStamp** — session_id, user_id, source, source_confidence, namespace on every
   episode/node/edge. Substrate for provenance-aware views. (See the "stamp like ingest" rule.)
5. **BeliefCommitContext** — best-effort (commit_sha, branch) from repo context at ingest. Truth
   primitive for code memory: "this belief was learned at commit X on branch Y."

## Near-primitives worth promoting

6. **EdgeFactProvenance** — synthetic edge-fact repair stamps facts original|llm_repaired|
   synthetic_fallback. An AssertionQuality primitive (trust/repair/low-confidence decisions).
7. **CodeEvidenceAnchor** — ANCHORED_TO links memory→file with weight (narrative 1.0, diff-only
   0.3). More than metadata.
8. **SimilarityDisposition** — new nodes routed related|conflict|merged by similarity thresholds.
   Decides novel vs related vs review-worthy vs duplicate.
9. **MemoryRevision** — `telemetry.memory_revisions`: field, old_value, new_value, changed_by,
   episode_uuid. Seed of a MemoryChangeLog primitive.

## Not yet

- Budget counters — operational telemetry, not a cognitive primitive (unless agents should reason
  about cost/failure behavior).
- Queue depth — operational, not memory.
- Preflight oversized rejection — a FailureEvent subtype, not standalone.

## The family

`ProcessingState · FailureEvent · AttemptEvent · SourceStamp · BeliefCommitContext ·
EdgeFactProvenance · CodeEvidenceAnchor · SimilarityDisposition · MemoryRevision · QuantState`

## Build order — FailureEvent → QuantState next (the MVP)

The clean stack, and the most agent-behavior-useful:
```
ingest already records  -> failure_events (SQLite, structured, indexed)   [EXISTS]
                           |  GROUP BY operation/error_type                [deterministic, no LLM]
QuantState fold         -> counter node (graph, recallable)               [BUILT: record_counter]
                           |
"enrichment has failed 3 times on <operation>"                            [surfaces in recall]
```
This is **cleaner than the QuantState-from-prose path we built**: events are already TYPED, so the
fold is a SQL GROUP BY — no perception/LLM step, deterministic, provenance-preserving via
episode_uuid. Directly changes agent behavior: "don't keep doing the thing that has already failed."

**Next build:** a `FailureEvent → QuantState` bridge (read telemetry.failure_events, fold, write
counters via the existing record_counter). No re-ingest, no LLM. Then it surfaces in recall like
the QuantState counters already proven to.

## MVP cut (decided 2026-07-02)

Ran the whole inventory through the MVP filter (must: change agent behavior directly · reuse the
View shape + a deterministic fold, no new node type · no re-ingest · data already recorded). Only
one other primitive clears it.

**Near-term stack:**
1. **✅ FailureEvent → QuantState** — "this failed 3 times" → stop repeating failed actions. BUILT.
2. **✅ MemoryRevision → InstabilityCounter** — "this belief was corrected 4 times" → lower
   confidence / ask before asserting. A NEW behavior lever (volatility, not repetition) on the
   SAME machinery: telemetry.memory_revisions (node_uuid, field, old/new_value, changed_by,
   episode_uuid, indexed) → SQL GROUP BY field → record_counter → supersedable View → recall.
   BUILT (`services/instability_counter_bridge.py`, commit `f8dd8ab` — status note 2026-08-08,
   curator audit; was marked "NEXT BUILD" here, stale).
3. **✅ Timeline View** — architecture-validation track: proved View(kind) generalizes BEYOND
   counters (a second View KIND, not a second counter). Dropped into the same node shape;
   **earned + made the `QuantState → View(kind)` rename** — machinery now in a generic
   `ViewRepository`, `QuantStateRepository` is an alias. The one thing that flexed: the value slot
   (scalar `view_value` for counter, ordered `view_payload` for timeline). Recall needed no
   per-kind code — both surface through the same stamped-`:Entity` path. See
   `.agent/architecture.md` and `.agent/data_models.md`. BUILT.

**Deferred, with reasons (unchanged):**
- ProcessingAttemptCounter — redundant with FailureEvent (same counter, same event).
- SimilarityDisposition — write-time routing decision, not a recallable View. Wrong layer.
- EdgeFactProvenance / CodeEvidenceAnchor — fact TRUST/quality, a different product surface from
  agent EXPERIENCE counters. Real, separate thesis.
- BeliefCommitContext / SourceStamp — substrate that enriches the Event (scopes future folds, e.g.
  "failed on branch X"), not a standalone View. A dimension on counters later, not an MVP primitive.
- ProcessingState — operational health ("what's stuck"), niche; not experience.

Rule intact: **Events are preserved. Views are additive. Reconcile/supersession is where
correctness bugs live.**
