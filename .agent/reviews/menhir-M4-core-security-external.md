# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root  
**Measured scope:** 5,097 lines (reconciles exactly with the declared total)  
**Status:** DRAFT — all scope files read; both transport gates traced; adversarial redaction/preflight execution complete; downstream-sink and repository sweeps remain

> Resume rule: Section 13 is the source-of-truth checkpoint. All 23 scope rows are `READ`. Resume with Section 8 downstream sinks, then Section 10 repository sweeps; do not reread scope or rerun completed adversarial probes.

## 1. Executive Summary, highest-risk result first

### M4-SEC-01 — High — readonly and unauthenticated-loopback callers can approve or reject candidates through Explorer

The canonical backend policy classifies `promote_candidate`, `reject_candidate`, and `approve_candidate` as operator operations (`src/menhir/api/routes_support.py:624-651`). The mounted Explorer instead exposes `POST /explorer/candidates/{uuid}/approve` and `/reject` and calls `candidate_service.approve()` / `.reject()` directly, with no tier check (`src/menhir/explorer/app.py:832-844`). Explorer is mounted into the live app when enabled (`src/menhir/api/server_support.py:193-221`). A remote caller authenticated only at readonly tier passes the middleware and reaches these handlers; additionally, the middleware deliberately bypasses all Explorer authentication for a direct loopback peer (`src/menhir/api/auth.py:300-386`). Consequence: a lower tier can make operator-classified memory-governance decisions. The preconditions are `explorer_enabled`, a resolvable candidate UUID, and either any valid readonly credential remotely or direct loopback access.

### M4-SEC-02 — High — static-key callers can self-select `client_name` and bypass MCP namespace/tool restrictions

In static bearer mode, request identity headers are trusted; caller-controlled `x-menhir-client-name` (or MCP `client_name` query metadata) becomes the bound session client name (`src/menhir/api/auth.py:208-286`, `378-411`). MCP namespace pins and tool allowlists are selected solely by that bound client name, while an absent/unconfigured name means unrestricted (`src/menhir/mcp/service_access.py:189-232`). `BaseTool.execute()` correctly forces the selected pin and enforces the selected allowlist (`src/menhir/mcp/contracts.py:282-346`), but a holder of a shared static tier key can evade both by naming an unconfigured client. OAuth and per-client-token modes derive identity from validated credentials and do not share this defect.

### M4-SEC-03 — High — REST ignores server-configured client namespace pins enforced by MCP

MCP forcibly overwrites a tool's caller-supplied namespace with `MENHIR_CLIENT_NAMESPACES[client_name]` (`src/menhir/mcp/contracts.py:282-300`). REST `_resolve_namespace()` instead accepts the request-body namespace first, then `x-menhir-namespace`, and never consults the authenticated client's configured pin (`src/menhir/api/routes_support.py:128-143`). Core performs no ownership check. A client restricted to one namespace on MCP can reuse the same authenticated HTTP access to read or write a different namespace through REST. This is a transport-asymmetric privilege escalation bounded by deployments that rely on namespace pins as an access-control boundary.

### M4-SEC-04 — High — generic backend failures log complete caller bodies without redaction

`POST /api/internal/backend/{operation}` accepts a generic dictionary and invokes the selected runtime method. On any non-preset exception, the handler logs `body=%r` with `logger.exception()` and re-raises (`src/menhir/api/routes_handlers.py:199-231`). Bodies for this surface can contain full memory episodes, diffs, paths, scan dictionaries, user/session identifiers, and other caller content (`src/menhir/api/routes_support.py:544-651`). Therefore ordinary backend failures can place secrets or private memory content into server logs. The externally returned 500 is generic, but the log disclosure is direct and remote-triggerable. Executed logging reproduction remains scheduled for Section 9.

### M4-SEC-05 — Medium — REST lets authenticated callers forge memory user/session attribution

`MemoryRequest` exposes optional `user_id` and `session_id` without binding them to the authenticated principal (`src/menhir/api/routes_support.py:288-299`). The agent-tier `/api/memory` handler deliberately replaces the bound caller session when either field is supplied and forwards those values to `queue_episode()` (`src/menhir/api/routes.py:305-333`). This remains true in OAuth and per-client-token modes, where middleware identity itself is verified. MCP's `add_memory` path instead derives identity from the bound request session. Consequence is provenance/session forgery rather than a proven tier escalation, so severity is Medium.

### M4-SEC-06 — Medium — guarded ingest has an agent-reachable unguarded compatibility rescan path

`ingest_document()` and `scan_and_write_project()` call `ensure_ingest_path_allowed()` before reading/scanning (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). `write_project_structure()` accepts a caller-supplied scan dictionary; when `symbols` is absent, it schedules `_background_symbol_rescan()` on caller-controlled `root_path` (`src/menhir/core/backend_runtime_data_ops.py:428-443`). The rescan checks only `os.path.isdir(root)` and invokes `ProjectScanner.scan(root, name)` without the guard (`src/menhir/core/backend_runtime_data_ops.py:453-481`). The internal REST policy explicitly exposes `write_project_structure` at agent tier and the generic dispatcher invokes it (`src/menhir/api/routes_support.py:544-651`; `src/menhir/api/routes.py:742-759`). This bypasses the containment control with limited, asynchronous reach; downstream data visibility remains under analysis.

### M4-SEC-07 — Medium — privacy redaction fails open for structured, non-string, and case-variant protected fields

Execution of the exact pinned `privacy.py` AST proved that only exact lowercase protected keys containing non-empty strings are reliably replaced with `[hidden]`. Beneath protected keys, a nested dictionary, nested list, and integer remained visible; `Content` and `SUMMARY` case variants remained visible; `None` and the empty string also passed through. A 10,000-character protected string was masked. Log execution masked a qualifying double-quoted phrase but left a single-quoted contraction, an unquoted phrase, and a malformed quoted phrase intact (`src/menhir/privacy.py:49-82`, `103-162`; `.agent/audit/m4_security_probe.py:557-674`). Explorer uses these helpers as a privacy display control, so protected memory content can remain visible in pathological but realistic row shapes or log syntax. This is a limited display-layer information disclosure, not graph/storage disclosure.

### M4-SEC-08 — Low — preflight treats attacker-looking hostname prefixes as local and suppresses provider authorization

Execution of the exact pinned predicate returned `True` for `http://localhost.evil.example/v1`, `http://127.0.0.1.evil.example/v1`, and `http://localhost@evil.example/v1`. `check_llama_connectivity()` therefore omits its configured bearer header for those non-loopback URL forms (`src/menhir/core/runtime_preflight.py:94-96`, `157-178`; `.agent/audit/m4_security_probe.py:596-674`). The base URL is configuration-controlled and the defect withholds rather than discloses a credential, so consequence is limited to an incorrect trust classification and unauthenticated preflight request to an operator-selected endpoint.

### Architectural result — authorization is absent from the shared backend contract

`MemoryBackend` contains no authenticated principal, tier, or namespace-ownership proof in any method signature (`src/menhir/core/backend_protocol.py:43-683`). `RuntimeProvider` composes both data and admin mixins into one object and stores no authorization policy (`src/menhir/core/backend_runtime_ops.py:5-12`; `src/menhir/core/backend_runtime.py:14-41`). Except for two filesystem-ingest guard calls, core executes whatever operation and attribution its transport supplied.

## 2. Trust Boundary Register — every caller assumption, whether each transport enforces it, with the call chain

| Core assumption | REST enforcement | MCP enforcement | Result / evidence |
|---|---|---|---|
| **Tier:** the caller is entitled to the selected runtime action. | Generic backend route maps operations to readonly/agent/operator, but Explorer candidate writes bypass that map. Empty tier skips `_require_tier()` (`src/menhir/api/routes_support.py:24-34`, `624-661`; `src/menhir/explorer/app.py:832-844`). | `BaseTool` checks `required_tier`, but only when tier is nonempty; stdio explicitly binds operator and authenticated HTTP binds a real tier (`src/menhir/mcp/contracts.py:305-333`; `src/menhir/mcp/service_access.py:234-250`). | **Violated by REST Explorer (M4-SEC-01).** Empty-tier fail-open is not a default remote bypass because nonloopback no-auth startup is rejected unless explicitly overridden; retain as defense-in-depth concern. |
| **Identity:** `user_id`, `session_id`, client identity, and reviewer attribution belong to the authenticated caller. | Middleware binds verified identity in OAuth/client-token modes, but `/api/memory` accepts body identity overrides; static mode also trusts identity headers (`src/menhir/api/auth.py:208-286`; `src/menhir/api/routes.py:305-333`). | Memory tools derive user/session from the bound request session; static mode still trusts caller-selected client name (`src/menhir/mcp/tools/ingest/add_memory.py:43-91`; `src/menhir/api/auth.py:378-411`). | **Violated by REST memory attribution (M4-SEC-05); static client identity is not trustworthy enough to key restrictions (M4-SEC-02).** |
| **Namespace ownership:** a caller-selected namespace is authorized for that principal/client. | `_resolve_namespace` accepts body/header values; no ownership or client-pin check (`src/menhir/api/routes_support.py:128-143`). | Configured client pin forcibly overrides tool input, but unpinned clients have no ownership check (`src/menhir/mcp/contracts.py:282-300`). | **Transport mismatch M4-SEC-03.** Neither transport implements general namespace ownership. |
| **Filesystem path:** agent/operator may make the server read/scan the selected host path only within policy. | Primary ingest methods consult request tier and guard; generic agent operation `write_project_structure` can trigger an unguarded rescan (`src/menhir/core/backend_runtime_data_ops.py:305-481`; `src/menhir/api/routes_support.py:624-651`). | `ingest_document` checks `isfile` then delegates to guarded core; `ingest_project` delegates to guarded scan. No public MCP tool for `write_project_structure` was found in the registered tool groups (`src/menhir/mcp/tools/ingest/ingest_document.py:43-73`; `src/menhir/mcp/tools/__init__.py:8-21`). | **REST-only guard bypass M4-SEC-06.** |
| **Input shape:** operation dictionaries and `query_structure` params match the selected implementation. | Pydantic validates named REST models, but internal backend body is arbitrary `dict[str, Any]`; dispatcher passes `**body` directly (`src/menhir/api/routes.py:742-759`; `src/menhir/api/routes_handlers.py:199-225`). | FastMCP/tool signatures and `BaseTool` typed endpoints constrain public tool calls; remote backend client still sends generic JSON internally. | **Only partially enforced.** Malformed internal bodies become `TypeError` and are fully logged (M4-SEC-04). |
| **Input size:** text, diffs, paths, scan dictionaries, query params, and identifiers are bounded before core. | Limits exist for selected numeric fields, but `MemoryRequest.episode`, `diff`, identity, source, and namespace have no maximum; generic backend body has no declared size/field bound (`src/menhir/api/routes_support.py:274-299`, `544-625`). | Tool annotations constrain types but most strings/collections have no explicit size bounds; FastMCP/ASGI deployment limits were not found in this layer. | **Not enforced consistently.** Record as Low unless a resource-exhaustion or log-amplification reproduction proves stronger consequence. |
| **Background-warning scope:** the supplied session ID uniquely identifies the authenticated caller. | Generic route drains by bound caller session, while producers may push by payload-controlled session (`src/menhir/api/routes_handlers.py:217-237`; `src/menhir/core/backend_shared.py:25-47`). | Tools normally derive session from the bound session; warnings are appended verbatim after a call (`src/menhir/mcp/contracts.py:347-355`). | **Partially enforced; collision/cross-session proof pending.** |
| **Redaction contract:** protected fields are canonical lowercase strings and log text matches the quote heuristic. | Explorer explicitly uses redaction helpers for memory rows/details; nested special cases are handled manually in some views (`src/menhir/explorer/app.py:538-583`, `607-638`). | No MCP response redaction layer was identified; authorized tools intentionally return memory content. | **Confirmed display-layer mismatch M4-SEC-07.** Exact lowercase strings mask, but structured/non-string/case-variant values and several log syntaxes fail open. |

**REST generic call chain:** bearer/client-token/OAuth middleware binds tier/session → `POST /api/internal/backend/{operation}` → `_required_tier_for_operation()` → `RuntimeProvider.<operation>(**body)` (`src/menhir/api/auth.py:300-411`; `src/menhir/api/routes.py:742-759`; `src/menhir/api/routes_handlers.py:199-231`).  
**MCP call chain:** same HTTP middleware (or explicit stdio operator binding) → FastMCP handler → `BaseTool.execute()` tier/allowlist/pin gates → tool endpoint → local `RuntimeProvider` or authenticated `BackendClient` (`src/menhir/mcp/contracts.py:282-367`; `src/menhir/mcp/service_access.py:234-314`).

## 3. Authorization Surface — privileged actions and what gates them

No scope function makes a general authorization decision. `request_context.py` stores tier/auth mode, but core reads tier only for `ingest_document()` and `scan_and_write_project()`. The following core actions trust transport authorization:

- memory/namespace: `queue_episode`, `flag_memory`, `unflag_memory`, `promote_memory`, `delete_memory`, `delete_namespace`, `enqueue_pending_episode` (`src/menhir/core/backend_runtime_data_ops.py:24-143`);
- host filesystem/structure: `ingest_document`, `scan_and_write_project`, `write_project_structure`, `_background_symbol_rescan`, `query_structure` (`src/menhir/core/backend_runtime_data_ops.py:305-513`);
- conflict/enrichment/scheduler: all conflict resolution, reset/release/recovery, takeover/pause/resume methods (`src/menhir/core/backend_runtime_admin_ops.py:25-207`);
- diagnostics/telemetry: operation/failure/lifecycle/task-event reads, `record_conflict_resolution`, memory overview, circuit/cache state, provider configuration (`src/menhir/core/backend_runtime_admin_ops.py:209-319`);
- todo/artifact/temporal/candidate mutation: all methods from `create_todo` through `approve_candidate` (`src/menhir/core/backend_runtime_admin_ops.py:321-603`).

**Representative REST trace:** readonly bearer → Explorer approve handler → `CandidateService.approve()`; no operator gate (M4-SEC-01).  
**Representative MCP trace:** operator bearer/stdio binding → `BaseTool.execute()` → operator tool endpoint → backend method; tool allowlist and namespace pin are applied before endpoint (`src/menhir/mcp/contracts.py:282-355`).  
**Representative generic REST trace:** agent bearer → `write_project_structure` dispatch → unguarded background rescan (M4-SEC-06).

## 4. Redaction Verification — executed adversarial inputs and real output

The probe compiles and executes selected exact AST nodes from the pinned files without importing the Menhir package (`.agent/audit/m4_security_probe.py:557-674`). The selected-AST hashes were recorded so the executed code fragment is identifiable.

**Command executed in the audit harness:**

```text
python -c "import json,runpy; from pathlib import Path; n=runpy.run_path('/mnt/data/m4_security_probe.py'); print(json.dumps(n['run_adversarial'](Path('/mnt/data/m4_exact')), indent=2, sort_keys=True))"
```

**Relevant literal output:**

```text
PRIVACY_AST_SHA256=5b99d76f1ce6f1e9b8accf60a0a26163fc00c831fb83b86d506a6726b73ce8f9
PREFLIGHT_AST_SHA256=4ebf9f40e0604ce6a5a5959b43b55a520114aae972234822f36afa19f771cc44
MAPPING_RESULT={"Content": "case-variant secret", "SUMMARY": "upper-case secret", "content": "[hidden]", "label": "", "name": null, "notes": 8675309, "preview": ["nested list value", {"token": "abc"}], "summary": {"secret": "nested dict value"}, "summary_preview": "[hidden]", "uuid": "structural-uuid"}
OVERSIZED='[hidden]'
REVEAL_SAME_OBJECT=True
LOG_1_OUT='2026-08-13 12:00:00,000 - menhir.test - INFO - content="[hidden]"'
LOG_2_OUT="2026-08-13 12:00:00,000 - menhir.test - INFO - content='Alice's confidential launch plan'"
LOG_3_OUT='2026-08-13 12:00:00,000 - menhir.test - INFO - content=Alice confidential launch plan'
LOG_4_OUT='malformed content="Alice confidential launch plan'
LOG_5_OUT='2026-08-13 12:00:00,000 - menhir.test - INFO - id="abc_def" content="[hidden]"'
BYPASS 'http://127.0.0.1:8080' -> True
BYPASS 'http://localhost:8080' -> True
BYPASS 'http://localhost.evil.example/v1' -> True
BYPASS 'http://127.0.0.1.evil.example/v1' -> True
BYPASS 'http://localhost@evil.example/v1' -> True
BYPASS 'https://localhost:8443' -> False
BYPASS 'http://[::1]:8080' -> False
```

The full JSON output SHA-256 is `6c6aa632e06557bf24c9adbaf31779495a1a69238427f5bc6d2469e3656e06f0`. The mapping control is **fail closed only for exact lowercase, non-empty string fields**, including the oversized string; it is **fail open** for nested dictionaries/lists, numeric values, case-only key variants, `None`, and empty text (`src/menhir/privacy.py:49-82`). The log helper is intentionally heuristic and execution confirms it fails open for contractions in single quotes, unquoted content, and malformed quotes, while masking qualifying double-quoted text (`src/menhir/privacy.py:103-162`). `reveal=True` returning the original object is intended passthrough behavior, not a finding.

## 5. Diagnostics Exposure — operator_diagnostics.py reachability by tier

`build_operator_diagnostics()` exposes bind host/port, loopback status, auth mode, key-presence booleans, no-auth override, OAuth consent/proxy checks, MCP backend diagnostics, and warning/check details, but not raw key values (`src/menhir/operator_diagnostics.py:42-297`). The located call site is the local `menhir diagnostics` CLI; no REST route or registered MCP tool call has yet been found. Treat remote exposure of this specific function as **disproved pending final controlled call-site sweep**.

A separate core method, `get_provider_config()`, returns Neo4j URI/database, LLM/embed endpoints, backend URL, provider kinds, and model names (`src/menhir/core/backend_runtime_admin_ops.py:296-319`). Because it falls into the generic dispatch's readonly remainder, any readonly REST credential can invoke `/api/internal/backend/get_provider_config` (`src/menhir/api/routes_support.py:544-661`). This is a **Medium information-disclosure candidate**; MCP reachability remains to be enumerated.

## 6. Startup and Credential Handling — preflight fail-open/closed, bootstrap file modes and logging

Runtime preflight checks interpreter, Graphiti, Neo4j, schema dimensions, provider compatibility/configuration, and provider connectivity (`src/menhir/core/runtime_preflight.py:98-456`). `_initialize_services()` fails closed only for interpreter or Neo4j failure; other failed checks produce degraded startup and continue (`src/menhir/core/runtime.py:442-469`). Core preflight does not validate HTTP bind/auth safety; settings construction performs that separate guard.

`bootstrap.py` writes no credential file and therefore establishes no file mode. It passes Neo4j credentials and provider keys/configuration into collaborators (`src/menhir/core/bootstrap.py:160-193`). Adapter-construction and edge-sync exceptions are logged/stored verbatim (`src/menhir/core/bootstrap.py:181-193`, `299-316`). No scope statement deliberately prints a raw key, but exception secrecy remains under execution/downstream review.

Executed predicate output confirms the string-prefix defect: three non-loopback/ambiguous URL forms beginning with `localhost` or `127.0.0.1` were classified as local, so `check_llama_connectivity()` suppresses its Authorization header for them (`src/menhir/core/runtime_preflight.py:94-96`, `157-178`; M4-SEC-08).

## 7. Guard and Identity Analysis — ingest_guard.py, reader_identity.py

`allowed_ingest_roots()` resolves configured roots and falls back to resolved current working directory when none survive (`src/menhir/core/ingest_guard.py:31-50`). The containment comparison uses resolved paths (`src/menhir/core/ingest_guard.py:53-54`). `ensure_ingest_path_allowed()` grants unrestricted access to operator **and empty tier**, then rejects out-of-root paths with an error containing the fully resolved path, tier, and environment-variable name (`src/menhir/core/ingest_guard.py:58-74`). Authenticated HTTP and explicit stdio binding normally prevent an empty tier; explicit insecure no-auth remote mode and future unbound call paths remain fail-open.

Protected path: primary document/project ingestion (`src/menhir/core/backend_runtime_data_ops.py:305-360`).  
Unguarded path: `write_project_structure()` compatibility rescan (`src/menhir/core/backend_runtime_data_ops.py:428-481`; M4-SEC-06).

`normalize_reader_id()` maps `None`, empty, and whitespace-only identifiers to shared literal `default` (`src/menhir/core/reader_identity.py:4-8`). Bootstrap receipt state is process-global and keyed by normalized reader plus workspace selection (`src/menhir/core/runtime_support.py:141-167`). REST's bootstrap request defaults `reader_id` to `default`; whether this allows one caller to satisfy another's receipt gate requires endpoint-level execution and remains DRAFT.

## 8. Injection and Traversal Register

| Caller-controlled input | Sink | Control | Result |
|---|---|---|---|
| ingest `path` | `read_text_utf8()` / `ProjectScanner.scan()` | Request-tier root guard on primary methods | Guard itself resolves traversal/symlinks, but empty tier fails open (`src/menhir/core/ingest_guard.py:53-74`). |
| `scan.root_path` with omitted `symbols` | background `ProjectScanner.scan()` | `isdir` only | **M4-SEC-06 confirmed** (`src/menhir/core/backend_runtime_data_ops.py:428-481`). |
| `query_type`, arbitrary `params` | graph-adapter structure method via `**params` | only two special cases normalized | Downstream Cypher/method allowlist inspection pending (`src/menhir/core/backend_runtime_data_ops.py:483-513`). |
| `repo_path`, repository, commit, old/new path | artifact corpus audit/relocation adapters | no core confinement | Downstream filesystem/subprocess/Cypher inspection pending (`src/menhir/core/backend_runtime_admin_ops.py:417-470`). |
| generic backend `operation` | `getattr(RuntimeProvider, operation)` | exact `_BACKEND_METHODS` allowlist | No arbitrary method traversal; operation names are allowlisted (`src/menhir/api/routes_handlers.py:213-225`). |
| generic backend body keys | Python keyword dispatch | arbitrary dict; implementation signature rejects unknown keys | No direct code injection, but every mismatch is logged with full body (M4-SEC-04). |
| provider base URL | `urlopen(base_url + /models)` | raw-prefix local-auth predicate | **Low M4-SEC-08 confirmed by execution** (`src/menhir/core/runtime_preflight.py:94-96`, `157-178`). |

## 9. Information Disclosure Register

| Surface | Reachability / tier | Disclosure | Status |
|---|---|---|---|
| generic backend exception log | any tier able to invoke its operation | full request body plus traceback | **High M4-SEC-04** (`src/menhir/api/routes_handlers.py:213-231`). |
| `get_provider_config` | readonly generic REST remainder | Neo4j URI/database, provider endpoints/models/backend URL | **Medium candidate** (`src/menhir/core/backend_runtime_admin_ops.py:296-319`; `src/menhir/api/routes_support.py:654-661`). |
| ingest guard rejection | agent path attempt | resolved host path, tier, control env var | MCP error rendering may expose it; REST generic 500 is sanitized. Final trace pending (`src/menhir/core/ingest_guard.py:71-74`). |
| ingest result | authorized agent | up to 4,000 characters of selected file content and absolute structure path | Intended only if containment holds; amplified by M4-SEC-06 (`src/menhir/core/backend_runtime_data_ops.py:319-339`). |
| background warnings | later response/tool result for same scope key | raw exception string truncated to 300 characters, no redaction | Cross-session isolation and exact transport rendering pending (`src/menhir/core/backend_shared.py:25-47`; `src/menhir/mcp/contracts.py:347-355`). |
| operator diagnostics | local CLI located; remote route/tool not located | auth/bind/OAuth/MCP posture, no raw keys | Remote exposure currently disproved pending controlled final sweep (`src/menhir/operator_diagnostics.py:42-297`). |
| Explorer privacy layer | authenticated/loopback Explorer | nested/list/numeric/case-variant protected values and several log syntaxes remain visible | **Medium M4-SEC-07 confirmed by execution** (`src/menhir/privacy.py:49-82`, `103-162`). |

## 10. Bug-Class Sweep Results — command and output, or NOT RUN

The strengthened probe's synthetic controls were executed before relying on it. They cover all six required classes plus the two adversarial security predicates (`.agent/audit/m4_security_probe.py:779-829`).

**Control command:**

```text
python /mnt/data/m4_security_probe.py --self-test-only
```

**Control output summary:**

```text
passed=True
cancelled_error_candidate=True
duplicate_body_difference=True
except_only_unbound=True
host_prefix_bypass=True
keyword_mismatch=True
line_counter=True
nested_redaction_leak=True
timestamp_comparison=True
unread_constant=True
```

The complete control-output SHA-256 is `3cf647a3772a66c1cddb2e971100b1a34650c7f71994b047b2f8b8131d36b18e`.

Repository-wide execution against the pinned checkout is **NOT RUN at this checkpoint**. Reason: the audit runtime has authenticated GitHub file access but cannot materialize a complete checkout/archive into the execution container; unauthenticated container networking is unavailable. Static results from source reading are therefore not mislabeled as executed sweep counts. The next checkpoint will reconstruct the exact 23-file scope through blob retrieval if possible.

1. **Duplicate definitions — NOT RUN:** no executed body-comparison result over the pinned scope yet. Manual reading found no same-scope duplicate; cross-file backend-family dispatch still requires the probe.
2. **Except-only unbound names / pyflakes — NOT RUN:** `pyflakes` has not been executed against a complete pinned checkout. Manual reading found no confirmed scope instance.
3. **`CancelledError` — NOT RUN:** `_get_services()` shields the initialization task, catches only `Exception`, and performs cleanup after the await; caller cancellation skips cleanup while the shielded task continues (`src/menhir/core/runtime.py:552-572`). Background scan tasks also catch only `Exception` (`src/menhir/core/backend_runtime_data_ops.py:389-419`, `464-481`). These are static candidates, not executed consequences.
4. **Lexicographic timestamps — NOT RUN:** no executed scope scan yet; manual reading found no confirmed instance.
5. **Unread invariant constants — NOT RUN:** no executed scope scan yet; manual reading found no confirmed instance.
6. **Keyword mismatch — NOT RUN:** no executed cross-file target scan yet; manual reading found no confirmed instance.

## 11. Disproved Candidates, with the evidence that disproved them

- **Primary ingest methods lack confinement — disproved:** both call `ensure_ingest_path_allowed()` before their filesystem sinks (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). The compatibility rescan remains a separate bypass.
- **Default remote empty-tier auth bypass — disproved:** settings reject unauthenticated nonloopback binding unless the explicit insecure override is enabled, and stdio explicitly binds operator. The empty-tier branches remain fail-open defense-in-depth defects, not a default LAN exploit.
- **Bootstrap writes credential files — disproved:** no file/credential write occurs in `bootstrap.py` (`src/menhir/core/bootstrap.py:1-316`).
- **Operator diagnostics returns raw keys — disproved:** it returns presence booleans, not values (`src/menhir/operator_diagnostics.py:50-57`, `278-286`).
- **Arbitrary backend method traversal — disproved:** generic dispatch rejects operations outside `_BACKEND_METHODS` before `getattr()` (`src/menhir/api/routes_handlers.py:213-224`).

## 12. Open Questions

- **OPEN — downstream injection:** inspect graph-adapter implementations reached by `query_structure`, artifact audit, and relocation for dynamic Cypher, subprocesses, and path traversal.
- **OPEN — warning isolation:** prove whether payload-controlled session IDs can cause one caller to receive another caller's background warning.
- **OPEN — reader receipt isolation:** execute two-client `reader_id=default` bootstrap flows and determine whether receipt state is shared across principals.
- **OPEN — diagnostics MCP reachability:** complete controlled enumeration of registered resources/tools for `get_provider_config` and `build_operator_diagnostics`.
- **OPEN — exception secrecy:** determine whether provider-library exception strings can include Authorization headers or API keys before bootstrap logs them.
- **OPEN — size controls:** identify deployment-level request-body limits; absent such a limit, grade unbounded body/log amplification.
- **OPEN — non-security:** `BackendClient.aclose()` may leave its owned client unclosed on cancellation (`src/menhir/core/backend_client.py:60-66`).
- **OPEN — non-security:** stdio bootstrap catches initialization failure and permits lifespan entry (`src/menhir/core/runtime.py:574-610`).

## 13. Coverage Table — all 23 files, measured line reconciliation against 5,097

| # | Scope file | Declared lines | Measured lines | Status | Evidence / resume note |
|---:|---|---:|---:|---|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | 703 | READ | Full read in three bounded ranges; EOF checked at 700-703. |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | 683 | READ | Full read in three bounded ranges; EOF checked at 681-683. |
| 3 | `src/menhir/core/runtime.py` | 646 | 646 | READ | Full read in three bounded ranges; EOF checked at 640-646. |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | 603 | READ | Full read in three bounded ranges; EOF checked at 600-603. |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | 513 | READ | Full read in two bounded ranges; EOF checked at 510-513. |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | 456 | READ | Full read in two bounded ranges; EOF checked at 450-456. |
| 7 | `src/menhir/core/bootstrap.py` | 316 | 316 | READ | Full read in two bounded ranges; EOF checked at 313-316. |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | 297 | READ | Full read in two bounded ranges. |
| 9 | `src/menhir/core/runtime_support.py` | 167 | 167 | READ | Full read. |
| 10 | `src/menhir/privacy.py` | 162 | 162 | READ | Full read; EOF checked at 155-162. |
| 11 | `src/menhir/core/backend_shared.py` | 129 | 129 | READ | Full read; EOF checked at 126-129. |
| 12 | `src/menhir/core/backend_client.py` | 102 | 102 | READ | Full read; EOF checked at 99-102. |
| 13 | `src/menhir/core/request_context.py` | 74 | 74 | READ | Full read; EOF checked at 71-74. |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | 74 | READ | Full read; EOF checked at 71-74. |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | 41 | READ | Full read; EOF checked at 38-41. |
| 16 | `src/menhir/core/backend_impl.py` | 30 | 30 | READ | Full read; EOF checked at 27-30. |
| 17 | `src/menhir/core/__init__.py` | 27 | 27 | READ | Full read; EOF checked at 24-27. |
| 18 | `src/menhir/core/backend_config.py` | 18 | 18 | READ | Full read; EOF checked at 15-18. |
| 19 | `src/menhir/__init__.py` | 16 | 16 | READ | Full read. |
| 20 | `src/menhir/main.py` | 14 | 14 | READ | Full read. |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | 12 | READ | Full read; EOF checked at 9-12. |
| 22 | `src/menhir/core/reader_identity.py` | 11 | 11 | READ | Full read; EOF checked at 8-11. |
| 23 | `src/menhir/__main__.py` | 3 | 3 | READ | Full read. |
|  | **Totals** | **5,097** | **5,097** | **23/23 READ** | Exact reconciliation; no scope file remains unread. |

## 14. What Was Checked, and what could not be verified in this environment

Checked and committed: every line of all 23 scope files; exact 5,097-line reconciliation; REST auth middleware, generic backend dispatcher, named `/api/memory` path, MCP base contract, session/pin/allowlist plumbing, stdio trust binding, server mounting, Explorer mutation routes, and representative MCP ingest tools. GitHub code search failed its control test (it returned no match for a visibly defined symbol), so absence conclusions rely on direct file/tree enumeration rather than the broken index.

Executed and committed: the strengthened probe's synthetic self-test; exact selected-AST adversarial execution for `privacy.py` and `_should_bypass_local_auth()`; literal outputs and AST hashes in Section 4. The exact pinned source fragments were executed without importing Menhir, avoiding dependency/environment substitution.

Not yet completed: downstream graph/filesystem/subprocess sink tracing, controlled diagnostics/resource enumeration, generic failure-log execution, pyflakes, and all six scans over a complete pinned scope snapshot. The container cannot clone/download the repository directly, and the GitHub connector did not materialize a tar/zip archive; these remain `NOT RUN` until exact blobs are reconstructed or a clean checkout becomes available.

## 15. Review Confidence (/100). If any scope went unread, cap it well below 80.

**Current confidence: 76/100.** Scope coverage, transport call chains, and the two adversarial predicates are now executed or directly traced. Confidence remains limited by downstream sink verification, logging reproduction, diagnostics enumeration, and six unexecuted repository-wide sweeps.
