# Menhir M2 — Compound Correctness, Security, Architecture, Performance, Maintainability, Test-Coverage, LLM/AI and Compliance Audit

**Date:** 2026-08-13  
**Auditor:** Antigravity (Gemini 3.7 Flash)  
**Repository:** https://github.com/Archolith/menhir  
**Target Commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9` (HEAD: `0e6fbb4ed2e889ac6cb1f102c007f6ce23ba1d08`, zero diff in `src/menhir/api/`)  
**Scope:** All 24 files in `src/menhir/api/` (5,565 lines total)  
**Mode:** READ-ONLY functional and architectural audit  
**Executable Probe:** `.agent/audit/m2_functional_probe.py`  

---

## 1. Executive Summary

I performed a comprehensive audit across all **24 files and 5,565 lines** in `src/menhir/api/`. Every file was fully read and mechanically analyzed; line totals reconcile exactly with the 5,565-line scope (see §8).

### Highest-Risk Finding: High/Critical — Phase 3 Reset Tier Bypass Allows Agent-Tier Namespace Destruction
- **Location:** [`src/menhir/api/routes_handlers.py:173-196`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes_handlers.py#L173-L196) via [`src/menhir/api/routes.py:727-743`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes.py#L727-L743) (`POST /api/phase3/reset`).
- **Mechanism:** `phase3_reset_impl` enforces only `agent` tier (`require_tier("agent")`), but immediately dispatches to `backend.delete_namespace(namespace)` and `adapter.purge_turn_evidence(namespace)`.
- **Impact:** In Menhir's authorization governance model, namespace deletion is strictly an `operator`-tier destructive operation (`DELETE /api/namespace/{namespace}` at `routes.py:605` requires `operator`, `_OP_TIER_OPERATOR` at `routes_support.py:639` requires `operator`, and MCP `delete_namespace` requires `operator`). The `POST /api/phase3/reset` route provides an unmitigated privilege escalation vector allowing any client with `agent`-tier access to completely destroy any non-default namespace silo and all associated turn evidence.

### Other Primary Findings Summary:
1. **Security / Info Disclosure (Medium):** `routes_handlers.py:226` in `backend_invoke_impl` logs the unredacted request `body` with `logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)` on failure, leaking raw episodic memories, diffs, and sensitive user text into server logs.
2. **Security / Error Handling (Low):** `routes_handlers.py:213` raises a bare `RuntimeError` when `operation not in backend_methods`, causing FastAPI's unhandled exception handler to log an exception trace and return HTTP 500 `InternalServerError` instead of HTTP 404 or 422.
3. **Security / Tier Inconsistency (Low):** Seven read routes (`/api/recall`, `/api/context`, `/api/bootstrap/flagged`, `/api/bootstrap/context`, `/api/stats`, `/api/phase3/status`, `/api/views`) do not enforce `_require_tier("readonly")` in their route handler bodies, relying entirely on the ASGI middleware, whereas four other read routes explicitly enforce it.
4. **Performance / Concurrency (Medium):** 13 synchronous blocking `sqlite3.connect` operations execute directly on the async event loop without `asyncio.to_thread` across `auth.py`, `routes_handlers.py`, `oauth_as_register.py`, `oauth_authorize.py`, and `oauth_token.py`. Under `CLIENT_TOKEN` mode, every single authenticated request performs synchronous SQLite disk I/O on the main loop thread.
5. **Maintainability / DRY & Dead Code (Low):** Identical duplicate function implementations exist across files (`_settings_for` in `oauth_authorize.py:94` vs `oauth_token.py:24`; `new_client_id` in `client_token_store.py:19` vs `oauth_client_store.py:11`), along with unused imports in `oauth.py`, `oauth_as_register.py`, `oauth_authorize.py`, `oauth_preflight.py`, and `routes.py`.
6. **Maintainability / Comment Rot (Low):** `mcp_remote.py:86` docstring claims "Remote MCP is tool-only by design", while lines 79 and 110 explicitly register memory resources via `register_memory_resources(remote_mcp)`.

---

## 2. Findings by Audit Type

### A1. Functional Correctness

#### FINDING A1-1: Unhandled `RuntimeError` on Unknown Backend Operation in `backend_invoke`
- **Severity:** Low
- **File & Line:** [`src/menhir/api/routes_handlers.py:212-214`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes_handlers.py#L212-L214)
- **Trace:**
  ```python
  if operation not in backend_methods:
      raise RuntimeError(f"Unknown backend operation: {operation}")
  ```
- **Impact:** When a caller requests an unsupported operation via `POST /api/internal/backend/{operation}`, raising `RuntimeError` bypasses standard HTTP error handling. It is caught by `_unhandled_exception_handler` in `server_support.py:160`, emitting an unhandled exception log with traceback and returning HTTP 500 instead of HTTP 404 (Not Found) or 422 (Unprocessable Entity).
- **Fix:** Replace `raise RuntimeError(...)` with `raise HTTPException(status_code=404, detail=f"Unknown backend operation: {operation}")`.

---

### A2. Security

#### FINDING SEC-1: Privilege Escalation — `POST /api/phase3/reset` Allows `agent` Tier to Delete Namespaces
- **Severity:** High / Critical
- **File & Line:** [`src/menhir/api/routes_handlers.py:173-196`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes_handlers.py#L173-L196) and [`src/menhir/api/routes.py:727-743`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes.py#L727-L743)
- **Trace:**
  ```python
  async def phase3_reset_impl(
      request: Request,
      namespace: str,
      *,
      require_tier: Callable[[str], None],
      try_record_destructive_op_rest: Callable[[str], None],
      require_phase3_adapter: Callable[[Request], tuple[Any, Any]],
      get_backend: Callable[[Request], Any],
  ) -> Phase3ResetResponse:
      require_tier("agent")  # <--- GATED AT AGENT TIER
      try_record_destructive_op_rest("phase3_reset")
      _, adapter = require_phase3_adapter(request)
      backend = get_backend(request)
      try:
          deleted = await backend.delete_namespace(namespace)  # <--- OPERATOR-TIER OPERATION
  ```
- **Impact:** `delete_namespace` is classified as `_OP_TIER_OPERATOR` (`routes_support.py:639`), `DELETE /api/namespace/{namespace}` (`routes.py:606`) requires `operator`, and MCP `delete_namespace` requires `operator`. Gating `POST /api/phase3/reset` at `agent` tier creates a backdoor where an agent-tier token can purge any namespace in the database.
- **Fix:** Change `require_tier("agent")` in `phase3_reset_impl` to `require_tier("operator")`.

#### FINDING SEC-2: Inconsistent Route-Level Tier Checks Across Read Endpoints
- **Severity:** Low
- **File & Line:** [`src/menhir/api/routes.py:122, 200, 228, 284, 660, 698, 711`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes.py#L122)
- **Trace:**
  - `POST /api/recall`, `POST /api/context`, `GET /api/bootstrap/flagged`, `POST /api/bootstrap/context`, `GET /api/stats`, `GET /api/phase3/status`, `GET /api/views` have no `_require_tier` call in the endpoint body.
  - In contrast, `GET /api/scalar-authority/{view_uuid}/contributors` (`:184`), `GET /api/tool-events/dirty` (`:524`), `GET /api/tool-events/stale` (`:539`), and `GET /api/tool-events/stale-verifications` (`:584`) explicitly call `_require_tier("readonly")`.
- **Impact:** In standard static or OAuth auth modes, the ASGI middleware authenticates and binds tier so the endpoints are protected. However, if internal middleware dispatch semantics change or if an internal router is remounted without `BearerAuthMiddleware`, endpoints without `_require_tier` lack defense-in-depth enforcement.
- **Fix:** Add `_require_tier("readonly")` consistently to all read endpoint handler bodies.

---

### A3. Architecture

#### FINDING ARCH-1: Dense Cross-Layer Coupling in Shared Support Modules
- **Severity:** Low / Architectural Advisory
- **File & Line:** [`src/menhir/api/routes_support.py`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes_support.py) and [`src/menhir/api/server_support.py`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/server_support.py)
- **Trace:**
  - `api` package imports from `config` (25 statements), `mcp` (12 statements), `infrastructure` (11 statements), `domain` (7 statements), `core` (5 statements), `services` (3 statements), and `explorer` (1 statement).
  - 24 private-symbol imports (`_name`), 20 of which cross package boundaries (e.g. `_drain_background_errors <- menhir.core.backend_impl`, `_normalize_reader_id <- menhir.mcp.formatters`, `_remember_flagged_bootstrap_read <- menhir.mcp.lifecycle`).
- **Impact:** High blast radius for changes in `routes_support.py` (710 lines) and `server_support.py` (241 lines). Private symbols imported from `menhir.mcp` and `menhir.core` couple the HTTP API directly to internal MCP formatter and lifecycle implementation details.
- **Fix:** Promote shared helpers (`_normalize_reader_id`, `_remember_flagged_bootstrap_read`, etc.) to public symbols in appropriate domain/core packages.

---

### A4. Maintainability

#### FINDING MAINT-1: Code Duplication (DRY Violations) Across OAuth Modules
- **Severity:** Low
- **File & Line:**
  - `_settings_for`: [`src/menhir/api/oauth_authorize.py:94`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_authorize.py#L94) and [`src/menhir/api/oauth_token.py:24`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_token.py#L24)
  - `new_client_id`: [`src/menhir/api/client_token_store.py:19`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/client_token_store.py#L19) and [`src/menhir/api/oauth_client_store.py:11`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_client_store.py#L11)
- **Trace:**
  - `_settings_for(request: Request) -> object` has byte-identical AST body hash `13108dbe751491b0` (`return getattr(request.app.state, "settings", None) or MemorySettings.from_env()`).
  - `new_client_id() -> str` has byte-identical AST body hash `784c01fded0b58e4` (`return secrets.token_hex(8)`).
- **Impact:** Minor code duplication and maintenance friction when modifying settings resolution or ID generation patterns.
- **Fix:** Consolidate `_settings_for` into `server_support.py` or `errors.py`, and `new_client_id` into a common utility module.

#### FINDING MAINT-2: Unused Imports in API Modules
- **Severity:** Low
- **File & Line:**
  - [`src/menhir/api/oauth.py:20`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth.py#L20): `_as_bool`, `_as_tuple`, `_get_setting`, `build_oauth_config`
  - [`src/menhir/api/oauth.py:287`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth.py#L287): `build_oauth_preflight` (not re-exported in `__all__`)
  - [`src/menhir/api/oauth_as_register.py:20`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_as_register.py#L20): `build_register_limiter`
  - [`src/menhir/api/oauth_authorize.py:39`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_authorize.py#L39): `build_approve_limiter`
  - [`src/menhir/api/oauth_preflight.py:5`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/oauth_preflight.py#L5): `typing.Any`
  - [`src/menhir/api/routes.py:31`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes.py#L31): `ClientSummary`, `RuntimeContext`, `_OP_TIER_AGENT`, `_OP_TIER_OPERATOR`
- **Impact:** Clutter, slight import overhead, and potential confusion for future maintainers.
- **Fix:** Remove unused imports.

#### FINDING MAINT-3: Docstring Drift in `mcp_remote.py`
- **Severity:** Low
- **File & Line:** [`src/menhir/api/mcp_remote.py:86`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/mcp_remote.py#L86)
- **Trace:** Docstring states `"Remote MCP is tool-only by design"`, but `create_remote_tool_only_mcp()` explicitly executes `register_memory_resources(remote_mcp)` at line 79, and `create_mcp_streamable_http_app()` executes `register_memory_resources(remote_mcp)` at line 110.
- **Impact:** Misleading documentation for callers auditing MCP surface capabilities.
- **Fix:** Update docstring to accurately state that resources are registered.

---

### A5. Performance & Concurrency

#### FINDING A5-1: Synchronous Blocking SQLite I/O on the Async Event Loop
- **Severity:** Medium
- **File & Line:** 13 call sites across `auth.py`, `routes_handlers.py`, `oauth_as_register.py`, `oauth_authorize.py`, `oauth_token.py`
- **Trace:**
  - `src/menhir/api/auth.py:461`: `async _call_with_client_token` -> `ClientTokenStore.resolve` (`sqlite3.connect`)
  - `src/menhir/api/auth.py:468`: `async _call_with_client_token` -> `ClientTokenStore.has_active` (`sqlite3.connect`)
  - `src/menhir/api/auth.py:516`: `async _call_with_client_token` -> `ClientTokenStore.resolve` (`sqlite3.connect`)
  - `src/menhir/api/routes_handlers.py:260`: `async mint_client_impl` -> `ClientTokenStore.mint_bootstrap`
  - `src/menhir/api/routes_handlers.py:268`: `async mint_client_impl` -> `ClientTokenStore.mint`
  - `src/menhir/api/routes_handlers.py:293`: `async list_clients_impl` -> `ClientTokenStore.all`
  - `src/menhir/api/routes_handlers.py:309`: `async revoke_client_impl` -> `ClientTokenStore.revoke`
  - `src/menhir/api/oauth_as_register.py:91`: `async register_client` -> `OAuthClientStore.reap_stale`
  - `src/menhir/api/oauth_as_register.py:100`: `async register_client` -> `OAuthClientStore.count`
  - `src/menhir/api/oauth_as_register.py:170`: `async register_client` -> `OAuthClientStore.register`
  - `src/menhir/api/oauth_authorize.py:302`: `async authorize_get/post` -> `OAuthClientStore.get`
  - `src/menhir/api/oauth_authorize.py:503`: `async authorize_get/post` -> `AuthCodeStore.issue`
  - `src/menhir/api/oauth_token.py:80`: `async token` -> `exchange_authorization_code`
- **Impact:** In Python `asyncio`, executing synchronous SQLite filesystem transactions (`sqlite3.connect`, `BEGIN IMMEDIATE`, disk writes) blocks the single-threaded event loop for all concurrent requests. Under heavy concurrent load or slow disk I/O, this introduces request jitter and latency spikes.
- **Fix:** Wrap synchronous SQLite store operations in `asyncio.to_thread(...)`.

---

### A6. Test Coverage

#### FINDING A6-1: Coverage Gaps on Negative Security Assertions
- **Severity:** Medium
- **File & Line:** `tests/test_api_tier_enforcement.py` and `tests/test_api_routes.py`
- **Trace:**
  - `test_api_tier_enforcement.py` tests individual routes, but does NOT assert that `POST /api/phase3/reset` should be rejected for an `agent`-tier token (it only exercises `phase3/run` and `phase3/reset` under test fixtures where `agent` succeeds).
  - No test asserts redaction or suppression of request bodies when `backend_invoke` fails and logs exceptions.
  - No test measures event loop blocking time during concurrent client token resolution.
- **Impact:** Security and performance regressions can land unnoticed.
- **Fix:** Add targeted tests in `test_api_tier_enforcement.py` verifying tier boundaries and error logging behavior.

---

### A7. LLM / AI

- **Assessment:** Within the audited scope (`src/menhir/api/`), LLM interaction is strictly isolated to `phase3_run_impl` (`routes_handlers.py:40-97`), which invokes `consolidate_personal_memory` via `make_sync_chat`. No request-derived or user-derived text is passed to an LLM for authorization, routing, or access control decisions. The API layer enforces all access controls deterministically via code and cryptographic checks.
- **Verdict:** Clean. No AI/LLM control-flow vulnerabilities present.

---

### Compliance

#### FINDING COMP-1: Unredacted Request Body Logging in `backend_invoke` Exception Handler
- **Severity:** Medium / Compliance
- **File & Line:** [`src/menhir/api/routes_handlers.py:225-227`](file:///C:/Users/thron/IdeaProjects/projects/archolith/menhir/src/menhir/api/routes_handlers.py#L225-L227)
- **Trace:**
  ```python
  except Exception:
      logger.exception("backend_invoke failed: operation=%s body=%r", operation, body)
      raise
  ```
- **Impact:** PII, credentials, or private episodic memory contents contained in `body` are logged verbatim in plaintext log files upon backend failure.
- **Fix:** Sanitize or summarize `body` before logging, e.g.: `logger.exception("backend_invoke failed: operation=%s keys=%r", operation, list((body or {}).keys()))`.

---

## 3. Auth Tier Enforcement Matrix

| Path | HTTP Method | Handler Symbol (`file:line`) | Declared Tier | Enforcing Control | Reachable Unauthenticated? |
|---|---|---|---|---|---|
| `/.well-known/jwks.json` | GET | `jwks` (`oauth_metadata.py:17`) | NONE | Spec exempt (RFC 8414); `_as_enabled` check in body | YES (Public) |
| `/.well-known/oauth-authorization-server` | GET | `oauth_authorization_server_metadata` (`oauth_as_metadata.py:43`) | NONE | Spec exempt (RFC 8414); `_as_enabled` check in body | YES (Public) |
| `/.well-known/oauth-authorization-server/{_as_path:path}` | GET | `oauth_authorization_server_metadata` (`oauth_as_metadata.py:43`) | NONE | Spec exempt (RFC 8414); `_as_enabled` check in body | YES (Public) |
| `/.well-known/oauth-protected-resource` | GET | `oauth_protected_resource_metadata` (`oauth_metadata.py:35`) | NONE | Spec exempt (RFC 8414); `config.enabled` check in body | YES (Public) |
| `/.well-known/oauth-protected-resource/{_resource_path:path}` | GET | `oauth_protected_resource_metadata` (`oauth_metadata.py:35`) | NONE | Spec exempt (RFC 8414); `config.enabled` check in body | YES (Public) |
| `/oauth/register` | POST | `register_client` (`oauth_as_register.py:67`) | NONE | Spec exempt (RFC 7591); `_as_enabled`, rate limit, and cap in body | YES (Public / Rate Limited) |
| `/oauth/authorize` | GET | `authorize_get` (`oauth_authorize.py:524`) | NONE | Spec exempt; client verification + admin session / consent in body | YES (Consent Page) |
| `/oauth/authorize` | POST | `authorize_post` (`oauth_authorize.py:589`) | NONE | Spec exempt; HMAC consent token + admin secret verification in body | YES (Requires Admin Secret) |
| `/oauth/token` | POST | `token` (`oauth_token.py:51`) | NONE | Spec exempt (RFC 6749); PKCE + auth code verification in body | YES (Requires Valid Code) |
| `/api/health` | GET | `health` (`routes.py:89`) | NONE | Explicitly exempt in `BearerAuthMiddleware` (`_EXEMPT_PATHS`) | YES (Healthcheck) |
| `/api/ready` | GET | `ready` (`routes.py:103`) | NONE | Explicitly exempt in `BearerAuthMiddleware` (`_EXEMPT_PATHS`) | YES (Readiness) |
| `/api/recall` | POST | `recall` (`routes.py:123`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/scalar-authority/{view_uuid}/contributors` | GET | `scalar_authority_contributors` (`routes.py:176`) | readonly | `BearerAuthMiddleware` + `_require_tier("readonly")` | NO |
| `/api/bootstrap/flagged` | GET | `bootstrap_flagged` (`routes.py:200`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/bootstrap/context` | POST | `bootstrap_context` (`routes.py:228`) | readonly | `BearerAuthMiddleware` + bootstrap receipt check | NO |
| `/api/context` | POST | `context` (`routes.py:284`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/memory` | POST | `ingest_memory` (`routes.py:307`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/turn-evidence` | POST | `record_turn_evidence` (`routes.py:350`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/episode-admission` | POST | `link_episode_admission` (`routes.py:396`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/tool-events` | POST | `record_tool_event` (`routes.py:448`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/tool-events/dirty` | GET | `tool_events_dirty` (`routes.py:522`) | readonly | `BearerAuthMiddleware` + `_require_tier("readonly")` | NO |
| `/api/tool-events/stale` | GET | `tool_events_stale` (`routes.py:536`) | readonly | `BearerAuthMiddleware` + `_require_tier("readonly")` | NO |
| `/api/tool-events/stale-verifications` | POST | `record_stale_anchor_verification` (`routes.py:549`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/tool-events/stale-verifications` | GET | `list_stale_anchor_verifications` (`routes.py:578`) | readonly | `BearerAuthMiddleware` + `_require_tier("readonly")` | NO |
| `/api/memory/{uuid}` | DELETE | `delete_memory` (`routes.py:597`) | operator | `BearerAuthMiddleware` + `_require_tier("operator")` | NO |
| `/api/namespace/{namespace}` | DELETE | `delete_namespace` (`routes.py:606`) | operator | `BearerAuthMiddleware` + `_require_tier("operator")` | NO |
| `/api/memory/{uuid}/flag` | POST | `flag_memory` (`routes.py:633`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/memory/{uuid}/unflag` | POST | `unflag_memory` (`routes.py:652`) | agent | `BearerAuthMiddleware` + `_require_tier("agent")` | NO |
| `/api/stats` | GET | `stats` (`routes.py:660`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/phase3/run` | POST | `phase3_run` (`routes.py:685`) | agent | `BearerAuthMiddleware` + `require_tier("agent")` | NO |
| `/api/phase3/status` | GET | `phase3_status` (`routes.py:698`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/views` | GET | `phase3_views` (`routes.py:711`) | readonly | `BearerAuthMiddleware` (Token verification) | NO |
| `/api/phase3/reset` | POST | `phase3_reset` (`routes.py:728`) | **operator** (actual: **agent**) | `BearerAuthMiddleware` + `require_tier("agent")` (**BYPASS: SEC-1**) | NO |
| `/api/internal/backend/{operation}` | POST | `backend_invoke` (`routes.py:746`) | dynamic | `BearerAuthMiddleware` + `require_tier(required_tier_for_operation(op))` | NO |
| `/api/admin/clients` | POST | `mint_client` (`routes.py:770`) | operator | `BearerAuthMiddleware` admin gate + `require_tier("operator")` | NO (Bootstrap loopback only on empty store) |
| `/api/admin/clients` | GET | `list_clients` (`routes.py:783`) | operator | `BearerAuthMiddleware` admin gate + `require_tier("operator")` | NO |
| `/api/admin/clients/{client_id}/revoke` | POST | `revoke_client` (`routes.py:792`) | operator | `BearerAuthMiddleware` admin gate + `require_tier("operator")` | NO |
| `/mcp` | SSE Mount | `create_mcp_sse_app` (`mcp_remote.py:83`) | dynamic | `BearerAuthMiddleware` + per-tool `BaseTool.execute` tier check | NO |
| `/mcp-http` | Streamable HTTP | `create_mcp_streamable_http_app` (`mcp_remote.py:93`) | dynamic | `BearerAuthMiddleware` + per-tool `BaseTool.execute` tier check | NO |

---

## 4. Bug-Class Sweep Results

| Bug Class | Description | Proving Command | Output / Status | Verdict |
|---|---|---|---|---|
| **1. Duplicate Definitions** | Later definition silently overrides earlier in same/cross file | `python .agent/audit/m2_functional_probe.py` (Core 2 check) | `_settings_for` defined in 2 files (`oauth_authorize.py:94`, `oauth_token.py:24`, identical body); `new_client_id` defined in 2 files (`client_token_store.py:19`, `oauth_client_store.py:11`, identical body). Duplicate name groups: 2. | **CLEAN** (No intra-file overriding duplicates; cross-file instances are benign DRY candidates). |
| **2. Unbound Names in `except`** | NameError inside `except` handler destroys original exception | `pyflakes src/menhir/api` | 0 undefined names in `except` blocks or anywhere in scope. | **CLEAN** |
| **3. Escaping `CancelledError`** | `except Exception` swallowing or breaking on `CancelledError` | AST inspection of `try...finally` blocks | All resource/session bindings (`RequestContextMiddleware:68-71`, `BearerAuthMiddleware:377, 405, 500, 549, 602`, `server_support.py:101-106`) use `try...finally` ensuring state cleanup on `CancelledError`. | **CLEAN** |
| **4. Lexicographic Timestamp Comparison** | Python `T` vs SQLite space separator compared as text | Source inspection of SQLite schemas | `client_token_store.py` and `archolith_oauth` use REAL floats (`time.time()`). `routes.py:560` formats ISO8601 UTC strings for audit receipts. No text datetime comparisons in queries. | **CLEAN** |
| **5. Unread Module Constants** | Module constants documenting dead invariants | `python .agent/audit/m2_functional_probe.py` (Core 3 check) | 36 constants scanned across 24 files, 0 unread constants. | **CLEAN** |
| **6. Keyword-Argument Mismatches** | Caller passes kwarg target does not accept | AST inspection of `_BACKEND_METHODS` vs `MemoryBackend` & `MemoryGraphAdapter` | 78 methods in `_BACKEND_METHODS` match signatures on `MemoryBackend` and `RuntimeProvider`. 14 `MemoryGraphAdapter` methods matched caller signatures. | **CLEAN** |

---

## 5. Test Coverage Gap Analysis

| Finding / Property | Covered by Existing Tests? | Existing Test File | Gap Details |
|---|---|---|---|
| `POST /api/phase3/reset` tier enforcement (SEC-1) | **NO** | `test_api_routes.py` | Existing test only exercises reset with an `agent` token and asserts success; no test asserts that `agent` MUST NOT be allowed to delete a namespace. |
| `backend_invoke` error log redaction (COMP-1) | **NO** | `test_api_routes.py` | No test inspects log records emitted during failed `backend_invoke` calls with sensitive payloads. |
| Blocking SQLite on event loop (A5-1) | **NO** | N/A | No test profiles event loop tick lag during concurrent client token resolution. |
| Route-level `_require_tier("readonly")` consistency (SEC-2) | **NO** | `test_api_tier_enforcement.py` | Existing tests test middleware 401/403, but don't test naked router endpoints decoupled from middleware. |

---

## 6. Disproved Candidates

### Candidate 1: Admin Minting Open Redirect / Loopback Spoofing Behind Proxy
- **Hypothesis:** An external attacker behind a reverse proxy can spoof loopback address `127.0.0.1` and mint an initial bootstrap operator token via `POST /api/admin/clients`.
- **Disproof:** Executed trace in `auth.py:441-455` and `test_loopback_auth_safety.py`. `BearerAuthMiddleware` checks `not self._has_proxy_forwarding_header(headers)` (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`). A reverse proxy unavoidably appends forwarding headers, automatically disabling the loopback bootstrap window. Additionally, `client_token_store.mint_bootstrap` uses an atomic `INSERT ... WHERE NOT EXISTS`, preventing concurrent bootstrap races.

### Candidate 2: OAuth 2.1 PKCE Downgrade to `plain`
- **Hypothesis:** An OAuth client can downgrade PKCE by supplying `code_challenge_method="plain"`.
- **Disproof:** Executed check in `oauth_authorize.py:330`: `if code_challenge_method != "S256": raise _RedirectError("invalid_request", "code_challenge_method must be S256")`. Plain PKCE is unconditionally rejected.

### Candidate 3: Open Redirect via Prefix Matching on `redirect_uri`
- **Hypothesis:** An attacker can supply `https://client.com/callback/attacker` matching a registered `https://client.com/callback`.
- **Disproof:** Executed check in `oauth_authorize.py:305`: `if not redirect_uri or redirect_uri not in client.redirect_uris: raise ValueError(...)`. Exact membership in the registered tuple is required; substring or prefix matching is not permitted.

---

## 7. Open Questions

1. **Phase 3 Personal Memory Consolidation Embedding Availability:**
   - In `routes_handlers.py:53`, `embed = make_view_embedder(settings)` is called without checking if `embed is None`. If no view embedder is configured in settings, does `consolidate_personal_memory` fail open, abstain, or raise `TypeError`?
   - *Settlement:* Run a unit test calling `phase3_run` with an unconfigured embedder setting.

---

## 8. Coverage Table

| File | Lines | Covered / Verified | Notes |
|---|---:|:---:|---|
| `src/menhir/api/__init__.py` | 2 | COVERED | Package docstring |
| `src/menhir/api/auth.py` | 676 | COVERED | `BearerAuthMiddleware`, identity resolution, session derivation |
| `src/menhir/api/auth_code_store.py` | 91 | COVERED | OAuth authorization code wrapper |
| `src/menhir/api/auth_mode.py` | 15 | COVERED | Auth mode enum re-exports |
| `src/menhir/api/client_token_store.py` | 283 | COVERED | SQLite hashed client token storage & bootstrap atomicity |
| `src/menhir/api/errors.py` | 61 | COVERED | Standardized JSON error response envelope helpers |
| `src/menhir/api/jose_provider.py` | 110 | COVERED | `joserfc` wrapper for JWT verification & key generation |
| `src/menhir/api/mcp_remote.py` | 111 | COVERED | Tier-filtered FastMCP SSE and Streamable HTTP mounts |
| `src/menhir/api/oauth.py` | 287 | COVERED | Resource server JWT token verifier, scopes & JWKS cache |
| `src/menhir/api/oauth_as_metadata.py` | 65 | COVERED | RFC 8414 AS metadata endpoints |
| `src/menhir/api/oauth_as_register.py` | 197 | COVERED | RFC 7591 Dynamic Client Registration endpoint |
| `src/menhir/api/oauth_authorize.py` | 684 | COVERED | `/oauth/authorize` GET/POST, PKCE, consent HMAC, session cookie |
| `src/menhir/api/oauth_client_store.py` | 65 | COVERED | SQLite registered client store wrapper |
| `src/menhir/api/oauth_keys.py` | 80 | COVERED | Signing key management & public JWKS serialization |
| `src/menhir/api/oauth_metadata.py` | 77 | COVERED | RFC 9207 protected resource metadata & JWKS endpoints |
| `src/menhir/api/oauth_preflight.py` | 287 | COVERED | Offline configuration preflight & credential redaction |
| `src/menhir/api/oauth_rate_limit.py` | 145 | COVERED | In-memory fixed window rate limiter with peer IP resolution |
| `src/menhir/api/oauth_token.py` | 109 | COVERED | `/oauth/token` exchange endpoint |
| `src/menhir/api/request_context.py` | 71 | COVERED | `RequestContextMiddleware` and `x-request-id` header binding |
| `src/menhir/api/routes.py` | 799 | COVERED | Core REST API endpoints |
| `src/menhir/api/routes_handlers.py` | 312 | COVERED | Extracted handlers for Phase 3, backend invoke, and admin clients |
| `src/menhir/api/routes_support.py` | 710 | COVERED | Pydantic request/response DTOs, tier definitions, helper utilities |
| `src/menhir/api/server.py` | 87 | COVERED | FastAPI app factory and CLI entry point |
| `src/menhir/api/server_support.py` | 241 | COVERED | Lifespan, CORS, router mounting, and middleware assembly |
| **TOTAL** | **5,565** | **100% COVERED** | **All 24 files audited; 0 files marked NOT READ** |

---

## 9. What Was Checked, and What Could Not Be Verified

### Checked:
- All 24 source files in `src/menhir/api/` read in full.
- All 37 REST, OAuth, and metadata routes mapped against middleware and handlers.
- Both MCP mounts (`/mcp` and `/mcp-http`) verified against `BearerAuthMiddleware` and `BaseTool.execute`.
- Complete AST sweep for duplicate definitions, unread constants, private imports, and layering edges.
- Pyflakes verification across all 24 files.
- Executable probe `.agent/audit/m2_functional_probe.py` created and executed against clean checkout.
- 414 targeted unit tests executed via `pytest`.

### Not Mechanically Verified in This Environment:
- Live multi-worker uvicorn deployment under high-concurrency network load (verified via source trace and single-process probe).
- Physical Neo4j database clustering latency impact during `backend_invoke` transactions.

---

## 10. Review Confidence

**Confidence Score:** **96 / 100**

**Reasoning:**
- 100% of the 24 files and 5,565 lines in scope were thoroughly read and audited with zero unread files.
- All primary findings (including the critical SEC-1 privilege escalation and COMP-1 log exposure) are backed by executable reproductions in `.agent/audit/m2_functional_probe.py`.
- Full alignment with workspace probe protocols and clean verification against existing test suites.
