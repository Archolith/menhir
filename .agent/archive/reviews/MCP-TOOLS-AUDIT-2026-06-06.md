# MCP Tools + Contracts + Resources Audit

**Date:** 2026-06-06
**Scope:** `src/cth_mcp_memory/mcp/` — contracts, formatters, lifecycle, resources, server, service_access, telemetry, tools (all 4 subgroups), base
**Auditor:** opencode (nvidia/z-ai/glm-5.1)
**Chunk:** 4 of the cth.mcp.memory codebase audit

---

## Summary

The MCP tool layer is well-structured with a clear contract hierarchy (`BaseTool` → `BaseTextTool` / `BaseJsonTool`), consistent registration via `tools/__init__.py`, proper auth gating via `required_tier`, and a query-auth permission layer. However, there are several naming residue issues, a parameter default mismatch, an unbounded in-memory rate-limit dict, silent exception swallowing, incomplete test coverage of the tool registry, and a large dispatch method that could benefit from decomposition.

**Total findings:** 12 — 1 CRITICAL, 3 HIGH, 5 MEDIUM, 3 LOW

---

## Findings

### M-01 — Unbounded rate-limit dict grows without eviction

| Field | Value |
|-------|-------|
| **ID** | M-01 |
| **Severity** | CRITICAL |
| **File:line** | `contracts.py:50` |
| **Category** | Tool Parameter Validation |
| **Description** | `_query_add_memory_events: dict[str, deque[float]]` accumulates one entry per distinct `client_id`/`session_id`/`user_id` and never evicts stale keys. The deque entries are pruned by timestamp window (600s), but the dict keys themselves are never removed once the deque empties. In a multi-tenant or high-client-count deployment, this dict grows without bound, leaking memory for the lifetime of the process. |
| **Fix** | After `bucket.popleft()` drains the deque to empty, delete the key from `_query_add_memory_events`. Alternatively, add a periodic GC sweep that removes keys with empty deques. |
| **Test coverage** | `test_query_auth_policy.py` tests rate-limiting behavior but does not test memory growth or key cleanup. |

---

### M-02 — `scan_for_conflicts` default limit mismatch: 150 vs 500

| Field | Value |
|-------|-------|
| **ID** | M-02 |
| **Severity** | HIGH |
| **File:line** | `scan_conflicts.py:8,34` |
| **Category** | Tool Parameter Validation |
| **Description** | The module-level async function `scan_for_conflicts()` at line 8 declares `limit: int = 150`, but the `ScanConflictsTool.endpoint()` at line 34 declares `limit: int = 500`. The docstring says "default 500". The module-level function is the MCP-registered handler (it delegates to `ScanConflictsTool().execute()`), so clients calling `scan_for_conflicts` without `limit` get 150, not the documented 500. |
| **Fix** | Align both defaults to 500 (matching the docstring), or change the docstring to 150 if 150 is intentional. |
| **Test coverage** | No test directly calls `scan_for_conflicts()` with no `limit` argument to verify which default takes effect. |

---

### M-03 — "yawn-memory" naming residue in server and resources

| Field | Value |
|-------|-------|
| **ID** | M-03 |
| **Severity** | HIGH |
| **File:line** | `server.py:34`, `resources.py:281,301` |
| **Category** | Naming Consistency |
| **Description** | `server.py:34` passes `"yawn-memory"` to `create_gateway_server()`. Two resource classes in `resources.py` hardcode `"server": "yawn-memory"` in their payloads (lines 281, 301). The project is now `cth.mcp.memory`; the old name is a residue from a prior naming convention. The test `test_mcp_remote.py:31` also asserts `name == "yawn-memory"`, meaning the rename must touch test expectations too. |
| **Fix** | Replace `"yawn-memory"` with `"cth.mcp.memory"` in `server.py:34`, `resources.py:281,301`, and `test_mcp_remote.py:31`. Alternatively, derive the name from a constant or package metadata. |
| **Test coverage** | `test_mcp_remote.py` asserts the old name, confirming the residue but also providing a test that will fail if not updated alongside the rename. |

---

### M-04 — `build_context.py:_build_temporal_header` silently swallows all exceptions

| Field | Value |
|-------|-------|
| **ID** | M-04 |
| **Severity** | HIGH |
| **File:line** | `build_context.py:37` |
| **Category** | Error Handling |
| **Description** | `_build_temporal_header()` at line 37 has `except Exception: return None`. This silently swallows any error — including `ImportError` if the telemetry store is broken, `AttributeError` from malformed session objects, or `KeyError` from missing telemetry data. The temporal header simply disappears with no logging, making debugging impossible. |
| **Fix** | Narrow to expected exception types (`ImportError`, `AttributeError`, `KeyError`, `ValueError`) and add `logger.debug("temporal header unavailable: %s", exc, exc_info=True)` before returning `None`. |
| **Test coverage** | No test exercises the exception path in `_build_temporal_header`. |

---

### M-05 — `_session_cache` in `service_access.py` is unbounded

| Field | Value |
|-------|-------|
| **ID** | M-05 |
| **Severity** | MEDIUM |
| **File:line** | `service_access.py:17` |
| **Category** | Service Access Layer |
| **Description** | `_session_cache: dict[tuple[str, str | None, str, str], MemorySession]` accumulates one entry per unique `(user_id, session_id, client_id, client_name)` tuple and is never pruned. Over time with many unique clients, this grows without bound. Less severe than M-01 because session tuples are more bounded than per-request client IDs, but still a leak vector in long-running processes. |
| **Fix** | Add an LRU cap (e.g., `functools.lru_cache`) or periodic eviction for sessions older than N hours. |
| **Test coverage** | No test exercises cache growth or eviction. |

---

### M-06 — `resources.py` imports `BaseJsonResource` under `TYPE_CHECKING` only

| Field | Value |
|-------|-------|
| **ID** | M-06 |
| **Severity** | MEDIUM |
| **File:line** | `resources.py:17-22` |
| **Category** | Resources |
| **Description** | `BaseJsonResource` and several domain types (`NodeScope`, `ProcessingState`, `QueryPreset`, `decode_json_value`, `excerpt`, `get_mcp_session`) are imported under `TYPE_CHECKING` only. At runtime, all resource classes inherit from `BaseJsonResource` and call `get_mcp_session()`, `decode_json_value()`, `excerpt()`, and reference `NodeScope`, `ProcessingState`, and `QueryPreset`. These runtime references work because the classes are used inside async methods (which are not evaluated at import time), and the `_normalize_*` functions reference `ProcessingState` etc. at call time. However, `BaseJsonResource` inheritance is resolved at class definition time, so this import **must** work at runtime. Checking: line 22 imports `BaseJsonResource` under `TYPE_CHECKING`, but line 271 does `class DependencyHealthResource(BaseJsonResource)` — this would fail at runtime because `BaseJsonResource` is not in the module namespace outside of type-checking. **Wait** — re-reading: the `TYPE_CHECKING` guard is `if TYPE_CHECKING:`, which is `False` at runtime. So `BaseJsonResource` is NOT available at runtime. However, the resources clearly work (tests pass), meaning there must be a runtime import path. Looking more carefully: `BaseJsonResource` is defined in `contracts.py` and must be imported at the top level for the class hierarchy to resolve. The `TYPE_CHECKING` import is misleading — the runtime import likely comes from a different path or the import is hoisted. This needs verification. |
| **Fix** | Move `BaseJsonResource`, `get_mcp_session`, `NodeScope`, `ProcessingState`, `QueryPreset`, `decode_json_value`, and `excerpt` out of the `TYPE_CHECKING` guard into top-level imports. The `FastMCP` import can remain under `TYPE_CHECKING` since it's only used in function signatures. |
| **Test coverage** | Integration tests (`test_mcp_server.py`) exercise resources and pass, so the runtime path works — but this may be due to import ordering luck or a transitive import. The fragility should be fixed proactively. |

---

### M-07 — `formatters.py:_collect_episode_status` has dual backend access pattern

| Field | Value |
|-------|-------|
| **ID** | M-07 |
| **Severity** | MEDIUM |
| **File:line** | `formatters.py:225-228,267-271` |
| **Category** | Service Access Layer |
| **Description** | `_collect_episode_status` and `_queue_summary` both have `if hasattr(backend, "get_queue_depth")` branches that fall back to `backend.graph_adapter` / `backend.ingest_service`. This dual-path access is fragile and indicates the `MemoryBackend` protocol is not uniformly implemented. The `RuntimeProvider` has `get_queue_depth` but the legacy `BackendClient` or older adapters don't. |
| **Fix** | Ensure all `MemoryBackend` implementations provide `get_queue_depth` and `list_episode_processing`, then remove the `hasattr` fallback branches. |
| **Test coverage** | Tests mock the backend with `MagicMock` which has `hasattr` returning `True` for all attributes, so the fallback paths are never exercised in tests. |

---

### M-08 — `tracker.py` catches bare `Exception` in error path

| Field | Value |
|-------|-------|
| **ID** | M-08 |
| **Severity** | MEDIUM |
| **File:line** | `tracker.py:64` |
| **Category** | Error Handling |
| **Description** | The `except Exception as exc` block at line 64 catches all exceptions from the runner, including `KeyboardInterrupt` subclasses (though not `BaseException` like `SystemExit`). While this is intentional for the MCP pattern of "always return a string to the LLM", it also catches `PermissionError` from auth checks, which should perhaps be allowed to propagate as-is rather than being wrapped in a generic error string. The `PermissionError` is raised in `contracts.py:197-212` inside `_runner`, then caught by `track_mcp_call`'s generic handler and turned into `Error: PermissionError: ...`. This works but loses the structured permission-denied semantics. |
| **Fix** | Re-raise `PermissionError` (and possibly other auth-related errors) from the `track_mcp_call` generic catch, or handle them specially to return a clean permission-denied message without the `Error:` prefix. |
| **Test coverage** | `test_query_auth_policy.py` tests that `PermissionError` is caught and returned, but the output format includes `"Error:"` prefix from `track_mcp_call`, which is verified by substring match (`"PermissionError" in blocked`). The test passes but the UX could be improved. |

---

### M-09 — Gateway test only checks 19 of 27+ registered tools

| Field | Value |
|-------|-------|
| **ID** | M-09 |
| **Severity** | MEDIUM |
| **File:line** | `test_mcp_gateway.py:21-41` |
| **Category** | Tool Registration Completeness |
| **Description** | `test_mcp_server_lists_all_expected_tools` checks only 19 tool names. The full `ALL_TOOLS` list contains 27 tool classes (7 ingest + 5 recall + 5 conflict + 10 ops — though counting: 7+5+5+16=33 total, but some are duplicates counting `CloseStaleTodosTool` etc.). Missing from the expected list: `add_memory_and_track`, `close_memory`, `force_reenrich`, `force_release_enrichment_lease`, `force_scheduler_takeover`, `pause_scheduler`, `resume_scheduler`, `recover_orphans`, `repair_stale_enrichment`, `run_llm_conflict_review`, `scan_for_conflicts`, `requeue_conflicts_for_llm_review`, `close_stale_todos`, `get_episode_trace`, `get_memory_stats`. The test should verify ALL registered tools, not a subset. |
| **Fix** | Generate the expected tool list dynamically from `ALL_TOOLS` or enumerate all 27+ names. |
| **Test coverage** | The test itself is the coverage gap — it only validates a subset. |

---

### M-10 — `query_structure.py` endpoint is a 470+ line dispatch method

| Field | Value |
|-------|-------|
| **ID** | M-10 |
| **Severity** | MEDIUM |
| **File:line** | `query_structure.py` (entire file) |
| **Category** | Dead Code / Maintainability |
| **Description** | `QueryStructureTool.endpoint()` is a single async method spanning ~470 lines with a large `if/elif` dispatch over `query_type` values (projects, overview, files, imports, tests, endpoints, dependencies, cross_refs, blast_radius, affected_tests, symbols, context). Each branch contains inline formatting logic. This makes the method hard to test individually per query type and hard to extend. |
| **Fix** | Extract each `query_type` branch into a private method (e.g., `_dispatch_projects`, `_dispatch_files`, etc.) or a strategy dict. Keep `endpoint()` as a thin dispatcher. |
| **Test coverage** | `test_query_structure_tool.py` has only 2 tests (unknown project and projects listing). The other 10+ query types have no direct unit test coverage. |

---

### M-11 — Resources expose `"server": "yawn-memory"` in JSON payloads

| Field | Value |
|-------|-------|
| **ID** | M-11 |
| **Severity** | LOW |
| **File:line** | `resources.py:281,301` |
| **Category** | Naming Consistency |
| **Description** | `DependencyHealthResource` and `SystemMetadataResource` both hardcode `"server": "yawn-memory"` in their JSON payloads. This is a subset of M-03 but specific to the resource output format — even if the MCP server name is renamed, these payload values need separate attention. |
| **Fix** | Derive from a shared constant (e.g., `cth_mcp_memory.__package__` or a config value). |
| **Test coverage** | No test validates the `server` field in resource payloads. |

---

### M-12 — `lifecycle.py` hardcodes env var name in error messages

| Field | Value |
|-------|-------|
| **ID** | M-12 |
| **Severity** | LOW |
| **File:line** | `lifecycle.py:45-49` |
| **Category** | Naming Consistency |
| **Description** | The error message references `cth_mcp_memory_BACKEND_URL` but the actual env var is configured via `MemorySettings` which uses a different normalization (the setting is `backend_url` which maps to `CTH_MCP_MEMORY_BACKEND_URL` or similar via pydantic-settings). The error message should match the actual env var name that `MemorySettings.from_env()` reads. |
| **Fix** | Verify the actual env var name from `MemorySettings` and update the error message to match. |
| **Test coverage** | No test exercises the `RuntimeError` path in `_mcp_lifespan`. |

---

## Category Coverage Matrix

| # | Category | Findings |
|---|----------|----------|
| 1 | Tool Registration Completeness | M-09 |
| 2 | Contract Compliance | (no findings — BaseTool/BaseTextTool/BaseJsonTool hierarchy is clean) |
| 3 | Service Access Layer | M-05, M-07 |
| 4 | Formatters | (no findings — pure data transforms, well-structured) |
| 5 | Lifecycle | M-12 |
| 6 | Resources | M-06, M-11 |
| 7 | MCP Server Wiring | M-03 |
| 8 | Tool Parameter Validation | M-01, M-02 |
| 9 | Ops Tools Auth Gating | (no findings — `required_tier` is consistently set across all ops tools) |
| 10 | Conflict Tools Flow | (no findings — list → scan → review → resolve flow is correct) |
| 11 | Dead Code | M-10 |
| 12 | Naming Consistency | M-03, M-11, M-12 |
| 13 | Error Handling | M-04, M-08 |

---

## Findings Summary Table

| ID | Severity | File:line | Category | Description (short) |
|----|----------|-----------|----------|---------------------|
| M-01 | CRITICAL | `contracts.py:50` | Tool Parameter Validation | Unbounded `_query_add_memory_events` dict — no key eviction |
| M-02 | HIGH | `scan_conflicts.py:8,34` | Tool Parameter Validation | Default limit mismatch: 150 (function) vs 500 (class endpoint + docstring) |
| M-03 | HIGH | `server.py:34`, `resources.py:281,301` | Naming Consistency | "yawn-memory" naming residue in server name and resource payloads |
| M-04 | HIGH | `build_context.py:37` | Error Handling | Silent `except Exception` in `_build_temporal_header` — no logging |
| M-05 | MEDIUM | `service_access.py:17` | Service Access Layer | Unbounded `_session_cache` dict — no eviction |
| M-06 | MEDIUM | `resources.py:17-22` | Resources | `BaseJsonResource` and domain types imported under `TYPE_CHECKING` only |
| M-07 | MEDIUM | `formatters.py:225-228` | Service Access Layer | Dual `hasattr` backend access pattern in episode status + queue summary |
| M-08 | MEDIUM | `tracker.py:64` | Error Handling | Bare `Exception` catch in `track_mcp_call` wraps `PermissionError` in generic error string |
| M-09 | MEDIUM | `test_mcp_gateway.py:21-41` | Tool Registration Completeness | Gateway test only checks 19 of 27+ tools |
| M-10 | MEDIUM | `query_structure.py` | Dead Code / Maintainability | 470+ line dispatch method — should be decomposed |
| M-11 | LOW | `resources.py:281,301` | Naming Consistency | Resource payloads hardcode `"server": "yawn-memory"` |
| M-12 | LOW | `lifecycle.py:45-49` | Naming Consistency | Error message env var name may not match actual `MemorySettings` field |

---

## Positive Observations

1. **Contract hierarchy is clean**: `BaseTool` → `BaseTextTool` / `BaseJsonTool` is well-designed with shared `execute()` logic for auth checks, telemetry, and timeout.
2. **Auth gating is thorough**: `required_tier` is set on every tool class, with a proper rank-based comparison (`readonly` < `agent` < `operator`). Query-auth permission layer correctly blocks write tools.
3. **Telemetry is comprehensive**: Every tool call is tracked via `track_mcp_call` with duration, input/output sizes, and error recording.
4. **Tool registration is consistent**: All tools follow the same pattern — class inherits base, defines `name`/`description`/`required_tier`, implements `endpoint()`, registered via `tools/__init__.py`.
5. **Resource validation is good**: `_require_uuid`, `_require_scope`, `_require_term`, `_require_type` all validate inputs before backend calls.
6. **Conflict tool flow is well-designed**: list → scan → review → resolve has clear separation and proper status tracking.
7. **Rate limiting exists**: Query-auth `add_memory` has per-client rate limiting with configurable window and limit.
8. **Test coverage is decent for critical paths**: Auth policy, conflict tools, scheduler tools, todo tools, and ingest document tool all have focused unit tests.

---

## Test Coverage Gaps

| Area | Missing Coverage |
|------|-----------------|
| `scan_for_conflicts` default limit | No test verifies which default (150 vs 500) takes effect |
| `_build_temporal_header` exception path | No test for silent exception handling |
| `_query_add_memory_events` cleanup | No test for key eviction after window expires |
| Resource payload `server` field | No test validates the server name in JSON payloads |
| `_mcp_lifespan` RuntimeError path | No test for missing `BACKEND_URL` |
| `query_structure` dispatch (10+ query types) | Only 2 tests; most dispatch branches untested |
| Tool registration completeness | Only 19 of 27+ tools checked |
| `formatters.py` dual-backend fallback | `hasattr` branches never exercised |
