# Remediation Plan: cth.mcp.memory Chunk 2 — Infrastructure (Graph/Neo4j Layer)

**Date:** 2026-06-05
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** `infrastructure/` (33 source files)

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 3 |
| MEDIUM | 11 |
| LOW | 12 |
| **Total** | **27** |

---

## Findings Inventory

### CRITICAL

| ID | File:Line | Description |
|----|-----------|-------------|
| I-18 | `structure_queries.py:485` | `link_episode_to_documents` uses `e.id` instead of `e.uuid` — silently breaks document linking for every episode |

### HIGH

| ID | File:Line | Description |
|----|-----------|-------------|
| I-01 | `embedding_cache.py:85-92` | `EmbeddingCache` module-level singleton lacks thread-safe access — `OrderedDict` ops not atomic |
| I-02 | `paths.py:12-22`, `llama_endpoint.py:9-24` | `os.getenv()` bypasses centralized `settings.py`; `SCHEDULER_URL` hardcodes fallback |
| I-29 | Multiple | 7 infrastructure modules have no dedicated unit tests (`consolidation_queries`, `correlation_queries`, `episode_stamping`, `episode_maintenance`, `pending_actions`, `logging_config`, `paths`) |

### MEDIUM

| ID | File:Line | Description |
|----|-----------|-------------|
| I-03 | `schema.py:18-27 vs 30-62` | `PHASE_ONE_REQUIRED_INDEXES` names don't match generated index names — `phase_one_schema_ready()` never reports ready |
| I-04 | `schema.py:84-190` | Edge backfill runs O(N) on every boot for un-stamped edges |
| I-05 | `structure_queries.py:1323-1332` | f-string for relationship type lacks allowlist validation (Cypher injection surface) |
| I-06 | `todo_repository.py:172-184` | CONCERNS query O(N) scan of all PERSISTENT entities |
| I-08 | `consolidation_queries.py`, `correlation_queries.py` | No dedicated unit tests for complex Cypher queries |
| I-09 | `schema.py:98,125` | `randomUuid()` for missing `session_id` defeats session-scoping |
| I-10 | `graphiti_patches.py` | Version guard has no upper bound; patches may silently break on library upgrades |
| I-14 | `todo_repository.py:100-134` | TEMPORAL node creation duplicates `TemporalRepository` logic (DRY violation) |
| I-19 | `structure_queries.py:678-688` | Blast radius cross-project refs ignore changed file filter |
| I-22 | `project_scanner.py` | `ast.parse()` has no try/except for syntax errors — scanner crashes on bad files |
| I-23 | `pending_actions.py` | SQLite no WAL mode or busy timeout — lock errors under concurrency |
| I-25 | `memory_queries.py` | `cosineSimilarity` on NULL embedding returns NULL not 0 — degrades recall |
| I-26 | `graphiti_client.py` | `add_episode` retries 4xx errors pointlessly |

### LOW

| ID | File:Line | Description |
|----|-----------|-------------|
| I-07 | `scheduler_trace.py:23-24` | `_registered` flag never resets — blocks re-registration |
| I-11 | `neo4j.py` | DCL pattern relies on GIL; not free-threaded-safe |
| I-12 | `telemetry/store.py:1107` | Singleton created at import; DB path stale if env changes |
| I-13 | `temporal_repository.py:54-83` | `create_temporal` omits `source_confidence` and `user_id` |
| I-15 | `__init__.py` | Structure/Temporal/Todo repos not exported from package |
| I-16 | `episode_repository.py` | 3-mixin inheritance relies on implicit `self.neo4j` contract |
| I-17 | `episode_lifecycle.py` | `context_retry_attempts=None` semantics undocumented |
| I-20 | `observability.py` | Langfuse init at module level, no health check |
| I-21 | `logging_config.py` | Hardcoded rotation settings, no settings integration |
| I-24 | `cypher.py` | `processing_substage_started_at = datetime()` on reset is wrong — should be null |
| I-27 | `embedding_dimensions.py` | Dimension inference from single node may miss dimension conflicts |
| I-28 | `structure_queries.py:1186` | Two separate MERGE paths risk divergence |

---

## Phase Plan

### Phase 1: Critical Cypher Fix (CRITICAL — I-18)

**Goal:** Fix `e.id` → `e.uuid` so document linking works.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 1.1 | Change `{id: $episode_id}` to `{uuid: $episode_id}` in `link_episode_to_documents` | `structure_queries.py:485` | I-18 |
| 1.2 | Change `e.id IN $uuids` to `e.uuid IN $uuids` in `get_linked_documents` | `structure_queries.py:505` | I-18 |
| 1.3 | Add test case for `link_episode_to_documents` verifying uuid-based matching | `test_structure_queries.py` | I-18 |

### Phase 2: Thread Safety + Env Var Centralization (HIGH — I-01, I-02)

**Goal:** Fix concurrent access bugs; route all env vars through settings.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 2.1 | Add `threading.Lock` to `EmbeddingCache.get()` and `.set()` | `embedding_cache.py:85-92` | I-01 |
| 2.2 | Add concurrent stress test for `EmbeddingCache` | `test_embedding_cache.py` | I-01 |
| 2.3 | Move `SCHEDULER_URL`, `LLAMA_URL` to `MemorySettings` | `settings.py`, `llama_endpoint.py` | I-02 |
| 2.4 | Move `WORKSPACE_ROOT`, `SCHEDULER_DIR_NAME`, `CTH_MCP_MEMORY_MCP_TELEMETRY_DB` to `MemorySettings` | `settings.py`, `paths.py` | I-02 |
| 2.5 | Remove hardcoded `http://localhost:3456` fallback for `SCHEDULER_URL` | `llama_endpoint.py` | I-02 |

### Phase 3: Schema Bootstrap Fixes (MEDIUM — I-03, I-04, I-09)

**Goal:** Schema bootstrap reports correctly; backfill is efficient and semantically correct.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 3.1 | Fix `PHASE_ONE_REQUIRED_INDEXES` names to match generated index names (e.g., `episode_uuid` → `episodic_uuid_idx`) | `schema.py:18-27` | I-03 |
| 3.2 | Add test asserting every name in `PHASE_ONE_REQUIRED_INDEXES` matches a `CREATE INDEX` name | `test_phase_one_schema.py` | I-03 |
| 3.3 | Change `randomUuid()` to fixed sentinel `'legacy'` for missing `session_id` | `schema.py:98,125` | I-09 |
| 3.4 | Add edge-backfill completion flag to avoid O(N) re-scan on every boot | `schema.py:84-190` | I-04 |

### Phase 4: Query Correctness + Safety (MEDIUM — I-05, I-06, I-14, I-25, I-26)

**Goal:** Cypher queries are correct, safe, and efficient.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 4.1 | Add allowlist validation for `rel_type` in `_write_edges_batch` | `structure_queries.py:1323` | I-05 |
| 4.2 | Optimize CONCERNS query with keyword pre-filter or composite index | `todo_repository.py:172-184` | I-06 |
| 4.3 | Refactor `create_todo` to call `TemporalRepository.create_temporal()` instead of inline Cypher | `todo_repository.py:100-134` | I-14 |
| 4.4 | Add `WHERE n.name_embedding IS NOT NULL` or `coalesce()` to recall scoring queries | `memory_queries.py` | I-25 |
| 4.5 | Fix `add_episode` retry to skip 4xx errors immediately | `graphiti_client.py` | I-26 |
| 4.6 | Fix `processing_substage_started_at = datetime()` → `null` in reset branch | `cypher.py` | I-24 |
| 4.7 | Rename blast radius cross-project refs key or add file filter | `structure_queries.py:678-688` | I-19 |

### Phase 5: Graphiti + Library Compatibility (MEDIUM — I-10)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 5.1 | Add upper bound version guard to graphiti patches: `< (0, 29, 0)` | `graphiti_patches.py` | I-10 |
| 5.2 | Add post-patch validation that patched attributes still exist | `graphiti_patches.py` | I-10 |
| 5.3 | Log warning when patches are skipped due to version mismatch | `graphiti_patches.py` | I-10 |

### Phase 6: Error Handling + Resilience (MEDIUM — I-22, I-23)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 6.1 | Wrap `ast.parse()` in try/except with warning log + empty return | `project_scanner.py` | I-22 |
| 6.2 | Add `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` to `pending_actions.py` SQLite | `pending_actions.py` | I-23 |
| 6.3 | Same WAL/busy_timeout for `telemetry/store.py` | `telemetry/store.py` | I-23 |

### Phase 7: Test Coverage (HIGH — I-29, I-08)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 7.1 | Create `test_consolidation_queries.py` | new file | I-08/I-29 |
| 7.2 | Create `test_correlation_queries.py` | new file | I-08/I-29 |
| 7.3 | Create `test_episode_stamping.py` | new file | I-29 |
| 7.4 | Create `test_episode_maintenance.py` | new file | I-29 |
| 7.5 | Create `test_pending_actions.py` | new file | I-29 |
| 7.6 | Create `test_paths.py` | new file | I-29 |

### Phase 8: LOW Items (deferred)

Items I-07, I-11, I-12, I-13, I-15, I-16, I-17, I-20, I-21, I-27, I-28 are LOW and can be addressed in subsequent sprints.

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Critical Cypher Fix | 3 | CRITICAL | Immediate |
| 2. Thread Safety + Settings | 5 | HIGH | High |
| 3. Schema Bootstrap | 4 | MEDIUM | Medium |
| 4. Query Correctness | 7 | MEDIUM | Medium |
| 5. Graphiti Compatibility | 3 | MEDIUM | Medium |
| 6. Error Handling + Resilience | 3 | MEDIUM | Medium |
| 7. Test Coverage | 6 | HIGH | High |
| 8. LOW Items | deferred | LOW | Low |
| **Total** | **31** (+ 11 deferred) | | |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| I-18 fix changes Cypher behavior | Existing test suite should catch regressions; add dedicated test |
| Thread lock on EmbeddingCache may slow recall path | Lock is uncontended in normal use — minimal overhead |
| Schema bootstrap changes affect production DB | Phase 3 changes are additive (fix naming, add flag) — non-destructive |
| Refactoring todo_repository TEMPORAL creation | Call `create_temporal()` which is well-tested; add integration test |
| Graphiti patch upper bound may block legitimate upgrades | Log warning on skip; patches are opt-in per version range |
