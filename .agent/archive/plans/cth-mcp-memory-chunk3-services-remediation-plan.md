# Remediation Plan: cth.mcp.memory Chunk 3 — Services

**Date:** 2026-06-05
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** `services/` (14 source files), `pipeline/` (1 file)

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 5 |
| MEDIUM | 8 |
| LOW | 6 |
| **Total** | **20** |

---

## Findings Inventory

### CRITICAL

| ID | File:Line | Description |
|----|-----------|-------------|
| S-01 | `ingest_service.py:799` | Undefined variable `project` in `ingest_episode()` — `NameError` at runtime when wiki-linking block is reached |

### HIGH

| ID | File:Line | Description |
|----|-----------|-------------|
| S-02 | `pipeline/__init__.py` | Dead code — entire `pipeline/` package is empty, no imports anywhere |
| S-03 | `services/__init__.py:11-18` | Missing exports: `CorrelationService`, `enrichment_steps`, `enrichment_failures`, `scheduler_tasks`, `scheduler_protocols` not in `__all__` |
| S-04 | `maintenance_scheduler.py:86-95` | Missing scheduler jobs: `apply_decay`, `scan_for_conflicts`, `consolidate_session` — decay/conflict/consolidation only run via manual MCP tool invocation |
| S-05 | `enrichment_steps.py:81` | Naming residue: `yawn_failure_details` attribute — dead code path + stale name |
| S-06 | 38 files across `src/`+`tests/` | Widespread `yawn-memory` naming residue (asyncio task names, server titles, lock files, CLI output, MCP metadata) |

### MEDIUM

| ID | File:Line | Description |
|----|-----------|-------------|
| S-07 | `scheduler_lease.py:26` | `threading.Lock` only covers init, not lease operations — fragile for multi-thread use |
| S-08 | `lifecycle_service.py:852` | Sync `auto_resolve_stale_conflicts` inconsistent with async design |
| S-09 | `ingest_service.py:63-64` | Single `asyncio.Lock` serializes all enrichment — intentional but undocumented bottleneck |
| S-10 | `context_builder.py:254-258` | No path traversal protection on `root_path` file reads |
| S-11 | `lifecycle_service.py:867` | Sync graph adapter calls block event loop |
| S-12 | `recall_service.py:143-144` | Accessing private `_structure` on graph adapter |
| S-13 | `scoring_service.py:54` | `type_boost` unweighted in scoring formula — dominates low-similarity results |
| S-14 | `correlation_service.py:17` | Docstring claims sharpness threshold 0.7, actual is 0.5 |

### LOW

| ID | File:Line | Description |
|----|-----------|-------------|
| S-15 | `maintenance_scheduler.py:242` | All jobs fire simultaneously at startup — burst I/O |
| S-16 | `enrichment_steps.py:791` | `CorrelationService` created per-episode, accesses private `_correlation` |
| S-17 | `scheduler_tasks.py:51-53` | No jitter on exponential backoff — thundering herd risk |
| S-18 | `ingest_service.py:194-198` | `_queued_episode_ids` minor leak on worker cancellation |
| S-19 | `context_builder.py:197-199` | Heuristic mode 50% token budget penalty undocumented/unconfigurable |
| S-20 | `scheduler_protocols.py` | Duplicate protocols for `LifecycleService` — `SchedulerLifecycleService` and `LifecycleServiceProtocol` |

---

## Phase Plan

### Phase 1: Critical NameError Fix (CRITICAL — S-01)

**Goal:** Fix `ingest_episode()` so it doesn't crash on wiki-linking.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 1.1 | Add `project: str | None = None` parameter to `ingest_episode()` | `ingest_service.py:799` | S-01 |
| 1.2 | Update all callers of `ingest_episode()` to pass `project` | `ingest_service.py` callers | S-01 |
| 1.3 | Add test case that exercises the wiki-linking code path | `test_ingest_live.py` or new | S-01 |

### Phase 2: Missing Scheduler Jobs (HIGH — S-04)

**Goal:** Decay, conflict scanning, and consolidation run automatically.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 2.1 | Add `apply_decay` job to `MaintenanceScheduler.__post_init__` with configurable interval | `maintenance_scheduler.py:86-95` | S-04 |
| 2.2 | Add `scan_for_conflicts` job with configurable interval | `maintenance_scheduler.py` | S-04 |
| 2.3 | Add `consolidate_session` / `recover_orphans` job | `maintenance_scheduler.py` | S-04 |
| 2.4 | Add required methods to `LifecycleServiceProtocol` and `SchedulerLifecycleService` | `scheduler_protocols.py` | S-04 |
| 2.5 | Add `scheduler_tasks.py` task wrappers for new jobs | `scheduler_tasks.py` | S-04 |
| 2.6 | Add staggered initial delays to avoid startup burst (also S-15) | `maintenance_scheduler.py:242` | S-04/S-15 |

### Phase 3: Dead Code + Missing Exports (HIGH — S-02, S-03, S-05)

**Goal:** Remove dead code; surface all public APIs.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 3.1 | Delete `pipeline/` directory (empty dead code) | `pipeline/__init__.py` | S-02 |
| 3.2 | Add `CorrelationService` to `services/__all__` | `services/__init__.py` | S-03 |
| 3.3 | Add `enrichment_steps`, `enrichment_failures`, `scheduler_tasks`, `scheduler_protocols` to `__all__` as module exports | `services/__init__.py` | S-03 |
| 3.4 | Remove dead `yawn_failure_details` getattr path in `enrichment_steps.py:81` | `enrichment_steps.py` | S-05 |

### Phase 4: Naming Migration (HIGH — S-06)

**Goal:** `yawn-memory` → `cth-mcp-memory` across all source and test files.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 4.1 | Bulk-rename `yawn-memory` → `cth-mcp-memory` in all asyncio task names | `backend_impl.py`, `ingest_service.py`, `maintenance_scheduler.py`, `runtime.py` | S-06 |
| 4.2 | Rename `yawn-memory` → `cth-mcp-memory` in FastAPI/MCP server titles | `api/server.py`, `mcp/server.py` | S-06 |
| 4.3 | Rename `yawn-memory-scheduler-startup.lock` → `cth-mcp-memory-scheduler-startup.lock` | `scheduler_lease.py` | S-06 |
| 4.4 | Rename `yawn-memory` in CLI output strings | `cli/hook.py`, `cli/output.py` | S-06 |
| 4.5 | Rename `yawn-memory` in MCP resource metadata | `mcp/resources.py` | S-06 |
| 4.6 | Update test assertions from `"yawn-memory"` to `"cth-mcp-memory"` | Multiple test files | S-06 |
| 4.7 | **Note:** HTTP headers (`x-yawn-*`) are wire-protocol and require versioned migration — defer to separate task | `api/`, `mcp/` | S-06 |

### Phase 5: Sync/Async Consistency (MEDIUM — S-08, S-11)

**Goal:** Graph adapter calls don't block the event loop.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 5.1 | Make `auto_resolve_stale_conflicts` async with internal `asyncio.to_thread` | `lifecycle_service.py:852` | S-08/S-11 |
| 5.2 | Update `scheduler_tasks.py` to call it directly instead of wrapping in `asyncio.to_thread` | `scheduler_tasks.py:103` | S-08 |
| 5.3 | Add `asyncio.to_thread` wrappers for other sync graph adapter calls in `lifecycle_service.py` | `lifecycle_service.py` | S-11 |

### Phase 6: API Surface + Scoring (MEDIUM — S-10, S-12, S-13, S-14)

**Goal:** Public API is clean; scoring is documented and controlled.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 6.1 | Add public `resolve_structural_neighbors` method to `MemoryGraphAdapter` | `memory_graph_adapter.py`, `backend_protocol.py` | S-12 |
| 6.2 | Update `recall_service.py` to call public method instead of `._structure` | `recall_service.py:143-144` | S-12 |
| 6.3 | Add path traversal validation in `context_builder.py` — check `root_path` under expected base directory | `context_builder.py:254-258` | S-10 |
| 6.4 | Add `type_boost` weight to `PRESET_WEIGHTS` or cap it relative to similarity | `scoring_service.py:54` | S-13 |
| 6.5 | Fix `correlation_service.py` docstring: sharpness threshold 0.7 → 0.5 | `correlation_service.py:17` | S-14 |
| 6.6 | Document single-flight enrichment lock design (intentional bottleneck) | `ingest_service.py:63-64` | S-09 |

### Phase 7: LOW Items (deferred)

Items S-07, S-16, S-17, S-18, S-19, S-20 are LOW and can be addressed in subsequent sprints. Key actions when picked up:
- S-07: Document single-thread safety guarantee for `SchedulerLeaseStore`
- S-16: Move `CorrelationService` to `EnrichmentContext` parameter
- S-17: Add jitter to `compute_failed_retry_delay_s`
- S-18: Document `_queued_episode_ids` lifecycle; add periodic sweep
- S-19: Make heuristic token penalty factor configurable
- S-20: Merge `SchedulerLifecycleService` and `LifecycleServiceProtocol`

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Critical NameError Fix | 3 | CRITICAL | Immediate |
| 2. Missing Scheduler Jobs | 6 | HIGH | High |
| 3. Dead Code + Exports | 4 | HIGH | High |
| 4. Naming Migration | 7 | HIGH | High |
| 5. Sync/Async Consistency | 3 | MEDIUM | Medium |
| 6. API Surface + Scoring | 6 | MEDIUM | Medium |
| 7. LOW Items | deferred | LOW | Low |
| **Total** | **29** (+ 6 deferred) | | |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| S-01 fix changes `ingest_episode()` signature | Add parameter with default `None` — backward compatible |
| Adding scheduler jobs increases Neo4j load | Configurable intervals; start with conservative defaults (e.g., decay every 6h) |
| Naming migration may break external consumers | `x-yawn-*` HTTP headers deferred; task names are internal |
| Making `auto_resolve_stale_conflicts` async | `asyncio.to_thread` is same pattern as existing callers — no semantic change |
| `type_boost` weight addition changes scoring behavior | Existing presets get `type_boost_weight=0` — no behavior change until configured |
