# Edge Case Testing Gaps

Comprehensive edge case analysis across all layers. Organized by severity, with method references and testability notes. Items marked **[TESTABLE]** can be covered with offline stub tests; **[ONLINE]** require live infrastructure.

> **Note (2026-03-21):** Line numbers below are from the M7 snapshot and may be stale after subsequent refactoring (server.py split, enrichment_steps extraction, telemetry move, episode_repository split, etc.). Use them as approximate pointers; grep for the function name to find current locations.

---

## Critical — Data Loss or Silent Corruption

### 1. Partial failure between Graphiti success and Neo4j stamp **[TESTABLE]**
`ingest_service.py:846-879` — `_stamp_and_finalize()`

If Graphiti returns successfully but `stamp_ingest_metadata()` fails, the episode stays ENRICHING until lease expires. On retry, `_try_reconcile_existing()` may find the artifact — but if artifact lookup also fails, the episode is permanently lost with no record of what Graphiti extracted.

### 2. Rehydrate-during-decay-sweep race **[ONLINE]**
`lifecycle_service.py:574-595` (decay delete phase), `lifecycle_service.py:426-517` (rehydrate_node)

Decay sweep fetches COMPRESSED candidates. Between fetch and deletion, `rehydrate_node()` transitions a node back to ACTIVE. The delete proceeds on the now-ACTIVE node — data loss.

### 3. Compress with None/empty LLM summary **[TESTABLE]**
`lifecycle_service.py:550-554` — compress logic

If `llm.compress_content()` returns None or empty string, `compress_node()` accepts it. Node becomes COMPRESSED with no content — a zombie node invisible to recall but consuming graph space.

### 4. NaN/Inf propagation through scoring pipeline **[TESTABLE]**
`scoring_service.py:40-41, 46, 85, 90`

No `isnan`/`isinf` guards anywhere in scoring. If any input field (similarity, adjacency_score, last_accessed_days_ago, edge_count) is NaN or Inf:
- `math.exp(-lambda * NaN)` → NaN
- `max(0.0, min(1.0, NaN))` → NaN (comparison with NaN always False)
- `sorted([..., NaN, ...], reverse=True)` → undefined ordering
- Final scores silently corrupt recall ranking

### 5. Stale embedding cache after model change **[TESTABLE]**
`observability.py:118-171` — `_CachingEmbeddingsEndpoint.create()`

Cache key is `SHA256(text)` only — no model version. If embedding model changes (e.g., `text-embedding-3-small` → `text-embedding-3-large`), cached vectors have wrong dimensionality. Downstream cosine similarity produces garbage.

### 6. Concurrent episode state updates without CAS **[ONLINE]**
`episode_repository.py:410-456` — `mark_episode_pending()`

Worker A calls `mark_episode_pending()`, Worker B calls `mark_episode_ready()` simultaneously on the same episode. No distributed lock; each method checks `worker_id` independently. Final state is last-write-wins.

---

## High — Silent Failures or Resource Corruption

### 7. Budget-capped episodes silently lost after requeue **[TESTABLE]**
`ingest_service.py:484-502` — `_process_pending_episode()`

When budget is exceeded, episode is requeued via `mark_episode_pending()`. But the episode UUID stays in `_queued_episode_ids` set. When the worker loop picks it up again, it's discarded as "already queued" — episode silently lost.

### 8. LLM callback cleanup leak on early crash **[TESTABLE]**
`ingest_service.py:459, 529-530` — `_process_pending_episode()`

If processing crashes after `set_llm_usage_callback()` but before `reset_llm_usage_callback()`, the callback token leaks. Subsequent episodes reuse the handler, cross-contaminating usage metrics. Memory grows unbounded over long runtime.

### 9. Enrichment lease expires mid-processing **[ONLINE]**
`maintenance_scheduler.py:477-487` — `_run_loop`

Instance A acquires lease, starts enriching. Lease expires (GC pause, network lag). Instance B acquires lease. Now both instances process episodes concurrently — duplicate enrichment, wasted LLM calls, potential state conflicts.

### 10. Negative `last_accessed_days_ago` from future timestamps **[TESTABLE]**
`scoring_service.py:40-41` — recency calculation

If `last_accessed` is in the future (clock skew, timezone bug), `days_ago()` returns negative. `math.exp(-lambda * negative)` → value > 1.0. Clamped to 1.0 by `min(1.0, ...)`, but the node gets maximum recency score unfairly.

### 11. Negative edge weights corrupt adjacency scoring **[TESTABLE]**
`recall_service.py:249-262` — `_compute_adjacency()`

No validation that edge weights are non-negative. A negative weight passes through normalization: `weight / max_adj` could produce negative adjacency_score. `max(0.0, min(1.0, negative))` clamps to 0.0 — node silently loses all adjacency signal.

### 12. LLM returns valid HTTP 200 with wrong JSON schema **[TESTABLE]**
`llm.py:80-110` — `_chat_text()`

If backend returns `{"id": "123"}` (missing `choices[0].message.content`), `create_chat_completion()` may fail internally or return wrong type. No schema validation on response — silent None return.

### 13. Episode text exceeds LLM context window **[TESTABLE]**
`graphiti_client.py:796-950` — `add_episode()`

No truncation before LLM call. If episode_body is 100KB, LLM fails with context-exceeded error. Circuit breaker may not trip (error type not recognized as trip-worthy). Episode marked FAILED with no retry path.

### 14. Circuit breaker state lost on process restart **[ONLINE]**
`circuit_breaker.py:88` — `__init__()`

All breaker state is in-memory. After restart, all breakers reset to CLOSED even if upstream is still down. Causes thundering herd of requests against a broken service.

---

## Medium — Degraded Quality or Unnecessary Work

### 15. `has_conflict` / `conflict_status` mismatch **[TESTABLE]**
`scoring_service.py:50-55`

`has_conflict=True` but `conflict_status=None` (or vice versa) is never tested. Code path at line 50 checks `has_conflict` first, then line 52 checks `conflict_status`. Mismatched flags produce wrong conflict_bonus.

### 16. Self-conflict: node conflicts with itself **[TESTABLE]**
`lifecycle_service.py:280-306` — `_check_contradictions_batch()`

If similarity search returns the node itself at score > 0.85, `set_conflict()` is called with `uuid_a == uuid_b`. The node becomes a self-conflicting singleton group — nonsensical state.

### 17. Transitive conflicts not joined **[TESTABLE]**
`lifecycle_service.py:609-636` — `scan_for_conflicts()`

A≈B (0.88) and B≈C (0.87) creates two separate conflict groups. No transitive closure — A and C are never compared. User sees two groups instead of one.

### 18. Double-resolve returns ValueError instead of graceful response **[TESTABLE]**
`consolidation_queries.py:515-724` — `resolve_conflict_group()`

Resolving an already-resolved group raises `ValueError("No removable members found")` because members already have null `conflict_group_id`. Should return "already resolved" status instead.

### 19. Duplicate UUIDs in similarity search inflate sharpness denominator **[TESTABLE]**
`lifecycle_service.py:234-256` — `_count_similar_nodes()`

Similarity search returning the same UUID twice with different scores double-counts it. `similar_count` is inflated, sharpness artificially lowered → premature compression.

### 20. Mixed batch embedding index ordering **[TESTABLE]**
`observability.py:118-171` — `_CachingEmbeddingsEndpoint.create()`

Batch of 5 texts with mixed cache hits/misses. Miss results fetched from upstream must be spliced back into correct positions. No test verifies ordering is preserved.

### 21. All candidates have identical scores — sort stability **[TESTABLE]**
`scoring_service.py:90` — `sorted(..., reverse=True)`

Python's sort is stable, so original order is preserved for equal scores. But the original order is the Graphiti search order, which may not be meaningful. No tiebreaker (e.g., by UUID or created_at) for deterministic ranking.

### 22. Retry of manually-deleted episode loops forever **[TESTABLE]**
`maintenance_scheduler.py:688-780` — `_retry_process_candidate()`

Episode in FAILED state; node deleted from Neo4j. Scheduler retries, reconciliation finds nothing, enrichment fails, episode stays FAILED, scheduler retries again. No "node deleted" terminal state.

### 23. `keep_uuid` node deleted between scan and resolve **[TESTABLE]**
`consolidation_queries.py:608-625` — `resolve_conflict_group()`

User requests `resolve(keep_uuid="A")`. Between member check and content absorption, node A is deleted. Content fetch returns empty; absorption writes null to non-existent node. Silent data loss.

### 24. Scheduler with all circuit breakers OPEN **[TESTABLE]**
`maintenance_scheduler.py:496-514` — `_run_due_jobs()`

No circuit breaker state check before scheduling jobs. Every tick (1.0s) fires all jobs, all fail immediately against OPEN breakers. Noisy logs, wasted CPU, no backoff.

---

## Low — Boundary Conditions and Cosmetic

### 25. Empty query string to recall **[TESTABLE]**
`recall_service.py:319` — `recall()`

No validation on query input. Empty string passes to Graphiti search — behavior undefined.

### 26. Zero/negative timeout in `wait_for_episode_processing` **[TESTABLE]**
`ingest_service.py:394-412`

Negative timeout clamps deadline to past → loop exits immediately returning None. No error raised.

### 27. Preset weights sum to zero **[TESTABLE]**
`scoring_service.py:31, 59-62`

If all preset weights are 0.0, final_score is always 0.0 for every candidate regardless of input. Valid but likely misconfiguration.

### 28. `_coerce_reference_time` silently replaces bad timestamp with now **[TESTABLE]**
`ingest_service.py:117-126`

Unparseable timestamp falls back to `datetime.now(UTC)`. Changes the episode's temporal placement silently.

### 29. Heartbeat cancel races with heartbeat write **[TESTABLE]**
`ingest_service.py:454-458, 531-534`

Heartbeat task cancelled while mid-write to `touch_episode_processing_heartbeat()`. No transaction wrapping — stale timestamp possible.

### 30. `confirm_contradiction` with identical content on both sides **[TESTABLE]**
`llm.py:160-196`

LLM receives same text twice. May produce unexpected output (not "CONFLICT" or "CLEAR"). Handled by fallback at line 195, but unnecessary LLM call.

---

## Coverage Summary

| Severity | Count | Currently Tested | ONLINE-only |
|----------|-------|-----------------|-------------|
| Critical | 6 | 4 (#1, #3, #4, #5) | 2 (#2, #6) |
| High | 8 | 7 (#7, #8, #10, #11, #12, #13) | 1 (#9) |
| Medium | 10 | 10 (#15–#24) | 0 |
| Low | 6 | 5 (#25–#30 excl #14) | 1 (#14) |
| **Total** | **30** | **26** | **4** |

### Batch 1 — Top 10 (2026-03-21, commit `975c768`)

All 10 implemented in `tests/test_edge_cases.py` (34 tests):

1. **NaN/Inf scoring** (#4) — 6 tests documenting NaN propagation and `_safe_float()` guard
2. **Compress with empty summary** (#3) — 3 tests documenting `not summary` guard
3. **Budget requeue silent loss** (#7) — 1 test confirming finally-block cleanup
4. **Callback cleanup leak** (#8) — 2 tests (cleanup after crash + double-reset raises)
5. **Negative days_ago** (#10) — 2 tests confirming recency clamp to 1.0
6. **has_conflict/conflict_status mismatch** (#15) — 4 tests covering all flag combinations
7. **Self-conflict UUID** (#16) — 1 test confirming `other_uuid == uuid` skip guard
8. **Double-resolve graceful handling** (#18) — 3 tests (re-resolve, nonexistent group, wrong keep_uuid)
9. **Stale embedding cache key** (#5) — 4 tests documenting model-aware cache key
10. **LLM wrong schema** (#12) — 6 tests (empty, whitespace, crash, thinking tags, garbage contradiction)

### Batch 2 — Remaining 16 TESTABLE (2026-03-21)

29 additional tests covering the remaining testable edge cases:

11. **Partial stamp failure / reconcile** (#1) — 2 tests (artifact found → reconcile succeeds, no artifact → returns False)
12. **Negative edge weights** (#11) — 1 test (negative adjacency clamped to 0)
13. **Episode preflight rejection** (#13) — 4 tests (oversized, small, zero limit, empty body)
14. **Transitive conflicts** (#17) — 1 test documenting no transitive closure
15. **Duplicate UUID sharpness inflation** (#19) — 1 test documenting duplicate UUID scoring
16. **Mixed batch embedding ordering** (#20) — 1 test (cache hit/miss position splicing)
17. **Identical score sort stability** (#21) — 1 test (Python stable sort preserves insertion order)
18. **Retry deleted episode** (#22) — 2 tests (empty UUID → terminal, parse error → terminal)
19. **keep_uuid deleted during resolve** (#23) — 1 test (ValueError on missing keep_uuid)
20. **Circuit breaker OPEN state** (#24) — 2 tests (OPEN raises CircuitOpenError, new instance → CLOSED)
21. **Empty recall query** (#25) — 1 test (empty query returns empty results)
22. **Zero/negative timeout** (#26) — 2 tests (zero and negative → immediate return)
23. **Zero weights scoring** (#27) — 2 tests (all-zero weights, one-zero weight)
24. **coerce_reference_time** (#28) — 5 tests (None, string, naive, aware, neo4j datetime)
25. **Heartbeat cancel race** (#29) — 1 test (stop_event causes prompt exit)
26. **Identical content contradiction** (#30) — 2 tests (CLEAR and garbage responses)

### ONLINE-only (4 edge cases, not testable offline)

- #2 — Rehydrate-during-decay-sweep race (requires Neo4j + concurrent transactions)
- #6 — Concurrent episode state updates without CAS (requires distributed Neo4j)
- #9 — Enrichment lease expires mid-processing (requires multi-instance scheduler)
- #14 — Circuit breaker state lost on restart (tested inline: new instance starts CLOSED)
