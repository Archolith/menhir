# Menhir M2 Compound Audit Results

**Repository:** `Archolith/menhir`  
**Pinned revision:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Scope:** exactly 24 files and 5,565 lines under `src/menhir/api/`  
**Status:** source review complete; runtime probes still require execution from a clean pinned checkout

## Evidence correction

The earlier chat response claimed that the report, probe, and selected pytest output existed. Sandbox inspection showed only a 444-byte ZIP containing a failed pytest invocation. The command exited 4 because `tests/test_api_auth.py` was not present in that working directory. That failure is not treated as test evidence.

## Executive summary

The highest-risk confirmed result is **M2-H01: Explorer authentication can be bypassed when Menhir is loopback-bound behind a same-host reverse proxy**. `BearerAuthMiddleware` sets `_loopback_admin_ok = loopback_bound` and later exempts Explorer when `_loopback_admin_ok or direct_loopback` is true. Forwarding headers constrain only `direct_loopback`, so a loopback-bound process remains exempt even for a proxied remote request. See `src/menhir/api/auth.py:151-163`, `src/menhir/api/auth.py:327-338`, and `src/menhir/api/server_support.py:220-238`.

A second High result, **M2-H02**, permits an `agent` token to call Phase 3 reset, which invokes namespace deletion and purges TurnEvidence. The ordinary namespace deletion route and internal backend operation require `operator`. See `src/menhir/api/routes_handlers.py:172-195`.

No Critical result was confirmed. Eight findings were confirmed:

| ID | Severity | Result |
|---|---|---|
| M2-H01 | High | Explorer auth bypass behind a loopback-bound reverse proxy |
| M2-H02 | High | Agent-tier Phase 3 reset deletes namespace data and evidence |
| M2-M01 | Medium | Failed consent approval returns a replacement consent token |
| M2-M02 | Medium | Synchronous SQLite token lookup blocks the ASGI event loop |
| M2-M03 | Medium | `/api/views` performs sequential N+1 storage calls |
| M2-M04 | Medium | Backend exceptions log the complete request body |
| M2-L01 | Low | OAuth preflight treats unknown schemes and parser failures as safe |
| M2-L02 | Low | DCR accepts `refresh_token` although the token endpoint does not implement it |

## Findings

### M2-H01 — Explorer bypass behind a loopback-bound reverse proxy

**Evidence:** `src/menhir/api/auth.py:151-163`, `src/menhir/api/auth.py:327-338`, `src/menhir/api/server_support.py:220-238`

The middleware stores whether the server itself is loopback-bound. Explorer is then passed through whenever that process-wide value is true, independently of the current request's forwarding headers. In a normal nginx or Caddy topology with Menhir listening on `127.0.0.1`, every proxied remote Explorer request satisfies the first side of the `or` and reaches the mounted UI without a bearer credential.

Explorer contains graph reads and candidate approve/reject writes, so this is an authorization bypass, not merely an information disclosure.

**Fix:** exempt Explorer only when the individual request is a direct, unforwarded loopback request. A trusted-proxy deployment should require explicit trusted-peer configuration and validate the forwarded client separately.

**Coverage gap:** `tests/test_api_auth.py:900-929` tests a forwarded request only with `loopback_bound=False`; it misses the vulnerable conjunction `loopback_bound=True`, loopback peer, forwarding header, and no Authorization header.

### M2-H02 — Agent-tier Phase 3 reset deletes namespace data and TurnEvidence

**Evidence:** `src/menhir/api/routes_handlers.py:172-195`

`phase3_reset_impl` asks for `agent`, records a destructive operation, calls `backend.delete_namespace(namespace)`, and calls `adapter.purge_turn_evidence(namespace)`. This gives a reversible-write tier a destructive capability that is operator-only everywhere else.

**Fix:** require `operator`; add middleware-level tests proving readonly and agent receive 403; consider the same `dry_run`, `max_nodes`, and `force` controls used by the ordinary namespace deletion route.

### M2-M01 — Invalid admin-secret response mints a replacement consent token

**Evidence:** `src/menhir/api/oauth_authorize.py:348-355`, `src/menhir/api/oauth_authorize.py:603-663`

The POST handler burns the submitted token's `jti` before checking the operator secret. Its comment says each guess therefore requires a fresh GET. On a wrong secret it calls `_render_consent`, which signs and embeds a new unspent consent token. A guesser can parse the 401 HTML and submit another POST without a GET. The per-IP limiter remains a defense, but the claimed one-shot boundary is absent.

**Fix:** after consuming the `jti`, return a restart page or redirect without signed hidden fields. Do not mint a replacement token on 401 or 429.

### M2-M02 — Synchronous SQLite authentication lookup blocks the event loop

**Evidence:** `src/menhir/api/auth.py:508-520`, `src/menhir/api/client_token_store.py:146-163`

Client-token mode calls synchronous `store.resolve()` directly inside the async ASGI middleware. `resolve()` opens SQLite and executes a query. Lock waits or filesystem latency therefore stall unrelated requests and MCP streaming on the same loop.

**Fix:** use `await asyncio.to_thread(store.resolve, token)` or an async/cached store. Apply the same treatment to admin-path `resolve()` and `has_active()`.

### M2-M03 — `/api/views` has sequential N+1 storage behavior

**Evidence:** `src/menhir/api/routes_handlers.py:146-170`; route limit at `src/menhir/api/routes.py:652-666`

The handler performs one `list_counters` call, then awaits one `counter_history` thread call for every non-receipt row. At the allowed limit of 500 this is up to approximately 501 storage calls, executed sequentially.

**Fix:** add a batch repository query, or use bounded concurrency if batching is impossible.

### M2-M04 — Internal backend failures log the complete body

**Evidence:** `src/menhir/api/routes_handlers.py:198-229`

The generic backend endpoint logs `body=%r` on exceptions. Operation bodies can contain raw memory text, document content, paths, identifiers, notes, or future secret-bearing fields.

**Fix:** log operation name, request ID, safe field names, and size/type metadata. Redact values by default.

### M2-L01 — OAuth preflight fails open on malformed or unsupported URLs

**Evidence:** `src/menhir/api/oauth_preflight.py:36-59`

`_is_https_or_loopback_http` returns true for schemes other than HTTP/HTTPS and for parsing exceptions. This can produce a false safe signal even though stronger runtime configuration validation may later reject the value.

**Fix:** return false for unknown schemes and parse failures and report a structured reason.

### M2-L02 — DCR accepts a grant type the token endpoint rejects

**Evidence:** `src/menhir/api/oauth_as_register.py:30-36`, `src/menhir/api/oauth_as_register.py:132-141`, `src/menhir/api/oauth_token.py:52-67`

DCR accepts requested `refresh_token`, while `/oauth/token` implements only authorization-code exchange. The response avoids advertising refresh tokens, but accepting internally inconsistent request metadata is still an interoperability defect.

**Fix:** restrict accepted DCR grant types to `authorization_code` until refresh exchange is wired.

## Audit-lane results

### Functional correctness

Confirmed M2-H02 and M2-M03. No duplicate definitions, lexicographic timestamp comparison, or local keyword-contract mismatch was confirmed by source review. The probe performs the required AST and pyflakes sweeps when run locally.

### Security

Confirmed M2-H01, M2-M01, M2-L01, and M2-L02. Exact redirect matching, S256-only PKCE, issuer and audience/resource checking, numeric expiry handling, and atomic single-use authorization-code redemption were traced and disproved as candidate defects.

### Architecture and maintainability

`routes.py` combines health, recall, bootstrap, ingestion, evidence, Hook Center, deletion, stats, Phase 3, internal dispatch, and client administration. `oauth_authorize.py` combines signing, replay storage, redirect validation, HTML rendering, cookie sessions, issuance, and endpoint orchestration. Policy is duplicated across route handlers, backend operation maps, MCP contracts, and middleware, which enabled M2-H02.

### Performance

Confirmed M2-M02 and M2-M03. The in-process OAuth rate limiter is bounded to 4,096 tracked keys and is not memory-unbounded.

### Test coverage

The named tests cover static, OAuth, and client-token modes; duplicate-header rejection; identity binding; PKCE; redirect binding; code replay; bind safety; generic tier ordering; consent-secret persistence; and OAuth metadata. Missing assertions tied to findings are:

1. loopback-bound plus forwarded Explorer request must be rejected;
2. agent must receive 403 on Phase 3 reset;
3. invalid secret response must contain no replacement consent token;
4. slow token storage must not block an unrelated coroutine;
5. maximum views request must use bounded repository calls;
6. exception logs must redact body values;
7. unsupported and malformed URLs must fail preflight;
8. DCR must reject `refresh_token` until implemented.

### LLM/AI

No request-derived text is used by this API layer to decide authentication or authorization. Phase 3 run can trigger downstream model work, but the tier decision occurs first and model output does not grant transport access.

### Compliance

No hard-coded production bearer token, OAuth client secret, private key, or password was found in scope. Normal 500 responses are generic. OAuth token responses use no-store headers. M2-M04 is the compliance-relevant logging defect.

## Auth tier matrix summary

- `/api/health` and `/api/ready`: public exemptions.
- Read routes such as recall, context, bootstrap, status, views, stats: any authenticated tier.
- Memory/evidence/tool-event writes: agent.
- Memory deletion, namespace deletion, operator recovery and admin operations: operator.
- `/api/phase3/reset`: **actual agent; intended operator — M2-H02**.
- Internal backend dispatch: tier selected from the total operation map.
- OAuth metadata, DCR, authorize, and token: public protocol endpoints with in-handler controls.
- MCP transports: middleware authentication plus per-tool invocation tier.
- Explorer static files: public.
- Explorer UI/actions: intended direct-loopback exemption or authenticated remote access; **loopback-bound proxy bypass — M2-H01**.

## Required bug-class sweep status

| Bug class | Source-review result | Command/probe |
|---|---|---|
| Duplicate definitions | none confirmed | AST sweep in `.agent/audit/m2_functional_probe.py` |
| Unbound exception names | no unbound logger confirmed | `python -m pyflakes src/menhir/api` |
| `except Exception` versus cancellation | no skipped-reset defect confirmed; middleware cleanup uses `finally` | async exception-handler sweep in probe |
| Lexicographic timestamps | none confirmed; reviewed stores use numeric epochs | timestamp pattern sweep in probe |
| Unused invariant constants | none confirmed | module constant read-count sweep in probe |
| Keyword contract mismatch | none confirmed in local or pinned shared-OAuth call paths | local signature sweep plus targeted runtime probes |

## Disproved candidates

- Authorization-code double redemption: closed by `BEGIN IMMEDIATE` and conditional update.
- Code binding to client and redirect URI: both are persisted and checked.
- Plain PKCE: rejected; S256 required at authorize, store, and exchange.
- JOSE algorithm confusion: verification receives an allowlist; embedded tokens are RS256.
- Missing issuer, expiry, or audience validation: all are checked.
- Client-token bootstrap race: guarded insert permits exactly one empty-store bootstrap winner.
- Unbounded rate-limit memory: tracked-key state is capped.

## Coverage

| File | Lines | Status |
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
| **Total** | **5,565** | **24/24 READ** |

## Environment limits and confidence

The connected environment did not expose a materialized Menhir checkout, so imports, pyflakes, the generated targeted probes, and the named pytest command could not execute here. The earlier pytest artifact is explicitly recorded as failed rather than passed.

**Review confidence: 78/100.** Source confidence is high because all 24 files and important pinned shared-OAuth implementations were traced. Confidence remains below 80 until the probe and named tests run from a checkout whose `git rev-parse HEAD` is exactly `eebf6d6dd83f15083167bf847b639d24b953fdc9`.
