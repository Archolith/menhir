# API + Explorer + CLI Audit — `src/cth_mcp_memory/{api,explorer,cli}/`

**Date:** 2026-06-06
**Auditor:** OpenCode (z-ai/glm-5.1)
**Scope:** All files under `src/cth_mcp_memory/api/` (7 files), `src/cth_mcp_memory/explorer/` (6 files incl. templates/static), `src/cth_mcp_memory/cli/` (6 files), plus 8 test files under `tests/`
**Categories:** Auth consistency, routes vs endpoints.md, MCP remote correctness, request context binding, Explorer session limits/Cytoscape, CLI hook/bootstrap/output, error handling, dead code, naming residue (`yawn_memory`/`yawn-memory`), security

---

## Findings

### A-01 — `yawn-memory` naming residue across 17 locations in API, CLI, and tests

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `mcp_remote.py:21,50`; `server.py:37,45,49,151`; `hook.py:156,229,265,321,338,365,382,389,397`; `bootstrap.py:77`; `_backend_context.py:1`; `tests/test_mcp_remote.py:31`; `tests/test_mcp_server.py:519,548,1161`; `tests/test_cli_hook.py:1,522` |
| **Category** | Naming residue |
| **Description** | The FastMCP server name, log messages, CLI docstrings, hook install/uninstall user-facing strings, and test assertions all still reference `yawn-memory` instead of `cth-mcp-memory` (or `cth.mcp.memory`). The package was renamed to `cth_mcp_memory` but these strings were never updated. This creates user confusion (e.g., `hook install` prints "Installed yawn-memory hooks...") and makes the codebase appear inconsistent. The MCP server name is visible to MCP clients and appears in tool metadata. |
| **Fix** | Replace all `yawn-memory` with `cth-mcp-memory` in API/CLI source. Update test assertions accordingly. For the FastMCP `name=` parameter, use `"cth-mcp-memory"`. Update `server.py` title and log messages. Update `hook.py` docstrings and `typer.echo` messages. Update `_backend_context.py` module docstring. |
| **Existing test** | `test_mcp_remote.py:31` asserts `name == "yawn-memory"` — must be updated; `test_mcp_server.py` asserts `server == "yawn-memory"`; `test_cli_hook.py:522` asserts "2 yawn-memory hook(s)" |

---

### A-02 — CORS defaults to `["*"]` (allow all origins) with no warning

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **File** | `server.py:118-125` |
| **Category** | Security |
| **Description** | When `cth_mcp_memory_CORS_ORIGINS` env var is empty or unset, CORS falls back to `allow_origins=["*"]` with `allow_methods=["*"]` and `allow_headers=["*"]`. Combined with the bearer auth middleware, this means any origin can make authenticated cross-origin requests if they possess a valid API key. For a service designed to be exposed via tunnels/proxies (per `mcp_remote.py:52` docstring), a wildcard CORS policy is dangerous — it allows any malicious webpage to proxy requests through a victim's browser if they have a stored token. |
| **Fix** | (a) Default to `["http://localhost:5173", "http://localhost:8787"]` (local dev only) instead of `["*"]`. (b) Add a startup warning log when CORS is set to wildcard in production (when `api_key` is non-empty). (c) Document the env var in `.agent/architecture.md` and `.env.example`. |
| **Existing test** | `test_api_server.py` (if it exists) — not checked for CORS coverage |

---

### A-03 — MCP remote docstrings contradict each other on resource exposure

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `mcp_remote.py:12-17` vs `mcp_remote.py:29-33` |
| **Category** | MCP remote correctness |
| **Description** | `create_remote_tool_only_mcp()` (line 12) docstring says "Exposes all tools and resources — full parity with the stdio surface". But `create_mcp_sse_app()` (line 29) docstring says "Remote MCP is tool-only by design." The `create_remote_tool_only_mcp()` function calls both `register_all_tools()` and `register_memory_resources()`, so it IS exposing resources. But `create_mcp_streamable_http_app()` (line 39) creates a SEPARATE `FastMCP` instance (not using `create_remote_tool_only_mcp()`) that also calls both `register_all_tools()` and `register_memory_resources()`. The function name `create_remote_tool_only_mcp` is misleading — it's not tool-only. And the SSE docstring is wrong — it delegates to `create_remote_tool_only_mcp()` which registers resources. |
| **Fix** | (a) Rename `create_remote_tool_only_mcp()` to `create_remote_mcp()` since it's not tool-only. (b) Update `create_mcp_sse_app()` docstring to say "Delegates to `create_remote_mcp()` which exposes tools and resources." (c) If the design intent IS tool-only, remove `register_memory_resources()` from both functions and update `create_remote_tool_only_mcp` docstring to match. Decide the actual design intent and make code + docs consistent. |
| **Existing test** | `test_mcp_remote.py` — checks name but not resource registration parity |

---

### A-04 — Explorer has no auth middleware; all routes are unauthenticated

| Field | Value |
|---|---|
| **Severity** | CRITICAL |
| **File** | `explorer/app.py:410-427` |
| **Category** | Auth consistency |
| **Description** | `create_app()` in the Explorer builds a bare `FastAPI` instance with no auth middleware. All explorer routes — including `/explorer/api/graph/{uuid}`, `/explorer/api/session/{session_id}`, and all partial HTML endpoints — are accessible without any authentication. The Explorer directly creates a `Neo4jRepository` with database credentials and runs arbitrary Cypher queries. If the Explorer is exposed on a network-accessible port (even via the VPS), anyone can browse, search, and read all memory graph data. The main API server (`server.py`) wraps its app in `BearerAuthMiddleware`, but the Explorer is a completely separate FastAPI app. |
| **Fix** | (a) Add `BearerAuthMiddleware` (or a read-only variant) to the Explorer app. (b) At minimum, add a simple API key check as an environment-gated feature (e.g., `cth_mcp_memory_EXPLORER_API_KEY`). (c) If the Explorer is intentionally local-only, bind to `127.0.0.1` only and document that it MUST NOT be exposed on public ports. (d) The VPS deployment config should be audited to confirm the Explorer port is not publicly reachable. |
| **Existing test** | `test_explorer_app.py` — tests routes with stub repo but does not test auth |

---

### A-05 — `_backend_context.py` hardcodes 10s timeout and internal endpoint path

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `_backend_context.py:36-38` |
| **Category** | CLI bootstrap |
| **Description** | `BackendContextBuilder.build_context()` uses `httpx.AsyncClient(timeout=10.0)` and hardcodes the path `/api/internal/backend/build_context`. The 10s timeout is not configurable and may be too short for large context recall queries. The path is fragile — if the internal backend prefix changes in `routes.py:24`, this will silently break. |
| **Fix** | (a) Add a `timeout` parameter to `BackendContextBuilder.__init__()` with default 10.0. (b) Derive the path from the same constant (`_INTERNAL_BACKEND_PREFIX`) used in `routes.py`, or at minimum add a comment linking to the canonical definition. (c) Consider making the path configurable via settings. |
| **Existing test** | None — `BackendContextBuilder` has no dedicated test file |

---

### A-06 — Turn counter file writes are not atomic; race condition on concurrent hook invocations

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `output.py:29-55` |
| **Category** | CLI output |
| **Description** | `should_run_this_turn()` reads the JSON counter file, increments, prunes, and writes it back with `counter_path.write_text(json.dumps(data))`. If two hook processes run concurrently (e.g., UserPromptSubmit and PostCompact firing simultaneously), they can both read the same file, increment, and overwrite each other's changes — causing lost increments and incorrect frequency gating. The `try/except pass` on write (line 51-55) silently swallows write failures. |
| **Fix** | (a) Use atomic write: write to a temp file in the same directory, then `os.replace()` to atomically rename. (b) Alternatively, use `fcntl.flock()` (Unix) or `msvcrt.locking()` (Windows) for file-level locking. (c) The `except Exception: pass` on write should at minimum log the failure at DEBUG level. |
| **Existing test** | `test_cli_hook.py` — tests `should_run_this_turn` basic logic but not concurrency |

---

### A-07 — `hook.py:run()` catches all exceptions silently

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `hook.py:41-50` |
| **Category** | Error handling |
| **Description** | The `run()` command wraps the entire dispatch in `try/except Exception`, printing an empty `wrap_hook_response()` on failure. The comment says "Never crash in hook mode — let Claude Code proceed", which is the correct design intent for a hook that should never block the editor. However, the exception is completely swallowed — no logging, no stderr output. If the hook fails due to a misconfiguration (bad Neo4j credentials, missing env vars), the user gets zero feedback that memory recall is silently disabled. |
| **Fix** | Add `logger.debug("Hook run failed", exc_info=True)` inside the except block. For known config errors (e.g., Neo4j connection refused), print a one-line warning to stderr. Keep the "never crash" contract but add observability. |
| **Existing test** | `test_cli_hook.py` — tests output formatting but not the top-level error handling |

---

### A-08 — `explorer.css` is empty — no custom styles loaded

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `explorer/static/explorer.css` |
| **Category** | Dead code |
| **Description** | `explorer.css` is a 1-line empty file. The Explorer HTML templates include a `<link>` tag for it, so it's loaded on every page but contributes nothing. Either custom styles were planned but never added, or the file is vestigial from an earlier design. |
| **Fix** | (a) If styles are needed, add them. (b) If not, remove the file and the `<link>` tag in `base.html`. A 404 for a CSS file is harmless but an empty file loaded on every request is a wasted HTTP round-trip. |
| **Existing test** | `test_explorer_app.py` — does not test CSS file presence |

---

### A-09 — `auth.py` uses `x-yawn-*` header names instead of `x-cth-mcp-memory-*`

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `auth.py:71-74` |
| **Category** | Naming residue |
| **Description** | `_request_session_headers()` reads `x-yawn-user-id`, `x-yawn-session-id`, `x-yawn-client-id`, `x-yawn-client-name` from request headers. These header names are part of the public API contract (consumers must set them), so renaming them would be a breaking change. However, the naming is inconsistent with the package rename to `cth_mcp_memory`. The `yawn-memory` name in the MCP server metadata (A-01) compounds this — clients that set `x-yawn-*` headers are implicitly coupled to the old name. |
| **Fix** | (a) Accept both old and new header prefixes: `x-yawn-*` (legacy) and `x-cth-mcp-memory-*` (canonical). (b) Document the canonical header names in `endpoints.md` and `architecture.md`. (c) Add a deprecation timeline: support both for N months, then drop `x-yawn-*`. (d) Update `routes.py:89-90` which also reads `x-yawn-user-id` and `x-yawn-session-id` directly. |
| **Existing test** | `test_api_auth.py` — tests auth middleware but may use old header names in assertions |

---

### A-10 — `routes.py:backend_invoke` uses `RuntimeError` instead of `HTTPException` for unknown operations

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `routes.py:390` |
| **Category** | Error handling |
| **Description** | `backend_invoke()` raises `RuntimeError(f"Unknown backend operation: {operation}")` when the operation name is not in `_BACKEND_METHODS`. Since this is inside a FastAPI route, the `RuntimeError` will be caught by the generic `Exception` handler in `server.py:99-116`, which returns a 500 error. An unknown operation is a client error (bad request), not a server error. The correct response is 400 Bad Request or 404 Not Found. |
| **Fix** | Replace `raise RuntimeError(...)` with `raise HTTPException(status_code=400, detail=f"Unknown backend operation: {operation}")`. Alternatively, return 404 since the operation "endpoint" doesn't exist. |
| **Existing test** | `test_api_routes.py` — may or may not test unknown operation error code |

---

### A-11 — `_BACKEND_METHODS` allowlist in `routes.py` may drift from actual backend protocol

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `routes.py:326-384` |
| **Category** | Routes vs endpoints.md |
| **Description** | `_BACKEND_METHODS` is a hardcoded `set` of 56 method names that must be kept in sync with `MemoryBackend` protocol methods and `RuntimeProvider` implementation. There is no automated check that this allowlist matches the actual backend interface. If a new method is added to `MemoryBackend` but not to `_BACKEND_METHODS`, the internal backend route will reject it with a misleading `RuntimeError`. Conversely, if a method is removed from `MemoryBackend` but stays in the allowlist, `backend_invoke` will pass the allowlist check then fail with an `AttributeError` when calling `getattr(backend, operation)`. The allowlist also includes methods not documented in `endpoints.md` (which explicitly says internal backend routes are "not part of the public OpenAPI contract"). |
| **Fix** | (a) Add a test that asserts `_BACKEND_METHODS` is a subset of the methods defined on `MemoryBackend` / `RuntimeProvider`. (b) Generate the allowlist from `inspect.get_members()` of the backend class at startup, filtered by a naming convention or decorator. (c) Add a comment in `routes.py` and `endpoints.md` cross-referencing each other. |
| **Existing test** | `test_api_routes.py` — tests some routes but not the allowlist exhaustiveness |

---

### A-12 — `request_context.py:40-41` redundant `isinstance(state, dict)` check after `setdefault`

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `request_context.py:40-42` |
| **Category** | Error handling |
| **Description** | `_ensure_scope_request_id()` calls `scope.setdefault("state", {})` which guarantees `state` is a dict (it either returns the existing value or inserts `{}`). The subsequent `if isinstance(state, dict):` check is therefore always True and is dead code. This is defensive against a hypothetical scenario where another middleware sets `scope["state"]` to a non-dict, but `setdefault` wouldn't replace it in that case — it would return the non-dict value, making the `isinstance` check actually meaningful. So the code is correct but confusing: `setdefault` doesn't guarantee a dict result if another caller already set `state` to something else. |
| **Fix** | (a) Add a comment explaining the edge case: "Another middleware may have set scope['state'] to a non-dict; skip injection in that case." (b) Alternatively, use `state = scope.get("state", {}); if isinstance(state, dict): scope["state"] = state; state["request_id"] = request_id` for clarity. |
| **Existing test** | None for this specific edge case |

---

### A-13 — Explorer Cypher queries in `app.py` use f-strings for variable-depth paths

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `explorer/app.py:303-327` |
| **Category** | Security |
| **Description** | `_graph_elements()` constructs Cypher queries using f-strings: `f"MATCH (seed {{uuid: $uuid}}) OPTIONAL MATCH p=(seed)-[*1..{clamped_depth}]-(neighbor)"`. While `clamped_depth` is derived from `max(1, min(depth, 2))` and is always an integer 1 or 2, the pattern of f-string Cypher construction is a code smell that could become a vulnerability if the depth validation is relaxed or removed in the future. The `uuid` parameter is properly parameterized via `$uuid`, which is correct. |
| **Fix** | (a) Keep the `max(1, min(depth, 2))` clamp but add a comment explaining why f-string is safe here (integer-only, bounded). (b) Alternatively, use a CASE expression or two pre-built query strings for depth=1 and depth=2. |
| **Existing test** | `test_explorer_app.py` — tests app routes but not Cypher injection surface |

---

### A-14 — Explorer graph queries have no result size limits for large graphs

| Field | Value |
|---|---|
| **Severity** | HIGH |
| **File** | `explorer/app.py:301-352` |
| **Category** | Explorer session limits / Cytoscape |
| **Description** | `_graph_elements()` performs a variable-depth traversal `MATCH p=(seed)-[*1..{depth}]-(neighbor)` with no `LIMIT` clause. On a densely connected graph, a depth-2 traversal from a hub node could return thousands of nodes and edges. The result is fed directly into Cytoscape without pagination. Similarly, `_session_graph_elements()` has no limit — a session with hundreds of nodes could produce a massive payload. Cytoscape's fcose layout (used in `explorer.js`) can freeze the browser on graphs with >500 nodes. |
| **Fix** | (a) Add `LIMIT 200` to node queries and `LIMIT 300` to edge queries in both `_graph_elements()` and `_session_graph_elements()`. (b) Add a `max_nodes` query parameter with a sensible default (100). (c) In the Cytoscape JS, add a warning overlay when the graph exceeds a visual threshold. (d) Consider server-side pagination or a "load more" button. |
| **Existing test** | `test_explorer_app.py` — tests routes but not result size behavior |

---

### A-15 — `bootstrap.py` catches all exceptions in full-path setup with only debug logging

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `bootstrap.py:50-77` |
| **Category** | CLI bootstrap |
| **Description** | `build_hook_services()` wraps the Graphiti/RecallService/ContextBuilderService construction in a bare `except Exception:` with `logger.debug(...)`. If the full path fails and the backend URL fallback also fails, the user sees only `print("yawn-memory hook: full recall unavailable, flagged-only mode", file=sys.stderr)`. The debug log is only visible if logging is configured. This makes it very difficult to diagnose why context recall is failing — the user just sees "flagged-only mode" with no actionable information. |
| **Fix** | (a) Log the exception type and message at WARNING level, not DEBUG. (b) Include the exception message in the stderr output: `f"yawn-memory hook: full recall unavailable ({exc}), flagged-only mode"`. (c) Consider a `--verbose` flag for the hook that enables full traceback output. |
| **Existing test** | None for `build_hook_services` error paths |

---

### A-16 — `auth.py` query-string API key leaks into server logs and browser history

| Field | Value |
|---|---|
| **Severity** | MEDIUM |
| **File** | `auth.py:129-133` |
| **Category** | Security |
| **Description** | For MCP paths (`/mcp/`, `/mcp-http`), the auth middleware accepts `?api_key=` as a query parameter fallback when no `Authorization` header is present. The code sanitizes the query string from the ASGI scope (line 138-141), which prevents downstream code from seeing the key. However, the raw query string with the API key is still visible in: (a) web server access logs that log the full URL, (b) browser history if accessed from a browser, (c) any proxy/tunnel logs. The query-string path exists because "connectors that can't set headers" need auth, but it's a significant security trade-off. |
| **Fix** | (a) Document this risk in `architecture.md` and the MCP remote section. (b) Add a startup warning when `api_key` is non-empty and query-string auth is enabled. (c) Consider a separate short-lived token endpoint that exchanges a bearer token for a temporary cookie. (d) At minimum, ensure the access log format in `build_logging_config()` does not log query strings for `/mcp*` paths. |
| **Existing test** | `test_api_auth.py` — tests query auth but does not test log sanitization |

---

### A-17 — `server.py:136` inserts streamable HTTP route at index 0 — fragile and undocumented

| Field | Value |
|---|---|
| **Severity** | LOW |
| **File** | `server.py:132-136` |
| **Category** | MCP remote correctness |
| **Description** | The streamable HTTP mount inserts a Starlette `Route` at `app.routes.insert(0, ...)` to avoid "sub-app host header issues". This bypasses FastAPI's routing system and relies on implementation details of Starlette's route list. If FastAPI changes its internal route storage (e.g., switches to a router tree), this will break silently. The comment explains the "why" but not the "what happens if" scenario. |
| **Fix** | (a) Add a more detailed comment explaining why `mount()` doesn't work for streamable HTTP (host header mismatch) and what the insert(0) achieves. (b) Add a test that verifies the `/mcp-http` route is reachable and returns the expected response. (c) Consider filing a FastMCP upstream issue for proper ASGI mounting of streamable HTTP. |
| **Existing test** | `test_mcp_server.py` — tests MCP server but the streamable HTTP route insertion may not be covered |

---

## Summary Table

| ID | Severity | Category | File(s) | Description |
|----|----------|----------|---------|-------------|
| A-01 | MEDIUM | Naming residue | `mcp_remote.py`, `server.py`, `hook.py`, `bootstrap.py`, `_backend_context.py`, 3 test files | 17 locations still reference `yawn-memory` instead of `cth-mcp-memory` |
| A-02 | HIGH | Security | `server.py:118-125` | CORS defaults to wildcard `["*"]` with no production warning |
| A-03 | MEDIUM | MCP remote correctness | `mcp_remote.py:12-17,29-33` | Docstrings contradict: "full parity" vs "tool-only" — function name is misleading |
| A-04 | CRITICAL | Auth consistency | `explorer/app.py:410-427` | Explorer has zero auth; all routes + Neo4j data are unauthenticated |
| A-05 | LOW | CLI bootstrap | `_backend_context.py:36-38` | Hardcoded 10s timeout and internal path; not configurable |
| A-06 | MEDIUM | CLI output | `output.py:29-55` | Turn counter writes are non-atomic; concurrent hooks can lose increments |
| A-07 | LOW | Error handling | `hook.py:41-50` | Top-level exception handler swallows all errors with zero logging |
| A-08 | LOW | Dead code | `explorer/static/explorer.css` | Empty CSS file loaded on every page |
| A-09 | MEDIUM | Naming residue | `auth.py:71-74`, `routes.py:89-90` | `x-yawn-*` header names instead of `x-cth-mcp-memory-*` |
| A-10 | MEDIUM | Error handling | `routes.py:390` | `RuntimeError` for unknown backend operation returns 500 instead of 400 |
| A-11 | MEDIUM | Routes vs endpoints | `routes.py:326-384` | `_BACKEND_METHODS` allowlist can drift from actual `MemoryBackend` protocol |
| A-12 | LOW | Error handling | `request_context.py:40-42` | Redundant `isinstance(state, dict)` after `setdefault` — confusing but correct |
| A-13 | MEDIUM | Security | `explorer/app.py:303-327` | f-string Cypher for variable-depth path (safe today but fragile) |
| A-14 | HIGH | Explorer limits | `explorer/app.py:301-352` | Graph traversal queries have no LIMIT; can freeze browser on large graphs |
| A-15 | LOW | CLI bootstrap | `bootstrap.py:50-77` | Full-path failure logged at DEBUG only; user gets no actionable diagnosis |
| A-16 | MEDIUM | Security | `auth.py:129-133` | Query-string API key leaks into access logs, browser history, proxy logs |
| A-17 | LOW | MCP remote correctness | `server.py:132-136` | `routes.insert(0, ...)` for streamable HTTP — fragile, undocumented |

**Critical:** 1 | **High:** 2 | **Medium:** 8 | **Low:** 6
