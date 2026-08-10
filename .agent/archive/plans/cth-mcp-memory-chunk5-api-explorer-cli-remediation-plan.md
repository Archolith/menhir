# Remediation Plan: cth.mcp.memory Chunk 5 — API + Explorer + CLI

**Date:** 2026-06-06
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** `api/`, `explorer/`, `cli/` (18 source files)

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 2 |
| MEDIUM | 8 |
| LOW | 6 |
| **Total** | **17** |

---

## Findings Inventory

### CRITICAL

| ID | File:Line | Description |
|----|-----------|-------------|
| A-04 | `explorer/app.py:410-427` | Explorer has zero auth middleware; all routes + Neo4j data are unauthenticated |

### HIGH

| ID | File:Line | Description |
|----|-----------|-------------|
| A-02 | `server.py:118-125` | CORS defaults to `["*"]` wildcard with no production warning |
| A-14 | `explorer/app.py:301-352` | Graph traversal queries have no LIMIT; can freeze browser on large graphs |

### MEDIUM

| ID | File:Line | Description |
|----|-----------|-------------|
| A-01 | Multiple (17 locations) | `yawn-memory` naming residue across API, CLI, and test files |
| A-03 | `mcp_remote.py:12-17 vs 29-33` | Docstrings contradict: "full parity" vs "tool-only"; function name `create_remote_tool_only_mcp` is misleading |
| A-06 | `output.py:29-55` | Turn counter writes are non-atomic; concurrent hooks can lose increments |
| A-09 | `auth.py:71-74`, `routes.py:89-90` | `x-yawn-*` header names instead of `x-cth-mcp-memory-*` — wire-protocol breaking change |
| A-10 | `routes.py:390` | `RuntimeError` for unknown backend operation returns 500 instead of 400 |
| A-11 | `routes.py:326-384` | `_BACKEND_METHODS` allowlist can drift from actual `MemoryBackend` protocol |
| A-13 | `explorer/app.py:303-327` | f-string Cypher for variable-depth path (safe today but fragile) |
| A-16 | `auth.py:129-133` | Query-string API key leaks into access logs, browser history, proxy logs |

### LOW

| ID | File:Line | Description |
|----|-----------|-------------|
| A-05 | `_backend_context.py:36-38` | Hardcoded 10s timeout and internal path; not configurable |
| A-07 | `hook.py:41-50` | Top-level exception handler swallows all errors with zero logging |
| A-08 | `explorer/static/explorer.css` | Empty CSS file loaded on every page |
| A-12 | `request_context.py:40-42` | Redundant `isinstance` after `setdefault` — confusing but correct |
| A-15 | `bootstrap.py:50-77` | Full-path failure logged at DEBUG only; no actionable diagnosis for user |
| A-17 | `server.py:132-136` | `routes.insert(0, ...)` for streamable HTTP — fragile, undocumented |

---

## Phase Plan

### Phase 1: Explorer Auth (CRITICAL — A-04)

**Goal:** Explorer requires authentication before accessing Neo4j data.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 1.1 | Add `BearerAuthMiddleware` (or read-only variant) to Explorer app | `explorer/app.py:410-427` | A-04 |
| 1.2 | Add `CTH_MCP_MEMORY_EXPLORER_API_KEY` env var support | `settings.py`, `explorer/app.py` | A-04 |
| 1.3 | Bind Explorer to `127.0.0.1` only by default; document MUST NOT be publicly exposed | `explorer/app.py` | A-04 |
| 1.4 | Audit VPS deployment config: confirm Explorer port is not publicly reachable | VPS config | A-04 |
| 1.5 | Add test: verify Explorer routes return 401 without auth | `test_explorer_app.py` | A-04 |

### Phase 2: Security Hardening (HIGH — A-02, A-14, MEDIUM — A-16)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 2.1 | Change CORS default from `["*"]` to `["http://localhost:5173", "http://localhost:8787"]` | `server.py:118-125` | A-02 |
| 2.2 | Add startup warning when CORS is wildcard AND api_key is non-empty | `server.py` | A-02 |
| 2.3 | Add `LIMIT 200` to node queries and `LIMIT 300` to edge queries in `_graph_elements()` and `_session_graph_elements()` | `explorer/app.py:301-352` | A-14 |
| 2.4 | Add `max_nodes` query parameter with default 100 | `explorer/app.py` | A-14 |
| 2.5 | Add Cytoscape JS warning overlay when graph exceeds 500 nodes | `explorer.js` | A-14 |
| 2.6 | Document query-string API key risk in `architecture.md`; add startup warning | `auth.py:129-133` | A-16 |
| 2.7 | Ensure access log format does not log query strings for `/mcp*` paths | `logging_config.py` | A-16 |

### Phase 3: Naming Migration (MEDIUM — A-01, A-09)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 3.1 | Replace all `yawn-memory` with `cth-mcp-memory` in API/CLI source (17 locations) | `mcp_remote.py`, `server.py`, `hook.py`, `bootstrap.py`, `_backend_context.py` | A-01 |
| 3.2 | Update test assertions in `test_mcp_remote.py`, `test_mcp_server.py`, `test_cli_hook.py` | test files | A-01 |
| 3.3 | Accept both `x-yawn-*` and `x-cth-mcp-memory-*` header prefixes in `auth.py` | `auth.py:71-74` | A-09 |
| 3.4 | Add `x-cth-mcp-memory-*` support in `routes.py:89-90` | `routes.py` | A-09 |
| 3.5 | Document canonical header names and deprecation timeline in `endpoints.md` | `.agent/endpoints.md` | A-09 |
| 3.6 | Log deprecation warning when `x-yawn-*` headers are used | `auth.py` | A-09 |

### Phase 4: MCP Remote Consistency (MEDIUM — A-03)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 4.1 | Rename `create_remote_tool_only_mcp()` to `create_remote_mcp()` | `mcp_remote.py:12` | A-03 |
| 4.2 | Update `create_mcp_sse_app()` docstring to match actual behavior | `mcp_remote.py:29-33` | A-03 |
| 4.3 | Decide: should remote MCP expose resources? If yes, update all docstrings. If no, remove `register_memory_resources()`. | `mcp_remote.py` | A-03 |

### Phase 5: API Route Correctness (MEDIUM — A-10, A-11, A-13)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 5.1 | Replace `RuntimeError` with `HTTPException(status_code=400)` for unknown backend operations | `routes.py:390` | A-10 |
| 5.2 | Add test asserting `_BACKEND_METHODS` is a subset of `MemoryBackend` methods | `test_api_routes.py` | A-11 |
| 5.3 | Add cross-reference comment between `_BACKEND_METHODS` and `endpoints.md` | `routes.py:326-384` | A-11 |
| 5.4 | Add comment explaining why f-string Cypher is safe in Explorer (integer-only, bounded depth) | `explorer/app.py:303-327` | A-13 |

### Phase 6: CLI Robustness (MEDIUM — A-06, LOW — A-05, A-07, A-15)

| # | Task | Files | Severity |
|---|------|-------|----------|
| 6.1 | Use atomic write for turn counter: temp file + `os.replace()` | `output.py:29-55` | A-06 |
| 6.2 | Add `logger.debug` to hook.py top-level exception handler | `hook.py:41-50` | A-07 |
| 6.3 | Add WARNING-level log + exception message in stderr for bootstrap failures | `bootstrap.py:50-77` | A-15 |
| 6.4 | Make `BackendContextBuilder` timeout configurable via parameter | `_backend_context.py:36-38` | A-05 |

### Phase 7: LOW Items (deferred)

A-08 (empty CSS), A-12 (redundant isinstance), A-17 (routes.insert fragility) — address in subsequent sprints.

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Explorer Auth | 5 | CRITICAL | Immediate |
| 2. Security Hardening | 7 | HIGH + MEDIUM | High |
| 3. Naming Migration | 6 | MEDIUM | High |
| 4. MCP Remote Consistency | 3 | MEDIUM | Medium |
| 5. API Route Correctness | 4 | MEDIUM | Medium |
| 6. CLI Robustness | 4 | MEDIUM + LOW | Medium |
| 7. LOW Items | deferred | LOW | Low |
| **Total** | **29** (+ 3 deferred) | | |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Explorer auth may break existing local-only workflows | Add `CTH_MCP_MEMORY_EXPLORER_AUTH_ENABLED=false` env var to disable auth for local dev |
| CORS default change may break existing clients | New default includes common local dev ports; document the env var for remote deployments |
| `x-cth-mcp-memory-*` headers are a wire-protocol change | Phase 3.3 adds dual-accept with deprecation warning; old headers work during transition |
| `_BACKEND_METHODS` test may be brittle across refactors | Generate from `inspect` or use a protocol decorator; test fails fast on drift |
| Explorer LIMIT may truncate useful data | `max_nodes` parameter lets users increase; default 200 is generous for Cytoscape |
