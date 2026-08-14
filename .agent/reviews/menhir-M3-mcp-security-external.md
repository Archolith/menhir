# Menhir M3 — Focused MCP Security Audit (External, Reconstructed)

**Repository:** `Archolith/menhir` (requested as `ctharvey/menhir`)  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m3-mcp-security-external`  
**Scope:** `src/menhir/mcp/` — 70 files, 7,222 physical lines  
**Audit question:** what an authenticated caller at one tier can cause or learn beyond the boundary that tier and any configured client namespace/tool restrictions are meant to enforce.  
**Status:** findings reconstructed and line-traced against the pinned commit.

## Reconstruction note

The original completed audit session and its local report/probe disappeared before the branch was pushed. This report preserves the seven findings recorded in that completed pass and revalidates each finding directly against commit-addressed source. The original probe transcript is not reproduced as though it had been rerun. The recovered quantitative results were **54 registered tools** (20 readonly, 16 agent, 18 operator), **9 resources**, and **13/54 tools with a literal `namespace` endpoint parameter**. The source mechanism and concrete affected paths are revalidated below; the vanished probe's exact stdout is not claimed as current execution evidence.

The registration count is independently visible in the four registries: 10 ingest tools (`src/menhir/mcp/tools/ingest/__init__.py:3-14`), 5 recall tools (`src/menhir/mcp/tools/recall/__init__.py:3-9`), 5 conflict tools (`src/menhir/mcp/tools/conflict/__init__.py:3-9`), and 34 ops tools (`src/menhir/mcp/tools/ops/__init__.py:3-74`). They are concatenated and registered through `BaseTool.register()` (`src/menhir/mcp/tools/__init__.py:7-22`; `src/menhir/mcp/contracts.py:367-379`). The nine resources are enumerated and registered separately (`src/menhir/mcp/resources.py:506-521`).

## 1. Executive Summary

The audit found **1 Critical, 2 High, 3 Medium, and 1 Low** security issue.

The central problem is an authorization split inside one MCP surface. Registered tools pass through `BaseTool.execute()`, which applies query-auth restrictions, tier enforcement, the per-client tool allowlist, destructive-operation audit recording, and namespace pinning (`src/menhir/mcp/contracts.py:292-356`). Registered resources use `BaseJsonResource.execute()`, which invokes the backend through telemetry without any of those controls (`src/menhir/mcp/contracts.py:167-237`). Both remote transports register all nine resources alongside the tools (`src/menhir/api/mcp_remote.py:59-111`).

That split makes the configured tier and client policy materially weaker than advertised:

| ID | Severity | Finding |
|---|---|---|
| M3-SEC-01 | Critical | All nine remote MCP resources bypass tier, query-auth, client-tool allowlist, and namespace enforcement. |
| M3-SEC-02 | High | A static-key caller can choose the `client_name` used to select its namespace pin and tool allowlist. |
| M3-SEC-03 | High | Namespace pinning applies only to tools whose endpoint literally declares `namespace`; UUID-based writes remain cross-namespace. |
| M3-SEC-04 | Medium | System resources return raw infrastructure topology and may disclose credentials embedded in the Neo4j URI. |
| M3-SEC-05 | Medium | Explicitly readonly tools expose global episode content, provenance, queue state, errors, model endpoints, and traceback previews without namespace reconciliation. |
| M3-SEC-06 | Medium | Raw tool/resource arguments and exception strings are logged and persisted without field-level redaction. |
| M3-SEC-07 | Low | The tier gate fails open when `BaseTool.execute()` is called with no request tier bound, including through module-level convenience wrappers. |

## 2. Findings

### M3-SEC-01 — Critical — all nine remote MCP resources bypass invocation authorization and client scoping

#### Concrete path

1. The combined server mounts SSE at `/mcp` and Streamable HTTP at `/mcp-http`, then wraps the application in `BearerAuthMiddleware` (`src/menhir/api/server_support.py:193-244`). Authentication therefore establishes a caller tier, but it does not itself decide whether an individual MCP operation is allowed.
2. Both remote FastMCP constructions register every tool **and every memory resource** (`src/menhir/api/mcp_remote.py:59-111`).
3. `TierFilteredFastMCP.list_tools()` filters only the **tool catalog** by client allowlist and tier (`src/menhir/api/mcp_remote.py:31-56`). It does not filter resources.
4. A tool handler calls `BaseTool.execute()`, which applies query-string-auth restrictions, tier enforcement, client allowlist enforcement, operator audit recording, and namespace pinning before calling the endpoint (`src/menhir/mcp/contracts.py:292-356`).
5. A resource handler instead calls `BaseJsonResource.execute()`, which directly builds the payload under `track_mcp_call()` and contains no call to `request_uses_query_auth()`, `get_request_tier()`, `_tier_allows()`, `get_client_tool_allowlist()`, or `get_pinned_namespace()` (`src/menhir/mcp/contracts.py:167-237`).

The nine affected resources include global recent-memory reads, detailed lookup by caller-supplied UUID, search, type/scope listings, lifecycle traces, processing-queue data, dependency checks, and system metadata (`src/menhir/mcp/resources.py:269-521`).

#### Impact

An authenticated readonly caller can invoke the same resources as an operator. A caller authenticated through the MCP `?api_key=` compatibility path also bypasses the special query-auth policy that limits tools to readonly operations plus rate-limited `add_memory`, because that policy exists only in `BaseTool.execute()` (`src/menhir/mcp/contracts.py:43-74,306-323`). A client restricted by `MENHIR_CLIENT_TOOLS` can still use resources because both the list-time and invocation-time allowlist checks operate on tools only (`src/menhir/api/mcp_remote.py:31-56`; `src/menhir/mcp/contracts.py:328-341`). A client pinned by `MENHIR_CLIENT_NAMESPACES` receives unpinned resource results because `BaseJsonResource` never reconciles a resource request with the pin.

This is a confidentiality boundary failure rather than a resource-side write primitive: no state-changing MCP resource was found. The severity reflects the breadth of memory and infrastructure disclosure across all remote tiers and both transports.

#### Recommended remediation

Give resources the same mandatory policy wrapper as tools. Each resource should declare a minimum tier; client allowlists should either cover resource URIs explicitly or use one unified capability policy; query-auth policy must apply before resource execution; and every memory-selecting resource must derive or reconcile namespace from the authenticated session rather than only from a caller URI variable. Unknown or unclassified resource operations should fail closed.

---

### M3-SEC-02 — High — static-key callers can self-select the identity used for namespace and tool policy

#### Concrete path

1. Static bearer mode resolves only the tier from the presented shared key (`src/menhir/api/auth.py:200-210,391-411`).
2. `_request_session_headers()` trusts `x-menhir-client-name` / legacy `x-yawn-client-name`; if the header is absent, MCP callers may supply `client_name` in the query string (`src/menhir/api/auth.py:208-286`).
3. Static mode calls `_request_session_headers()` without setting `trust_identity_headers=False`, then binds the returned caller-selected `client_name` into the request session (`src/menhir/api/auth.py:391-411`).
4. `get_pinned_namespace()` and `get_client_tool_allowlist()` look up policy solely by the lowercased bound `session.client_name`. Missing session/name/config entries return an empty value, and an empty allowlist means unrestricted (`src/menhir/mcp/service_access.py:189-232`).
5. `BaseTool.execute()` enforces the policy returned for that selected name (`src/menhir/mcp/contracts.py:282-300,328-346`).

A holder of a shared readonly, agent, or operator static key can therefore present the name of an unconfigured client and obtain no namespace pin and no tool allowlist, or present another configured name and receive that identity's policy.

#### Impact

`MENHIR_CLIENT_NAMESPACES` and `MENHIR_CLIENT_TOOLS` are not security boundaries in static-key mode unless every holder of a shared key is trusted to identify itself honestly. The apparent server-side policy is keyed by a caller-controlled label.

OAuth and per-client-token modes do **not** share this specific defect: those paths bind the registered/token-derived identity and call `_request_session_headers(..., trust_identity_headers=False)` (`src/menhir/api/auth.py:529-551,587-615`).

#### Recommended remediation

Ignore all identity headers and `client_name` query metadata on protected static-key requests, or map each static key to a fixed server-side client identity. Prefer per-client tokens or OAuth for remote clients. Where namespace/tool configuration exists, an unknown client identity should fail closed instead of meaning unrestricted.

---

### M3-SEC-03 — High — namespace pinning is conditional on endpoint signature and does not protect UUID-addressed writes

#### Concrete path

`BaseTool._accepts_namespace()` uses `inspect.signature(self.endpoint)` and returns true only when the endpoint contains a parameter literally named `namespace`. `_apply_pinned_namespace()` returns the original kwargs unchanged when the tool does not accept that parameter (`src/menhir/mcp/contracts.py:271-300`).

The recovered AST enumeration found that only **13 of 54** registered tools accepted `namespace`, leaving 41 outside the forcing mechanism. This is not merely a cosmetic count. State-changing tools operate on global UUIDs without any namespace argument:

- `flag_memory(node_uuid, bootstrap_scope)` is an agent-tier-by-default mutation and calls `backend.flag_memory(node_uuid, ...)` without a namespace (`src/menhir/mcp/tools/ingest/flag_memory.py:8-49`; default tier at `src/menhir/mcp/contracts.py:239-245`).
- `delete_memory(node_uuid)` is operator-only but calls `backend.delete_memory(node_uuid)` without a namespace (`src/menhir/mcp/tools/ingest/delete_memory.py:8-37`).

A pinned caller can therefore take a UUID learned through a global read path and invoke one of these operations. The pin is never injected because the endpoint signature cannot receive it; the MCP endpoint performs no object-ownership reconciliation before mutation.

#### Impact

Namespace pinning constrains argument-shaped operations, not object ownership. It does not prove that a UUID belongs to the caller's namespace. Agent-tier callers can change retention state across silos; operator-tier callers can delete cross-silo nodes. Similar risk applies to other UUID-addressed lifecycle, conflict, TODO, artifact, and enrichment operations that omit a namespace argument.

#### Recommended remediation

Move namespace authorization below the transport boundary. Every backend read or mutation of a namespace-owned object should receive the server-derived caller namespace and verify the target object's namespace before returning or changing it. Tool registration should require explicit scope metadata such as `scope_mode = "namespace" | "global-operator" | "session"`; do not infer security policy from whether a Python function happens to have an argument named `namespace`.

---

### M3-SEC-04 — Medium — system resources expose raw infrastructure endpoints and may return URI userinfo

#### Concrete path

`_neo4j_dependency_snapshot()` parses the configured URI to derive host and port but returns the original unredacted `settings.neo4j_uri` in the response (`src/menhir/mcp/resources.py:221-235`). If that URI is configured as `neo4j://user:password@host:7687`, the response includes the userinfo.

`SystemMetadataResource` additionally returns session identifiers, Neo4j URI/database, local LLM endpoint, scheduler endpoint, provider kinds, model names, queue depth, failed-enrichment count, scheduler state, and the graph overview (`src/menhir/mcp/resources.py:287-337`). Under M3-SEC-01 these resources have no resource-level tier requirement and no client allowlist restriction.

#### Impact

At minimum, the lowest authenticated tier receives detailed internal topology useful for reconnaissance. Deployments that place credentials in the Neo4j URI may disclose them directly. The dependency resource also gives callers a reachability oracle for the configured graph endpoint, although the target is server configuration rather than caller-controlled input.

#### Recommended remediation

Redact URI userinfo before any response or diagnostic rendering. Return only a scheme, normalized host class, database name where necessary, and boolean health state. Make detailed system metadata operator-only and separate safe health/status resources from configuration disclosure.

---

### M3-SEC-05 — Medium — readonly tools expose global memory and operational data without namespace reconciliation

This finding is separate from the ungated resources: these are ordinary tools that correctly pass the tier gate but are intentionally declared `readonly` and still have no namespace ownership check.

#### Concrete paths

- `get_provenance` is readonly, accepts only `node_uuid` and `content_chars`, calls `fetch_node_receipts(node_uuid)`, and returns source episode UUIDs, source labels, timestamps, up to 5,000 characters of each episode's content, evidence, and structural anchor paths (`src/menhir/mcp/tools/ops/get_provenance.py:12-84`).
- `get_episode_trace` is readonly, accepts only `episode_uuid` and `limit`, and returns processing owner, lease data, active task/kind/model/endpoint, current error, task details, failure details, error strings, and `traceback_preview` (`src/menhir/mcp/tools/ops/get_episode_trace.py:10-99`).
- `list_enrichment_queue` is readonly, accepts only state and limit, and returns global row UUIDs, stages, attempts, owner, lease/heartbeat timestamps, and processing errors (`src/menhir/mcp/tools/ops/list_enrichment_queue.py:11-66`).

None of those endpoint signatures accepts `namespace`, so `_apply_pinned_namespace()` cannot constrain them (`src/menhir/mcp/contracts.py:271-300`).

#### Impact

A readonly caller who knows or discovers a UUID can retrieve source memory content and operational traces across namespace boundaries. Queue listings disclose active work and errors globally. Model and endpoint fields reveal internal processing topology. These reads also provide identifiers that can be reused against UUID-addressed mutation tools when the caller has a higher tier.

#### Recommended remediation

Apply server-derived namespace filters in the backend methods and require object ownership before UUID lookup. Split operational diagnostics from ordinary readonly memory access; detailed traces, errors, owners, endpoints, and traceback previews should be operator-only unless a namespace-scoped support role is explicitly designed.

---

### M3-SEC-06 — Medium — raw MCP arguments and exception text are persisted or logged without field-aware redaction

#### Concrete path

1. `BaseTool.call_payload()` returns kwargs verbatim, or positional arguments as a list; `BaseJsonResource.call_payload()` follows the same pattern (`src/menhir/mcp/contracts.py:209-215,255-262`).
2. Tool wrappers place sensitive inputs directly in those kwargs. For example, `add_memory` passes complete memory `text`, `source`, optional git `diff`, namespace, and evidence UUID into `AddMemoryTool.execute()` (`src/menhir/mcp/tools/ingest/add_memory.py:10-47`).
3. `track_mcp_call()` computes `payload_preview = _preview_of(payload)` before execution, persists that preview on timeout, failure, and success, and logs the complete diagnosed exception string on failure (`src/menhir/mcp/telemetry/tracker.py:40-136`).
4. `_preview_of()` performs plain JSON serialization and keeps the first 500 characters; it has no key-based or value-based redaction (`src/menhir/infrastructure/telemetry/helpers.py:42-50`).
5. The telemetry store writes `error` and `payload_preview` into SQLite and exposes them through `fetch_recent()` (`src/menhir/infrastructure/telemetry/event_store.py:13-76`).

#### Impact

Memory content, diffs, notes, labels, paths, namespaces, UUIDs, and similar caller data can be written into `.agent/mcp_telemetry.db`. Backend/driver exception messages are logged and stored verbatim after only diagnostic prefixing, so exceptions that contain query values, file paths, endpoints, or content fragments become durable telemetry (`src/menhir/mcp/telemetry/tracker.py:20-38,82-107`).

No normal path was found that deliberately places the bearer token itself in a tool/resource payload; the middleware strips a query `api_key` before downstream handling in static and client-token modes (`src/menhir/api/auth.py:397-401,526-532`). The confirmed issue is unredacted operation content and error text, not a demonstrated raw-token log.

#### Recommended remediation

Replace generic payload previews with per-operation safe schemas. Redact or hash content, diffs, notes, paths, source strings, namespaces, and identifiers before persistence; ideally store only field names, lengths, and bounded classification metadata. Sanitize exception strings before both logging and SQLite persistence. Make telemetry retention, permissions, and operator visibility explicit.

---

### M3-SEC-07 — Low — direct calls fail open when the request tier is unbound

#### Concrete path

The shared request-tier `ContextVar` defaults to an empty string, and `get_request_tier()` documents that empty value as the unbound result (`src/menhir/core/request_context.py:14-20,67-74`). `BaseTool.execute()` rejects a caller only when `tier` is truthy:

```python
if tier and not _tier_allows(tier, self.required_tier):
    raise PermissionError(...)
```

That condition is at `src/menhir/mcp/contracts.py:323-327`.

Tool modules expose importable convenience functions that instantiate the class and call `.execute()` directly. For example, the operator-only `delete_namespace()` wrapper reaches `DeleteNamespaceTool().execute(...)` (`src/menhir/mcp/tools/ops/delete_namespace.py:8-37`). A Python caller invoking that wrapper outside an HTTP or stdio request context receives the empty tier and skips the authorization rejection.

#### Reachability and impact

This is not a default LAN bypass. Authenticated HTTP requests bind a real tier, and the stdio entry point explicitly binds operator trust before starting the server (`src/menhir/mcp/server.py:48-60`; `src/menhir/mcp/service_access.py:234-260`). The defect affects internal callers, tests, plugins, accidental direct imports, or future entry paths that forget to bind context. Because the default is authorization success rather than failure, a new entry path can silently acquire operator behavior.

#### Recommended remediation

Make an unbound tier fail closed in `BaseTool.execute()`. Preserve stdio behavior by retaining the explicit operator binding. If trusted in-process callers require a bypass, expose a deliberately named internal API or explicit trust context rather than treating missing context as authorization.

## 3. Entry-Path and Enforcement Summary

| Entry path | Authentication / identity | Invocation controls | Result |
|---|---|---|---|
| Remote SSE `/mcp` | `BearerAuthMiddleware` binds auth mode, session and tier (`src/menhir/api/server_support.py:193-244`; `src/menhir/api/auth.py:300-411`) | Tools pass `BaseTool.execute()`; resources pass only `BaseJsonResource.execute()` | Tool gate is structurally reached; resource gate is absent. |
| Remote Streamable HTTP `/mcp-http` | Same outer middleware | Same registrations and split execution path (`src/menhir/api/mcp_remote.py:92-111`) | Same resource bypass. |
| Local stdio | Explicit operator binding (`src/menhir/mcp/server.py:48-60`) | Same tool/resource classes | Tools are intentionally operator-trusted; resources remain unclassified. |
| Direct Python convenience wrapper | Whatever context the caller happened to bind | Calls `.execute()` directly | Empty-tier tool calls fail open; resources have no tier check at all. |

For registered tools, enforcement is structurally centralized rather than opt-in: `register_all_tools()` instantiates each class and `BaseTool.register()` installs a handler that calls `self.execute()` (`src/menhir/mcp/tools/__init__.py:17-22`; `src/menhir/mcp/contracts.py:367-379`). The security problems arise from policy inputs and the separate resource base, not from ordinary tool classes bypassing registration.

## 4. Disproved or Narrowed Candidates

- **No state-changing MCP resource was found.** M3-SEC-01 is a broad read-side authorization and confidentiality failure, not a demonstrated resource-side mutation.
- **Normal registered tool dispatch does not depend on each tool author remembering a decorator or tier check.** The shared `BaseTool.register()` handler always calls `execute()` (`src/menhir/mcp/contracts.py:367-379`). Individual tools can still omit a stricter `required_tier`, but they do not bypass the common wrapper when reached through normal FastMCP registration.
- **The static `client_name` spoofing issue is narrowed to static/no-auth cooperative identity.** Client-token and OAuth paths bind verified/registered identities with `trust_identity_headers=False` (`src/menhir/api/auth.py:529-551,587-615`).
- **The stdio server does not rely on the empty-tier fail-open behavior.** It explicitly binds operator tier (`src/menhir/mcp/server.py:48-60`).
- **No arbitrary shell-command, raw-Cypher, or prompt-template injection was confirmed in the scoped MCP transport code.** Caller text does reach backend recall and ingestion operations, but the confirmed transport-layer issue is telemetry/content exposure; query construction and model prompt defenses live outside this module and were not converted into an unsupported finding.

## 5. Coverage and Quantitative Context

The original completed pass reconciled the requested scope as follows:

| Group | Files | Lines | Status |
|---|---:|---:|---|
| `mcp/*.py` | 8 | 2,106 | READ |
| `mcp/telemetry/` | 2 | 170 | READ |
| `mcp/tools/*.py` | 2 | 29 | READ |
| `mcp/tools/ingest/` | 11 | 803 | READ |
| `mcp/tools/recall/` | 6 | 1,225 | READ |
| `mcp/tools/ops/` | 35 | 2,413 | READ |
| `mcp/tools/conflict/` | 6 | 476 | READ |
| **Total** | **70** | **7,222** | **70/70 READ** |

Recovered completed-pass counts:

- 54 registered tools: 20 readonly, 16 agent, 18 operator.
- 9 registered resources.
- 53 module-level convenience wrappers.
- 13/54 tool endpoints accepted a literal `namespace`; 41/54 did not.

The current reconstruction directly re-read the central contract, remote transport, auth identity binding, namespace policy, resource implementations, representative readonly tools, representative cross-namespace mutations, telemetry path, request context, and stdio composition at the pinned commit. It did **not** rerun the vanished AST probe or independently repeat `wc -l` in a materialized checkout, so the recovered numerical counts are not presented as fresh execution output.

## 6. What Was Checked and Environment Limits

### Checked directly at the pinned commit

- Remote SSE and Streamable HTTP registration and middleware composition.
- Tool versus resource registration and invocation wrappers.
- Query-auth, tier, client allowlist, destructive audit, and namespace pin enforcement sites.
- Static-key, client-token, OAuth, no-auth, and stdio identity/tier binding relevant to these findings.
- All nine resource implementations and the sensitive fields they return.
- Concrete readonly provenance, trace, and queue tools.
- Concrete UUID-addressed agent/operator mutations that cannot receive a namespace pin.
- Raw payload-preview and exception persistence from MCP call to SQLite.
- Direct module wrapper behavior when request context is absent.

### Not rerun in this reconstruction

- The original security probe's exact stdout and self-test transcript.
- A fresh filesystem `wc -l` reconciliation.
- Live FastMCP requests against a running Menhir server.
- Backend/Neo4j execution proving a cross-namespace object mutation with production data.

Those limitations do not remove the static authorization mismatches: each finding cites the exact caller path and control omission. They do reduce confidence in the recovered aggregate counts and in runtime behavior supplied by dependencies outside the inspected code.

## 7. Review Confidence

**86 / 100.** The seven issues are directly line-traced against the pinned source, and the highest-risk paths are structurally explicit. Confidence is reduced because the original probe artifact disappeared, the current environment did not rerun its exhaustive count, and no live server/backend exploit was executed during reconstruction.
