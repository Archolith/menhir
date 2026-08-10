# Remediation Plan: cth.mcp.memory Chunk 4 — MCP Tools + Contracts + Resources

**Date:** 2026-06-06
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** `mcp/` (40 source files)

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 3 |
| MEDIUM | 5 |
| LOW | 3 |
| **Total** | **12** |

---

## Findings Inventory

### CRITICAL

| ID | File:Line | Description |
|----|-----------|-------------|
| M-01 | `contracts.py:50` | Unbounded `_query_add_memory_events` dict — no key eviction; memory leak in long-running processes |

### HIGH

| ID | File:Line | Description |
|----|-----------|-------------|
| M-02 | `scan_conflicts.py:8,34` | Default limit mismatch: function declares 150, class endpoint declares 500, docstring says 500 |
| M-03 | `server.py:34`, `resources.py:281,301` | `yawn-memory` naming residue in server name and resource payloads |
| M-04 | `build_context.py:37` | Silent `except Exception` in `_build_temporal_header` — no logging, impossible to debug |

### MEDIUM

| ID | File:Line | Description |
|----|-----------|-------------|
| M-05 | `service_access.py:17` | Unbounded `_session_cache` dict — no eviction |
| M-06 | `resources.py:17-22` | `BaseJsonResource` and domain types imported under `TYPE_CHECKING` only — fragile |
| M-07 | `formatters.py:225-228` | Dual `hasattr` backend access pattern — protocol not uniformly implemented |
| M-08 | `tracker.py:64` | Bare `Exception` catch wraps `PermissionError` in generic error string |
| M-09 | `test_mcp_gateway.py:21-41` | Gateway test only checks 19 of 27+ registered tools |
| M-10 | `query_structure.py` | 470+ line dispatch method — should be decomposed per query_type |

### LOW

| ID | File:Line | Description |
|----|-----------|-------------|
| M-11 | `resources.py:281,301` | Resource payloads hardcode `"server": "yawn-memory"` (subset of M-03) |
| M-12 | `lifecycle.py:45-49` | Error message env var name may not match actual `MemorySettings` field |

---

## Phase Plan

### Phase 1: Memory Leak Fix (CRITICAL — M-01)

**Goal:** Rate-limit dict evicts stale keys.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 1.1 | After `bucket.popleft()` drains deque to empty, `del _query_add_memory_events[key]` | `contracts.py:50` | M-01 |
| 1.2 | Add periodic GC sweep: remove keys with empty deques every 60s | `contracts.py` | M-01 |
| 1.3 | Add test: verify key count stabilizes after burst of unique client IDs | `test_query_auth_policy.py` | M-01 |

### Phase 2: Parameter Default Alignment + Naming (HIGH — M-02, M-03)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 2.1 | Align `scan_for_conflicts()` default limit to 500 (match docstring and endpoint) | `scan_conflicts.py:8` | M-02 |
| 2.2 | Replace `"yawn-memory"` with `"cth-mcp-memory"` in `server.py:34` | `server.py` | M-03 |
| 2.3 | Replace `"server": "yawn-memory"` with `"server": "cth-mcp-memory"` in `resources.py:281,301` | `resources.py` | M-03/M-11 |
| 2.4 | Update `test_mcp_remote.py:31` and `test_mcp_server.py` assertions | test files | M-03 |
| 2.5 | Derive server name from a shared constant (e.g., `cth_mcp_memory.__package__`) | `server.py`, `resources.py` | M-03/M-11 |

### Phase 3: Error Handling (HIGH — M-04, MEDIUM — M-08)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 3.1 | Narrow `except Exception` in `_build_temporal_header` to expected types + add `logger.debug` | `build_context.py:37` | M-04 |
| 3.2 | Add test exercising exception path in `_build_temporal_header` | new test | M-04 |
| 3.3 | Re-raise `PermissionError` from `track_mcp_call` or handle specially with clean message | `tracker.py:64` | M-08 |

### Phase 4: Cache + Import Fragility (MEDIUM — M-05, M-06)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 4.1 | Add LRU cap to `_session_cache` (e.g., `functools.lru_cache` or manual maxsize=256) | `service_access.py:17` | M-05 |
| 4.2 | Move `BaseJsonResource`, `get_mcp_session`, domain types out of `TYPE_CHECKING` guard | `resources.py:17-22` | M-06 |
| 4.3 | Keep `FastMCP` under `TYPE_CHECKING` (only used in signatures) | `resources.py` | M-06 |

### Phase 5: Protocol + Test + Maintainability (MEDIUM — M-07, M-09, M-10)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 5.1 | Ensure all `MemoryBackend` implementations provide `get_queue_depth` and `list_episode_processing` | `backend_protocol.py`, implementations | M-07 |
| 5.2 | Remove `hasattr` fallback branches in `formatters.py` once protocol is uniform | `formatters.py:225-228` | M-07 |
| 5.3 | Generate expected tool list dynamically from `ALL_TOOLS` in gateway test | `test_mcp_gateway.py:21-41` | M-09 |
| 5.4 | Decompose `QueryStructureTool.endpoint()` into per-query-type private methods | `query_structure.py` | M-10 |
| 5.5 | Add unit tests for each query_type dispatch branch | `test_query_structure_tool.py` | M-10 |

### Phase 6: LOW Items (deferred)

M-12 (lifecycle.py env var name) — verify actual env var name from `MemorySettings` and update error message.

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Memory Leak Fix | 3 | CRITICAL | Immediate |
| 2. Parameter + Naming | 5 | HIGH | High |
| 3. Error Handling | 3 | HIGH + MEDIUM | High |
| 4. Cache + Import | 3 | MEDIUM | Medium |
| 5. Protocol + Test + Maintainability | 5 | MEDIUM | Medium |
| 6. LOW Items | deferred | LOW | Low |
| **Total** | **19** (+ 1 deferred) | | |
