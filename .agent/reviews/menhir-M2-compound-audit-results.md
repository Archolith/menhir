# Menhir M2 Compound Audit Results

**Status:** IN PROGRESS — evidence draft, not final disposition  
**Audit target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Scope:** `src/menhir/api/` (24 files; prompt baseline 5,565 lines)  
**Probe:** `.agent/audit/m2_functional_probe.py`  
**Last updated:** 2026-08-12

> This report is being refined in place. Findings below are supported by traced code paths; executed-output fields remain explicitly pending until the committed probe is run against a clean checkout. No pending item is counted as executed evidence.

## 1. Executive Summary

The highest-risk result found so far is a **tier-enforcement bypass on the destructive Phase 3 reset endpoint**. `POST /api/phase3/reset` asks for only `agent` tier and then deletes the namespace partition and purges its `TurnEvidence`, while the ordinary namespace-delete route and internal backend-dispatch policy classify namespace deletion as `operator`. An agent credential can therefore perform a destructive operation outside its documented tier.

Current supported findings:

| ID | Severity | Audit lanes | Summary |
|---|---|---|---|
| M2-01 | High (provisional) | A1, A2, A6 | `/api/phase3/reset` authorizes `agent` and performs namespace/evidence deletion |
| M2-02 | Medium (provisional) | A1, A2, A6 | A failed token exchange consumes the authorization code before PKCE/resource/client validation |
| M2-03 | Medium (provisional) | A1, A3 | Runtime startup failures after `start_runtime()` can bypass `stop_runtime()` |
| M2-04 | Medium (provisional) | A3, A5, A6 | `/api/views` performs one history query per returned counter (bounded at 500, sequential) |
| M2-05 | Low (provisional) | A1, A2, Compliance | OAuth preflight treats unsupported/malformed URL schemes as safe and can fail open on credential redaction |

No LLM/AI authorization finding has been identified. Request-derived text reaches LLM-backed work through the Phase 3 run and memory-processing services, but no LLM output in this API layer is used to authenticate a caller or choose a caller tier.

## 2. Findings by Audit Type

### M2-01 — Agent tier can delete a Phase 3 namespace and its evidence

**Severity:** High (provisional)  
**Lanes:** A1 Functional Correctness; A2 Security; A6 Test Coverage  
**Primary code:** `src/menhir/api/routes_handlers.py:182-191`  
**Route wrapper:** `src/menhir/api/routes.py:730-748`  
**Contradicting policy:** `src/menhir/api/routes_support.py:618-652`

`phase3_reset_impl()` calls `require_tier("agent")`, records a destructive operation, calls `backend.delete_namespace(namespace)`, and then calls `adapter.purge_turn_evidence(namespace)`. This is not a read or reversible write: it removes both the namespace graph partition and its captured evidence.

The same module's total internal-dispatch policy places `delete_namespace` in `_OP_TIER_OPERATOR`, and the ordinary `DELETE /api/namespace/{namespace}` route also requires operator. The dedicated Phase 3 route therefore bypasses the shared destructive-operation policy.

**Impact:** Any agent-tier bearer/client token can erase a non-default namespace through the dedicated reset endpoint. The backend's default/shared-namespace refusal limits the blast radius, but does not restore the missing operator authorization for other namespaces.

**Reproduction:** `probe_phase3_reset_tier()` in the committed probe invokes the real handler with fakes that record the requested tier and destructive calls. Expected vulnerable output records `required_tier=agent`, `delete_namespace_calls=1`, and `purge_turn_evidence_calls=1`.

**Fix:** Require `operator` in `phase3_reset_impl()`. Prefer routing the operation through the same total operation-policy function used by internal dispatch, or define one shared constant/policy entry that both endpoints consume. Add an agent-denied/operator-allowed integration test through the actual ASGI middleware.

### M2-02 — Invalid verifier/resource attempts irreversibly burn a valid authorization code

**Severity:** Medium (provisional)  
**Lanes:** A1 Functional Correctness; A2 Security; A6 Test Coverage  
**Menhir call site:** `src/menhir/api/oauth_token.py:45-98`  
**Pinned dependency trace:** `archolith_oauth/stores.py:138-225`; `archolith_oauth/tokens.py:70-135`

Menhir delegates `/oauth/token` to `exchange_authorization_code()`. In the pinned `archolith-oauth` dependency, the exchange first calls `AuthorizationCodeStore.redeem()`. `redeem()` atomically sets `redeemed_at` before returning the row. Only afterward does the exchange validate the resource, PKCE verifier, and continued client existence. The store also checks `client_id` and `redirect_uri` only after the row has been marked redeemed.

**Impact:** A request that possesses a valid code but submits a wrong verifier, resource, client ID, or redirect URI permanently invalidates the code. The legitimate client cannot retry with correct values. This creates a one-request denial of service against an in-flight authorization grant and makes ordinary client correction impossible.

**Reproduction:** `probe_authorization_code_burn()` issues a real code in temporary SQLite stores, exchanges it once with a wrong verifier, and then retries with the correct verifier. The second request should succeed under validate-then-consume semantics; the vulnerable implementation returns `invalid_grant` because the first failure already consumed the code.

**Fix:** Validate all code bindings and PKCE before committing redemption, while retaining an atomic compare-and-set to guarantee single use. One safe shape is a transaction that selects the unredeemed row, validates client/redirect/resource/PKCE, and updates `redeemed_at` only when all checks pass.

### M2-03 — Partial startup can leak an initialized runtime

**Severity:** Medium (provisional)  
**Lanes:** A1 Functional Correctness; A3 Architecture  
**Code:** `src/menhir/api/server_support.py:67-111`

`build_runtime_lifespan()` calls `start_runtime(settings)` before constructing `MemoryGraphAdapter`, lifecycle/candidate services, and before entering `mcp_http_instance.session_manager.run()`. The `try/finally` that calls `stop_runtime()` exists only inside that later session-manager context.

**Impact:** If adapter/service construction or the session-manager context entry raises after runtime initialization, startup fails without calling `stop_runtime()`. Neo4j, Graphiti, scheduler, or other runtime resources can remain live in a partially initialized process/test host.

**Reproduction:** `probe_startup_cleanup_gap()` patches the real module's `start_runtime()` to succeed and `MemoryGraphAdapter` to raise. It records whether `stop_runtime()` runs.

**Fix:** Put every post-`start_runtime()` step inside an outer `try/finally`, or use an `AsyncExitStack` that registers `stop_runtime()` immediately after successful startup.

### M2-04 — `/api/views` performs sequential N+1 history queries

**Severity:** Medium (provisional)  
**Lanes:** A3 Architecture; A5 Performance; A6 Test Coverage  
**Code:** `src/menhir/api/routes_handlers.py:146-177`

`phase3_views_impl()` loads up to 500 counters, then loops over them and awaits `adapter.counter_history(...)` one counter at a time. A response with N non-receipt counters executes 1 + N adapter queries and serializes all of their histories into one response.

**Impact:** Latency grows linearly with the number of counters and accumulates round-trip time serially. The endpoint can also return a large nested response because each of up to 500 counters carries its complete history.

**Reproduction:** `probe_phase3_views_n_plus_one()` supplies three counters to the actual handler and records three separate `counter_history` calls after the initial list.

**Fix:** Add a batch adapter method that returns counters and histories in one bounded query, or fetch histories concurrently with an explicit small semaphore and a total-history/result-size cap. Prefer the batch query to avoid graph round-trip amplification.

### M2-05 — OAuth preflight URL safety and redaction fail open

**Severity:** Low (provisional)  
**Lanes:** A1 Functional Correctness; A2 Security; Compliance  
**Code:** `src/menhir/api/oauth_preflight.py:11-54`

`_is_https_or_loopback_http()` returns `True` for every non-HTTP scheme and on parse exceptions, despite its contract saying only HTTPS or loopback HTTP is safe. As a result, values such as `ftp://...` and `file://...` produce no scheme warning.

Separately, `_redact_url_credentials()` catches every exception and returns the original string. A URL with userinfo and an invalid port can parse sufficiently to expose `username/password`, then raise when `.port` is accessed; the fallback returns the unredacted credential-bearing value.

**Impact:** Operator diagnostics can affirm unsafe URL configurations and can echo credentials in malformed URL values rather than redacting them. This is diagnostic/configuration exposure, not proof that the runtime HTTP client accepts every such URL.

**Reproduction:** `probe_oauth_preflight_fail_open()` prints safety results for unsupported schemes and the redaction result for a credential-bearing invalid-port URL.

**Fix:** Return `False` for all schemes other than HTTPS and loopback HTTP, and return `False` on parse errors. Redaction should fail closed: strip or replace userinfo without touching `.port`, and return a fixed redacted placeholder if parsing is invalid.

## 3. Auth Tier Enforcement Matrix

Draft matrix; every row will be reconciled against the final route enumeration and executed ASGI tests.

| Surface | Operation | Intended minimum | Actual gate | Status |
|---|---|---:|---:|---|
| `/api/health` | health | public | middleware exemption | expected |
| `/api/ready` | readiness | public | middleware exemption | expected |
| `/api/recall`, `/api/context`, bootstrap reads, `/api/stats` | memory/runtime reads | readonly | authenticated middleware; no stronger route gate | expected |
| `/api/memory`, turn evidence, admissions, tool events, flag/unflag | writes | agent | explicit `_require_tier("agent")` where destructive/write semantics require it | traced; final row audit pending |
| `DELETE /api/memory/{uuid}` | delete memory | operator | explicit operator | expected |
| `DELETE /api/namespace/{namespace}` | delete namespace | operator | explicit operator | expected |
| `POST /api/phase3/reset` | delete namespace + evidence | operator | **agent** | **M2-01** |
| `/api/internal/backend/{operation}` | operation-dependent | total policy map | `_required_tier_for_operation()` | traced; operation-by-operation reconciliation pending |
| `/api/admin/*` | token administration | operator/bootstrap exception | middleware admin gate + route defense in depth | traced |
| `/mcp`, `/mcp/*`, `/mcp-http` | MCP | tool-dependent | middleware auth + invocation-time tier gate | traced |
| `/oauth/register`, `/oauth/authorize`, `/oauth/token`, well-known metadata | OAuth protocol | protocol-specific/public | outside bearer middleware; in-handler controls | traced |
| `/explorer/*` | explorer | remote authenticated; direct loopback exception | middleware path gate | traced |

## 4. Bug-Class Sweep Results

| Bug class | Current result | Evidence status |
|---|---|---|
| Duplicate definitions | sweep pending AST/probe output | not yet executed |
| Names only used in `except` handlers | pyflakes/probe sweep pending | not yet executed |
| `except Exception` vs `CancelledError` cleanup | one real startup-cleanup defect found, but not caused by swallowing cancellation; complete sweep pending | traced, not executed |
| Lexicographic timestamp comparison | no in-scope instance identified yet | proving search pending |
| Unused invariant constants | manual inventory in progress | proving search pending |
| Cross-module keyword mismatch | call-contract inventory in progress | proving execution pending |

## 5. Test Coverage Gap Analysis

Pending full read of all eleven named test files. Initial gaps to verify:

- A test must assert that agent tier is denied on `/api/phase3/reset`; merely testing successful reset does not cover M2-01.
- Authorization-code tests must assert that failed PKCE/resource/client binding does **not** consume the code; asserting only that the bad exchange fails does not cover M2-02.
- Lifespan tests must inject a failure after `start_runtime()` and assert cleanup; ordinary startup/shutdown success does not cover M2-03.
- Views tests must bound query count or batch histories; response-shape assertions do not cover M2-04.
- Preflight tests must include unsupported schemes, malformed ports, and credential redaction failure paths; ordinary HTTP/HTTPS cases do not cover M2-05.

## 6. Disproved Candidates

### Configured OAuth rate limits are ignored — disproved for the real server construction path

`oauth_as_register.py` and `oauth_authorize.py` define default module-level limiter instances, which initially suggested that configured rate/window settings were dead. `build_server_prereqs()` replaces both globals with `build_register_limiter(settings)` and `build_approve_limiter(settings)` during application construction (`src/menhir/api/server_support.py:38-55`). The production `create_app()` path calls this function before routes serve requests. This candidate is retained here because direct module-only tests can see defaults, but it is not a production-path finding.

### `/mcp-http` sits outside bearer middleware — disproved

`_is_mcp_path()` explicitly includes exact `/mcp-http` (`src/menhir/api/auth.py:90-93`), and `mount_server_routes()` inserts an exact Starlette route at that path (`src/menhir/api/server_support.py:205-222`). `/mcp`, `/mcp/*`, and `/mcp-http` all enter the same bearer-mode dispatch before the MCP application.

## 7. Open Questions

- **Dynamic registration cap race:** `count()` and `register()` are separate SQLite transactions. Concurrent registrations may exceed `MENHIR_OAUTH_AS_MAX_CLIENTS`; an executable barrier probe will settle this.
- **Client-token settings split:** HTTP uses the configured store snapshot, while MCP helpers can re-read environment state through `get_client_token_store()`. Tests with explicit settings differing from environment will determine whether this selects no store or a different database.
- **MCP query-string secret exposure:** middleware strips `api_key` only from the downstream scope. Uvicorn/proxy access logging may observe the original query before middleware. Deployment/logging trace pending.
- **HTTPException headers:** the application exception handler serializes `exc.headers` into JSON instead of applying them as response headers. Search is pending for in-scope exceptions relying on `WWW-Authenticate`, `Retry-After`, or other headers.

## 8. Coverage Table

All 24 scope files have been read. Independent newline measurement and final 5,565-line reconciliation will be emitted by the probe; values below are the control-prompt baseline until that output is committed.

| File | Baseline lines | Read status |
|---|---:|---|
| `routes.py` | 799 | READ |
| `routes_support.py` | 710 | READ |
| `oauth_authorize.py` | 684 | READ |
| `auth.py` | 676 | READ |
| `routes_handlers.py` | 312 | READ |
| `oauth_preflight.py` | 287 | READ |
| `oauth.py` | 287 | READ |
| `client_token_store.py` | 283 | READ |
| `server_support.py` | 241 | READ |
| `oauth_as_register.py` | 197 | READ |
| `oauth_rate_limit.py` | 145 | READ |
| `mcp_remote.py` | 111 | READ |
| `jose_provider.py` | 110 | READ |
| `oauth_token.py` | 109 | READ |
| `auth_code_store.py` | 91 | READ |
| `server.py` | 87 | READ |
| `oauth_keys.py` | 80 | READ |
| `oauth_metadata.py` | 77 | READ |
| `request_context.py` | 71 | READ |
| `oauth_client_store.py` | 65 | READ |
| `oauth_as_metadata.py` | 65 | READ |
| `errors.py` | 61 | READ |
| `auth_mode.py` | 15 | READ |
| `__init__.py` | 2 | READ |
| **Total** | **5,565** | **24/24 READ; measurement pending** |

## 9. What Was Checked / Environment Limits

Checked so far:

- All 24 in-scope API files at the pinned commit.
- Pinned `archolith-oauth` store and token-exchange implementations selected by `pyproject.toml`.
- Supporting auth mode, OAuth config, entrypoint, and MCP invocation contracts.
- ASGI middleware path coverage for API, explorer, SSE MCP, and streamable HTTP MCP.

Still in progress:

- Full named-test inspection and property-vs-exercise classification.
- Executing the committed probe and recording verbatim output.
- AST/pyflakes/constant/call-contract sweeps.
- Final line measurement, route enumeration, and severity calibration.

The current harness can read and write through the connected GitHub API but cannot resolve GitHub from the local execution container. Therefore no command output is claimed yet. The probe is designed to run from a clean checkout and will remain the sole source for executed claims in the final report.

## 10. Review Confidence

**Current confidence: 64/100.** All scope code is read and the leading findings have complete control-flow traces, but the score remains preliminary because tests and executable sweeps are not finished and no probe output has yet been recorded.
