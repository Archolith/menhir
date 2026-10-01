---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- #171 source-memory section: pass the #168 latency limit

Project Scope: `Archolith/menhir`, branch `perf/171-source-memory-latency`, base `origin/main` `1f6eb392`.

## Why
Measured on the LME oracle copy (195 q x 2, sequential): recall p50 147 ms off -> 188 ms on (+28%,
#168 limit 3 is +15%); p95 unchanged. The section's phase is ~31 ms, all of it the episode search:
`memory_queries.search_episode_embeddings` filters every `:Episodic` node with
`tenant_scope_cypher` (`coalesce(n.namespace, n.group_id, '') IN $tenant_namespaces`), which cannot
use an index. The query embedding is already a cache hit (Graphiti's vector search embeds the same
text first). `:Episodic(namespace)` and `:Episodic(group_id)` RANGE indexes already exist.

## Behavior (contract)
1. **Index-friendly prefilter (no result change).** In `src/menhir/domain/namespace.py`, next to
   `tenant_scope_cypher`, add `tenant_scope_prefilter_cypher(variable: str = "n") -> str` returning
   `({v}.namespace IN $tenant_namespaces OR {v}.group_id IN $tenant_namespaces)` (same identifier
   validation as `tenant_scope_cypher`; same `$tenant_namespaces` parameter from
   `tenant_scope_params`). Docstring: it is a SUPERSET used only to let the planner seek the existing
   namespace/group_id indexes; it never replaces `tenant_scope_cypher`, which still decides membership.
   Callers must only use it when the namespace is scoped (params not None).
2. In `infrastructure/memory_queries.py::search_episode_embeddings` (and ONLY there): when
   `namespace` is scoped, put the prefilter FIRST in the WHERE list, keep every existing predicate
   (including `tenant_scope_cypher("n")`) unchanged. Unscoped (namespace None) is unchanged.
   Do NOT hand-write any `.namespace`/`.group_id` comparison in memory_queries.py (the CF-127 guard
   `tests/test_cf127_tenancy_scope_predicate.py` counts them per file).
3. **Concurrency.** In `services/recall_pipeline.py::run_recall`, start the section as an asyncio
   task right after the vector-search phase (`_t_phases["vector_search"]` is set, ~line 529):
   `source_memory_task = asyncio.create_task(_build_source_memories(...same args as today...))`,
   but only when the effective limit is > 0 and include_session is True (otherwise skip the task and
   use `(None, None, 0)` exactly as the helper would). At the current await site (~line 1530) replace
   the call with `await source_memory_task`. The `content_query_vector` argument must be whatever is
   known at task start (it is set in the content-vector branch before line 529; pass it if set, else
   None -- the helper embeds via the cache). Wrap the span between task creation and the await in
   `try/finally` that cancels the task (and suppresses CancelledError) if the function exits early or
   raises before awaiting it, so no orphan task survives. Results, notes, trace, and every return path
   must be identical to today.
4. No other behavior changes. No schema changes (the indexes exist).

## Tests (new file `tests/test_source_memory_latency_171.py`; do not edit existing assertions)
- `tenant_scope_prefilter_cypher` text contains both `namespace IN $tenant_namespaces` and
  `group_id IN $tenant_namespaces`; rejects a non-identifier variable like `tenant_scope_cypher` does.
- `search_episode_embeddings` with a namespace: the query contains the prefilter AND still contains
  `tenant_scope_cypher("n")`; without a namespace the prefilter is absent.
- run_recall starts the lane before the metadata/adjacency phases: with a stub graph adapter whose
  `search_episode_embeddings` records a timestamp and a stub whose metadata fetch records one, the
  search starts before metadata fetch completes (or assert via an asyncio.Event ordering) -- keep it
  simple and deterministic.
- an exception raised after task creation and before the await leaves no pending task
  (`asyncio.all_tasks()` check) and the original exception propagates.
- results identical: flag on, same stubs, `source_memories`/`results`/note equal to the sequential
  behavior expected by existing tests in tests/test_source_memory_lane.py (those must keep passing).
- the CF-127 guard test still passes.

## Test policy
Executor: `uv run pytest -q tests/test_source_memory_latency_171.py tests/test_source_memory_lane.py
tests/test_recall_history.py tests/test_cf127_tenancy_scope_predicate.py tests/test_recall_service.py
tests/test_content_vector_retrieval.py` and ruff on changed files; at most once more after a fix.
No full suite. No commit.

## Non-goals
No vector index, no change to ranking, results, limits, or the section's content; no other queries.
