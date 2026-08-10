# menhir — temporal-preserving bulk ingest (nice-to-have optimization)

## Status

backlog / nice-to-have — NOT scheduled. An ingest-throughput optimization for large
batch loads (e.g. the LongMemEval Mode-B build). The shipped single-episode path is
correct and is what the in-flight LME enrichment uses; this is purely a speedup for
future bulk loads. Do not start while a run is in flight.

## Context (what the research settled, 2026-06-30)

Per-episode enrichment averages ~13.5 LLM calls (max 88), dominated by per-edge
`extract_attributes` (date extraction). At `MENHIR_INGEST_CONCURRENCY=8` that's ~8/min →
~18h for the ~8k-episode LME build. We investigated whether a cheaper ingest exists that
keeps temporal fidelity (the thing LongMemEval scores — Graphiti/Zep 63.8% vs Mem0 49%).

Findings:
- **A temporal-preserving bulk already exists.** `Graphiti.add_episode_bulk` in BOTH our
  `graphiti-core 0.28.2` and upstream `0.29.2` runs `resolve_extracted_edges` **per
  episode** inside `_resolve_nodes_and_edges_bulk` (0.28.2 `graphiti.py:712`, 0.29.2
  `:904`) — the same dates + invalidation primitive as single `add_episode`. The
  0.28.2 docstring saying bulk "does not perform edge invalidation or date extraction"
  is **stale** (PR getzep/graphiti#1476 rewrote bulk to share primitives; 0.29.2 removed
  the docstring). Issue #1489 is NOT a bulk-temporal gap (it's MCP `reference_time`,
  which menhir already handles, + delete-episode orphan cleanup).
- Mem0-style engines ingest ~86% faster but drop temporal structure → wrong trade for
  this benchmark. `SEMAPHORE_LIMIT` (graphiti env, default 20) is a separate, cheap,
  temporal-safe lever (within-episode call parallelism) worth trying independently.
- Upstream `getzep/graphiti` (0.29.2) cloned to `projects/forked/graphiti` for
  source analysis + patching.

## The one real gap

`add_episode_bulk` takes **one `group_id` per call** and resolves all the batch's
episodes' edges **concurrently against the pre-batch graph** (a single `semaphore_gather`
over episodes). So **edges within one batch never invalidate each other** — intra-batch
supersession does not fire. Each LongMemEval question is its own namespace and its
knowledge-updates happen *within* that session (turn 5 supersedes turn 1), so batching a
namespace's turns as-is would **miss intra-session supersession → regress the
knowledge-update question category** (exactly the temporal edge we want to keep).

## GO / NO-GO gate (do this first, ~1 session in the fork)

Before any menhir work, verify the win is real:
- Instrument `extract_nodes_and_edges_bulk` in `projects/forked/graphiti` to confirm it
  merges multiple episodes into **fewer** extraction LLM calls, vs merely gathering
  per-episode extraction concurrently. If it's only concurrent gather (same call count),
  the throughput gain is modest (better parallelism, which `SEMAPHORE_LIMIT` also buys) →
  reconsider / prefer the `SEMAPHORE_LIMIT` lever instead.
- Measure calls/episode + wall time for a small namespace via bulk vs single on a
  throwaway graph.

## The graphiti fork patch (if GO)

In `projects/forked/graphiti` `graphiti_core/graphiti.py::_resolve_nodes_and_edges_bulk`:
resolve edges **sequentially in `valid_at` order** instead of one concurrent
`semaphore_gather` — each episode's `resolve_extracted_edges` sees prior (same-batch)
episodes' just-persisted edges, so intra-batch supersession fires. Keep node extraction +
dedup batched (the cheap win). This is the ATOM shape: batch the cheap part, order the
temporal part.
- Test: a batch with "user lives in Boston" (t1) then "user moved to Denver" (t5) in one
  `add_episode_bulk` call → the Boston edge ends up `invalid_at`-stamped, Denver current.
- Package the patch as a menhir-vendored fork or an upstream PR.

## menhir-side changes (if GO)

| # | Change | Why |
|---|---|---|
| 1 | Batching layer: accumulate PENDING per namespace (chronological), call `add_episode_bulk` per (namespace, batch) | worker is per-episode today |
| 2 | `claim_pending_episodes_batch` (N per namespace, atomic leases) + batch stale-recovery | `claim_pending_episode` is single |
| 3 | Batch state machine PENDING→ENRICHING→READY; partial-failure handling (bulk raises → batch retries) | per-episode today |
| 4 | Worker-loop/queue rework: enqueue namespace-batches (IngestGate per-namespace fits: 1 bulk call = 1 gate acquire) | queue is per-UUID |
| 5 | Map `AddBulkEpisodeResults` → each episode row (`resolved_episode_uuid`, nodes/edges, `mark_episode_ready`) | single-result today |
| 6 | Build `list[RawEpisode]` with per-episode `reference_time` (backdating/`occurred_at`), chronological order | reference_time already captured |
| 7 | Per-batch budget/telemetry (`processing_llm_tasks_attempt`, `MAX_LLM_CALLS_PER_JOB`) | per-episode today |
| 8 | Test surface: `test_services_pipeline` / edge-cases assume single-episode | add bulk coverage |

## Non-goals

```text
- do NOT run this against the current LME build (single-episode path is correct + in flight)
- do NOT switch memory engines (Mem0 etc.) — loses the temporal accuracy the benchmark scores
- keep a fallback to single add_episode for small namespaces / correctness-critical loads
```

## References

- `projects/forked/graphiti` (upstream 0.29.2) — patch target
- graphiti-core 0.28.2 (installed): `graphiti.py` `add_episode_bulk` (1037), `_resolve_nodes_and_edges_bulk` (623, `resolve_extracted_edges` at 712)
- getzep/graphiti PR #1476 (bulk shares per-episode primitives), Issue #1489 (temporal backfill — reference_time + delete cleanup)
- ATOM (arXiv 2510.22590) — batched atomic-fact decomposition + temporal resolution (the conceptual model)
- Related memory: retraced ingest mechanics + ingest-alternatives research (2026-06-30)
