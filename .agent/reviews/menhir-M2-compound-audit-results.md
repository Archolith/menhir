# Menhir M2 Compound Audit Results

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Prompt:** `ctharvey/workspace-meta/.agent/plans/menhir-M2-compound-audit-prompt.md`  
**Methodology:** `ctharvey/workspace-meta/.agent/audit/functional-correctness-audit.md`  
**Primary scope:** exactly 24 files under `src/menhir/api/`, 5,565 lines  
**Runtime dependency traced:** `Archolith/archolith_oauth@19042194ee4e4234da97f478beec85345c3a7110`  
**Disposition:** **BLOCK** pending the Critical findings

## Executive Summary

The audit confirmed **29 findings: 4 Critical, 2 High, 15 Medium, and 8 Low**. Four independent boundary failures make this revision unsafe as a hardened remote API:

1. A static-key MCP caller controls the `client_name` used to select its tool allowlist and namespace pin.
2. `POST /api/phase3/reset` requires only `agent` but deletes a namespace and its turn evidence.
3. Explorer bypasses authentication whenever Menhir is loopback-bound, including same-host reverse-proxy traffic; Explorer includes approve/reject writes.
4. Loopback no-auth mode disables the MCP SDK's DNS-rebinding protection without installing an equivalent Host/Origin gate.

Important controls did hold: exact registered redirect matching, mandatory S256 PKCE, signed single-use consent tokens, explicit JOSE algorithms, issuer/expiry/resource checks, duplicate-sensitive-header rejection, no-store token responses, atomic client-token bootstrap, hashed token storage, and proxy-aware bootstrap denial.

The rerunnable evidence script is `.agent/audit/m2_functional_probe.py`. It performs no production writes; its optional live OAuth-code check uses a temporary SQLite database.

| Severity | Count | Meaning |
|---|---:|---|
| Critical | 4 | authorization bypass or unauthorized destructive access |
| High | 2 | persistent provenance corruption or caller-controlled model spend |
| Medium | 15 | correctness, disclosure, resource-leak, or material performance defect |
| Low | 8 | interoperability, lifecycle-policy, diagnostics, or maintainability gap |

## Coverage Reconciliation

The prompt and code are intentionally in different repositories. The prompt and methodology were read from `ctharvey/workspace-meta`; every scoped implementation file was read at the exact Menhir commit.

| File | Lines | Read |
|---|---:|:---:|
| `src/menhir/api/__init__.py` | 2 | yes |
| `src/menhir/api/auth.py` | 676 | yes |
| `src/menhir/api/auth_code_store.py` | 91 | yes |
| `src/menhir/api/auth_mode.py` | 15 | yes |
| `src/menhir/api/client_token_store.py` | 283 | yes |
| `src/menhir/api/errors.py` | 61 | yes |
| `src/menhir/api/jose_provider.py` | 110 | yes |
| `src/menhir/api/mcp_remote.py` | 111 | yes |
| `src/menhir/api/oauth.py` | 287 | yes |
| `src/menhir/api/oauth_as_metadata.py` | 65 | yes |
| `src/menhir/api/oauth_as_register.py` | 197 | yes |
| `src/menhir/api/oauth_authorize.py` | 684 | yes |
| `src/menhir/api/oauth_client_store.py` | 65 | yes |
| `src/menhir/api/oauth_keys.py` | 80 | yes |
| `src/menhir/api/oauth_metadata.py` | 77 | yes |
| `src/menhir/api/oauth_preflight.py` | 287 | yes |
| `src/menhir/api/oauth_rate_limit.py` | 145 | yes |
| `src/menhir/api/oauth_token.py` | 109 | yes |
| `src/menhir/api/request_context.py` | 71 | yes |
| `src/menhir/api/routes.py` | 799 | yes |
| `src/menhir/api/routes_handlers.py` | 312 | yes |
| `src/menhir/api/routes_support.py` | 710 | yes |
| `src/menhir/api/server.py` | 87 | yes |
| `src/menhir/api/server_support.py` | 241 | yes |
| **Total** | **5,565** | **24/24** |

No scoped file is NOT READ. Supporting configuration, MCP, Explorer, runtime, backend, ingest-guard, and pinned OAuth files were read only to complete entry-to-consequence traces; they are not counted in the denominator.

## Critical Findings

### C1 — Static-key callers can relabel themselves out of MCP policy

**Locations:** `api/auth.py:212-279, 367-404`; `mcp/service_access.py:170-222`; `mcp/contracts.py:278-350`; `api/mcp_remote.py:43-62`  
**Audit:** Security, Architecture

Static mode authenticates the bearer key's tier, then trusts caller-controlled identity headers/query parameters and binds `client_name` into the request session. MCP catalog and invocation enforcement index `MENHIR_CLIENT_TOOLS` and `MENHIR_CLIENT_NAMESPACES` by that name. Missing or unconfigured names produce empty policy values, and empty means unrestricted.

A valid static agent key can omit or change `X-Menhir-Client-Name`, regain all agent-tier tools, and write outside its configured namespace pin. This defeats an authorization control, though it does not elevate above the key's base tier.

**Reproduce:** configure a restrictive allowlist/pin for one client name; compare `tools/list` and invocation with that name versus an omitted/unconfigured name.  
**Fix:** derive policy identity from the credential. Require client-token mode for per-client enforcement, map distinct static keys to immutable IDs, or reject per-client policy configuration in shared static-key mode.

### C2 — Agent-tier Phase 3 reset performs operator-class destruction

**Locations:** `api/routes_handlers.py:180-195`; `api/routes_support.py:637-669`; `api/routes.py:724-747`  
**Audit:** Functional Correctness, Security

`phase3_reset_impl()` requires `agent`, then calls `backend.delete_namespace(namespace)` and `adapter.purge_turn_evidence(namespace)`. The total generic backend policy classifies `delete_namespace` as operator-only. The dedicated route bypasses that policy SSOT.

An agent credential can erase all nodes and evidence in any caller-selected non-default namespace.

**Reproduce:** authenticate as agent and POST `/api/phase3/reset?namespace=<disposable>`.  
**Fix:** require `operator` or dispatch through the total operation policy; add a full-app agent-vs-operator test.

### C3 — Loopback-bound Explorer stays unauthenticated behind a reverse proxy

**Locations:** `api/auth.py:326-345`; supporting `explorer/app.py:780-800`  
**Audit:** Security

The middleware correctly computes `direct_loopback` as loopback peer with no forwarding headers, but authorizes Explorer when `self._loopback_admin_ok OR direct_loopback`. The bind-derived branch is true whenever the upstream binds loopback, even when a same-host proxy supplies `X-Forwarded-For`, `Forwarded`, or `X-Real-IP`.

Explorer includes POST candidate approve/reject routes, so this is an unauthenticated write path, not merely a local dashboard disclosure.

**Reproduce:** `loopback_bound=True`, peer `127.0.0.1`, forwarding header present, request an Explorer write without credentials.  
**Fix:** authorize only request-derived `direct_loopback`; a loopback bind must never independently authenticate a request.

### C4 — Loopback no-auth mode is DNS-rebinding exposed

**Locations:** `api/mcp_remote.py:94-110`; `api/auth.py:356-384`; supporting `config/settings_helpers.py:140-166`  
**Audit:** Security, Architecture

No-auth is permitted on loopback, and `AuthMode.NONE` forwards protected requests without credentials. The MCP server is deliberately created with `host="0.0.0.0"` to disable the SDK's DNS-rebinding protection, relying on parent auth. In no-auth mode there is no parent authentication and no replacement trusted-Host/Origin check.

A malicious browser origin can rebind its DNS to `127.0.0.1` and invoke local REST/MCP endpoints. Empty tier intentionally skips route tier checks, including destructive operations.

**Reproduce:** send a loopback-peer ASGI request with an attacker-controlled Host in no-auth mode.  
**Fix:** retain SDK protection or install an equivalent Host allowlist before auth dispatch; test hostile Host/Origin combinations.

## High Findings

### H1 — `/api/memory` body fields replace verified identity

**Location:** `api/routes.py:302-340`  
**Audit:** Functional Correctness, Security

The route resolves the authenticated `caller_session`, then replaces it when the body contains `user_id` or `session_id`. OAuth and client-token modes correctly reject spoofable identity headers, but the JSON body reaches the same identity fields after authentication.

A valid agent can forge persistent user/session provenance and, if `user_id` is an isolation boundary, write across principals.

**Reproduce:** authenticate as one client and POST memory with another `user_id`.  
**Fix:** use only credential-derived identity in authenticated modes; expose delegation as a separate operator-only action.

### H2 — Agent caller controls an unbounded real LLM budget

**Locations:** `api/routes_support.py:490-497`; `api/routes_handlers.py:27-108`  
**Audit:** Performance, LLM/AI

`Phase3RunRequest.call_budget` is an unbounded `int | None`. The agent-tier handler constructs the configured model and passes the caller value directly to personal-memory consolidation.

A caller can request arbitrarily high external-model work and cost; negative values also cross the boundary without defined semantics.

**Fix:** add schema and server-side maxima, timeout/cancellation, per-principal metering, and a production feature gate for benchmark routes.

## Medium Findings

| ID | Location | Confirmed behavior and consequence | Remediation |
|---|---|---|---|
| M1 | pinned `archolith_oauth/stores.py:237-276`; `api/oauth_token.py:48-99` | `redeem()` marks a code used before client, redirect, resource, or PKCE validation. A wrong-client request can burn a code without its verifier. | Atomically validate immutable bindings and consume only after resource/PKCE success. |
| M2 | `api/server_support.py:64-103` | Runtime starts before entering the MCP manager context containing the only `stop_runtime()` finally. Exception or cancellation during service construction/manager entry leaks the started runtime. | Outer `try/finally` immediately after successful start; idempotent stop. |
| M3 | `api/oauth_authorize.py:83-87,234-249,599-638` | Every valid consent POST stores a JTI before decision. Deny needs no secret and bypasses the approve limiter, so GET→deny cycles grow memory until TTL expiry. | Rate-limit all consent POSTs and cap the cache. |
| M4 | `api/oauth_as_register.py:83-124,170-187` | DCR cap is `count()` then later `register()` in separate transactions; concurrent requests overshoot the claimed hard cap. | Conditional insert under one `BEGIN IMMEDIATE`. |
| M5 | `api/oauth_preflight.py:43-57` | URL safety returns true for all non-HTTP schemes and all exceptions; `ftp:`, `file:`, and malformed values appear safe. | Fail closed unless valid HTTPS or loopback HTTP. |
| M6 | `api/oauth_preflight.py:12-29` | Invalid `parsed.port` raises inside credential redaction; broad exception returns the original credential-bearing URL. | Never return input on redaction failure; use a fully redacted placeholder. |
| M7 | `api/oauth_authorize.py:468-514,546-580,661-678`; `api/oauth_token.py:66-91` | Authorization binds arbitrary nonempty `resource`; token exchange later requires the canonical AS resource. It can issue a guaranteed-unredeemable code, then M1 consumes it. | Validate/canonicalize resource before consent and issuance. |
| M8 | `api/auth_code_store.py`; pinned store `:277-286` | Store provides `purge_expired()`, but no production call exists under `src/menhir`; redeemed/expired rows accumulate indefinitely. | Opportunistic bounded purge or scheduled cleanup. |
| M9 | `api/routes_handlers.py:48-54,96-99,130-140` | Dirty membership uses `list_dirty_namespaces(limit=500)`; later namespaces are reported clean/not selected. | Direct predicate or complete pagination. |
| M10 | `api/routes_handlers.py:143-174`; `api/routes.py:707-723` | `/api/views` fetches up to 500 counters, then serially awaits one history query per row: up to 501 round trips. | Batch histories or bounded parallelism. |
| M11 | `api/routes_handlers.py:200-228` | Generic backend exception logging emits `body=%r`, including memory text, paths, and future secret fields. | Log operation/request ID/body keys and redacted identifiers only. |
| M12 | `api/auth.py:28-29`; `api/routes.py:84-112` | Auth-exempt `/api/ready` returns raw runtime failures, which can contain internal URLs, model names, topology, and exception text. | Public stable codes; detailed diagnostics operator-only. |
| M13 | `api/auth.py:483-560`; `api/client_token_store.py:145-171` | Async ASGI auth directly opens/queries SQLite for every client-token request, blocking the event loop. | Async store, safe cache, or off-loop lookup. |
| M14 | `api/routes_handlers.py:182-195` | Reset deletes graph namespace before evidence purge; second-leg failure leaves partial teardown. | Coordinated/idempotent operation with recoverable partial state. |
| M15 | `api/routes_support.py:546-669`; supporting `backend_runtime_admin_ops.py:273-304` | `get_provider_config` falls through readonly policy and returns Neo4j/local-LLM/backend URLs, DB name, providers, and models. | Operator-only or redacted capability summary. |

## Low Findings

| ID | Location | Gap | Remediation |
|---|---|---|---|
| L1 | `api/routes.py:524-538` | Stale diagnostics accept negative/arbitrarily large `limit`. | `Query(ge=1, le=<cap>)`. |
| L2 | `api/client_token_store.py:39-251` | Tokens have no expiry/active cap; revoked rows persist and active listing is unbounded. | Document permanent-until-revoked policy or add expiry, cap, cleanup, pagination. |
| L3 | `api/client_token_store.py:254-283` | MCP accessor re-runs `MemorySettings.from_env()` instead of trusting the configured singleton; environment mutation can drift enablement/path. | Configured singleton as SSOT. |
| L4 | `api/oauth_as_register.py:45-59` | Redirect validator permits fragments and embedded userinfo. | Explicit rejection and supported-profile normalization. |
| L5 | `api/oauth_as_register.py:157-166` | Unknown requested scopes are silently dropped; registration can succeed with an empty effective scope. | Reject unsupported scope metadata. |
| L6 | `api/oauth_authorize.py:454-467` | Consent session cookie is `SameSite=Strict`, so normal cross-site authorization GETs do not use the advertised one-click session. | Use a profile compatible with top-level authorization navigation, normally Lax. |
| L7 | `api/routes_handlers.py:207-212` | Unknown caller-controlled backend operation raises `RuntimeError` and becomes 500. | Return standard 404/422. |
| L8 | `api/oauth_token.py:27-31,44-45` | `_access_ttl_s()` and `_signing_kid()` are exported but unused by production token issuance. | Remove, test as intentional compatibility API, or route through one SSOT. |

## Auth-Tier Enforcement Matrix

| Surface | None/loopback | Static | Client token | OAuth | Result |
|---|---|---|---|---|---|
| Health/readiness | public | public | public | public | readiness disclosure M12 |
| Readonly REST | empty tier skips | readonly+ | registry readonly+ | read scope+ | normal gate holds |
| Agent REST | empty tier skips | agent+ | registry agent+ | write scope+ | normal gate holds; H1 identity override |
| Generic destructive REST | empty tier skips | operator | operator token | admin scope | total operation map holds |
| Phase 3 reset | open | **agent+** | **agent+** | **write scope+** | **C2** |
| Phase 3 run | open | agent+ | agent+ | write scope+ | H2 unbounded cost |
| MCP catalog/invocation | unfiltered | tier + caller label | tier + credential identity | tier + token identity | C1 in static mode |
| MCP namespace pin | caller label | caller label | credential-bound | token-bound | static mode bypass C1 |
| Explorer direct loopback | public | public | public | public | intended exception |
| Explorer via same-host proxy, upstream loopback | public | public | public | public | **C3** |
| Client-token bootstrap | loopback empty-store | operator key | operator/empty-store bootstrap | n/a | atomic and proxy-aware |
| OAuth DCR | protocol-public | same | same | same | limiter holds; cap race M4 |
| OAuth authorize | consent/admin secret | same | same | same | strong redirect/PKCE; M3/M7/L6 |
| OAuth token | code + PKCE | same | same | same | M1 early consumption |
| Local stdio MCP | operator trust | operator trust | operator trust | operator trust | explicit local-process boundary |

The generic backend tier map is total. The failure is composition: dedicated handlers can bypass it, and static/no-auth caller labels are suitable for telemetry but not for selecting enforcement policy. Tool allowlists and namespace pins are MCP-only; REST remains caller-namespace-selectable.

## Required Bug-Class Sweeps

| Sweep | Result |
|---|---|
| Duplicate definitions | No duplicate function/class definition in one lexical scope across the 24 files. |
| Except-only names / pyflakes | Exact command attempted; local environment lacked `pyflakes`. Conservative AST fallback found no except-only name loaded afterward. |
| BaseException / CancelledError cleanup | **M2 confirmed.** |
| Lexicographic timestamp comparisons | No string-timestamp ordering candidate in scope. |
| Module constants never read | No security-relevant constant defect; compatibility helper functions are L8. |
| Keyword contract mismatch | No same-module literal mismatch; cross-module/pinned wrappers were traced manually. |

The committed probe reruns all six sweeps, exact file/line reconciliation, source-chain assertions for every finding, and the optional live M1 check.

## Named-Test Gap Analysis

| Test | Material missing case |
|---|---|
| `test_api_auth.py` | static client-name relabel; loopback Explorer plus forwarding header; hostile Host |
| `test_api_routes.py` | full-auth agent reset; body identity override; >500 dirty namespaces; N+1 instrumentation |
| `test_api_tier_enforcement.py` | dedicated handlers that bypass generic policy |
| `test_auth_code_store.py` | valid retry after wrong client/redirect or bad PKCE |
| `test_auth_mode.py` | Host/Origin requirement in loopback no-auth |
| `test_client_token_tier_auth.py` | expiry/cap, event-loop blocking, settings-snapshot drift |
| `test_config_api_boundaries.py` | maximum Phase 3 call budget and production route gate |
| `test_loopback_auth_safety.py` | Explorer proxy path and DNS rebinding |
| `test_oauth_as_consent_secret.py` | deny-cycle cache bound and browser SameSite behavior |
| `test_oauth_as_e2e.py` | failed exchange then valid retry; noncanonical resource authorization |
| `test_oauth_as_metadata.py` | non-HTTP preflight schemes and malformed credential-bearing ports |

The Phase 3 route test mounts the router without auth middleware, so empty tier deliberately skips enforcement; it proves deletion behavior but cannot catch C2.

## Disproved Candidates / Controls That Held

- DCR limiter settings are applied during app prerequisite construction.
- Fixed-window limiter memory is bounded.
- Token success/error responses set `no-store` and `no-cache`.
- Authorization redirect matching is exact, not prefix-based.
- S256 PKCE is mandatory; downgrade was not found.
- JOSE uses explicit algorithms and checks issuer, time claims, and audience/resource.
- Unknown `kid` raises and reaches the rate-limited refresh path.
- Consent JTI consumption is lock-protected within one process.
- Client-token bootstrap uses atomic conditional insert.
- Raw bearer tokens are not persisted; only SHA-256 hashes are stored.
- Bootstrap rejects proxy forwarding headers.
- Duplicate sensitive headers are rejected.
- Request ContextVars are reset in `finally`.
- Normal ingest/project scan resolves symlinks and confines non-operator paths to configured roots.
- `delete_todo` operator mismatch was not established; policy explicitly treats TODO lifecycle as agent work.
- Artifact corpus audit can scan a caller-selected path, but returned file-body disclosure was not proven; retained as an open boundary question.

## Audit-Type Conclusions

- **A1 Functional:** failed — C2, H1, M1, M7, M9, M14.
- **A2 Security:** failed — C1–C4, H1, plus medium disclosures.
- **A3 Architecture:** total operation policy is good; dedicated routes, caller-selected policy identities, and split host-safety assumptions undermine it.
- **A4 Maintainability:** narrow wrappers are readable, but environment re-reads, singletons, full-body logging, and unused helpers create drift.
- **A5 Performance:** failed — unbounded model budget, event-loop SQLite, N+1 history, JTI growth, and code-row accumulation.
- **A6 Tests:** strong individual happy-path controls; weak adversarial composition across middleware, proxies, dedicated routes, and failed-then-retry transitions.
- **A7 LLM/AI:** agent controls real model budget; dirty-status truncation can misreport consolidation selection.
- **Compliance:** strong redirect/PKCE/cache/JOSE controls; M1, M4–M7, and L4–L6 remain relevant gaps.

## Open Questions

1. Is REST `user_id` a tenant boundary or provenance only? H1 remains High either way; impact differs.
2. Are `/api/phase3/*` benchmark routes intentionally production-mounted?
3. Are client tool/namespace settings security controls or cooperative small-model guidance? Current comments describe enforcement against an untrusted caller.
4. Should namespace pinning apply across REST and MCP?
5. Is detailed public readiness intentional? If yes, it needs a redaction contract.
6. Is permanent-until-revoked client-token lifetime intentional?
7. Should readonly artifact corpus audit accept arbitrary server-side repository paths?

## Executable Evidence and Environment Limits

Run:

```bash
python .agent/audit/m2_functional_probe.py
python .agent/audit/m2_functional_probe.py --strict
```

Local audit-workstation results:

- Probe syntax compilation: **PASS** (`python -m py_compile`, exit 0).
- `python -m pyflakes src/menhir/api`: unavailable; exact output was `/opt/pyvenv/bin/python: No module named pyflakes`.
- Full pytest was not executable locally: the container could not materialize the repository and lacked pinned JOSE/MCP/`archolith_oauth` dependencies.
- Source was read through the authenticated GitHub connector at the exact target commit.
- Pinned OAuth behavior was read at the exact dependency commit.
- Production code changes: **none**. This branch changes only this report and the probe.

Repository PR checks are the authoritative full-suite validation for the audit-only branch.

## Remediation Order

1. C2 destructive tier; C3/C4 local-browser trust and Host protection; C1 credential-bound client identity.
2. H1 identity override and H2 model-budget bound/feature gate.
3. M1/M7 OAuth consumption/resource ordering.
4. M2 startup cleanup, M4 cap atomicity, M3/M8 storage bounds.
5. M10/M13 performance and M11/M12/M15 redaction/tiering.
6. Lower-severity OAuth and lifecycle gaps.

## Review Confidence

**High (0.92).** All 24 scoped files and every Critical/High call chain were read and reconciled. Remaining uncertainty is policy intent around per-client MCP constraints and `user_id` tenancy, not the observed control flow. Dynamic full-suite execution is delegated to repository CI because the local environment lacked a materialized checkout and pinned dependency set.
