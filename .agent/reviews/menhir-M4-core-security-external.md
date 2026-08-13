# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root  
**Measured scope:** 5,097 lines (reconciles exactly with the declared total)  
**Status:** DRAFT — all scope files read; transport tracing and executed sweeps remain

> Resume rule: Section 13 is the source-of-truth checkpoint. All 23 scope rows are now `READ`; resume with the first unresolved item in Sections 2, 4, 5, 8, 9, or 10 rather than rereading scope.

## 1. Executive Summary, highest-risk result first

### DRAFT M4-SEC-01 — guarded ingest has an unguarded compatibility rescan path (severity pending transport reachability)

`ingest_document()` and `scan_and_write_project()` resolve the request tier and call `ensure_ingest_path_allowed()` before reading or scanning (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). However, the separately exposed `write_project_structure()` accepts a caller-supplied scan dictionary; when the `symbols` key is absent, it takes caller-controlled `root_path` and schedules `_background_symbol_rescan()` (`src/menhir/core/backend_runtime_data_ops.py:428-443`). The rescan checks only `os.path.isdir(root)` and invokes `ProjectScanner.scan(root, name)` without the ingest guard (`src/menhir/core/backend_runtime_data_ops.py:453-481`). If an agent-tier remote caller can select `write_project_structure`, this restores arbitrary host-directory scanning despite the intended containment control. Transport/internal-route reachability is not yet traced, so this remains DRAFT.

### DRAFT M4-SEC-02 — an unbound tier disables path containment (severity pending transport binding proof)

The request-tier context defaults to the empty string (`src/menhir/core/request_context.py:14-20`, `71-74`). The guard grants unrestricted access when tier is empty or `operator` (`src/menhir/core/ingest_guard.py:58-70`). Thus, any ingest chain that reaches core without first binding a tier fails open. The two primary ingest methods consult the context but do not verify that a transport actually bound it (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). Both transport bind/reset paths remain to be traced.

### DRAFT M4-SEC-03 — redacted fields leak nested/non-string values and case variants (execution pending)

`redact_mapping()` applies `redact_text()` only to exact, case-sensitive field names (`src/menhir/privacy.py:62-82`). `redact_text()` masks only non-empty strings; dicts, lists, numeric values, `None`, and empty strings pass through (`src/menhir/privacy.py:49-59`). This contradicts the function documentation that nested dict/list values under a redacted key are “masked wholesale” (`src/menhir/privacy.py:68-73`). Therefore values such as `{"content": {"secret": "..."}}`, `{"content": ["..."]}`, and `{"Content": "..."}` appear to fail open. Required adversarial execution remains pending.

### DRAFT M4-SEC-04 — local-auth bypass accepts attacker-controlled hostnames beginning with `localhost` or `127.0.0.1` (execution pending)

`_should_bypass_local_auth()` uses raw string prefix checks rather than parsing and validating the hostname (`src/menhir/core/runtime_preflight.py:94-96`). `check_llama_connectivity()` omits the Authorization header whenever that predicate returns true (`src/menhir/core/runtime_preflight.py:157-166`). URLs such as `http://localhost.evil.example` and `http://127.0.0.1.evil.example` therefore appear to be treated as trusted local endpoints. The cheap proving execution is pending.

### Architectural result — authorization is absent from the shared backend contract

`MemoryBackend` carries no authenticated principal, tier, or namespace-ownership proof in any method signature; it accepts caller-selected user/session/namespace/path values throughout (`src/menhir/core/backend_protocol.py:43-683`). The concrete `RuntimeProvider` composes data and admin mixins into one object and stores only process/caller session objects, not an authorization policy (`src/menhir/core/backend_runtime_ops.py:5-12`; `src/menhir/core/backend_runtime.py:14-41`). Apart from path containment on two ingest methods, every authorization conclusion depends on transport-side gates.

## 2. Trust Boundary Register — every caller assumption, whether each transport enforces it, with the call chain

| Assuming core surface | Assumption made by core | REST enforcement | MCP enforcement | Evidence / current trace state |
|---|---|---|---|---|
| `queue_episode` | `user_id`, `session_id`, source, namespace, evidence UUID, and payload size are already authorized and valid. | UNTRACED | UNTRACED | Creates a new session directly from caller strings and queues content without binding it to request identity (`src/menhir/core/backend_runtime_data_ops.py:24-52`). |
| Memory/todo/artifact/candidate mutations | Caller tier is sufficient for the selected operation and object; namespace ownership was checked elsewhere. | UNTRACED | UNTRACED | Runtime methods call services/adapters directly; no tier/identity lookup (`src/menhir/core/backend_runtime_data_ops.py:54-143`; `src/menhir/core/backend_runtime_admin_ops.py:14-603`). |
| `delete_namespace` | Caller may select namespace, `max_nodes`, and `force`; namespace ownership is not required or was checked already. | UNTRACED | UNTRACED | Protects only default/shared namespace and node-count blast radius; no tier or owner check (`src/menhir/core/backend_runtime_data_ops.py:82-139`). |
| `ingest_document` / `scan_and_write_project` | Transport bound an accurate request tier; supplied identity/project name are legitimate; input size is acceptable. | UNTRACED | UNTRACED | Tier comes only from a context variable; `session_id` and `user_id` remain payload-controlled (`src/menhir/core/backend_runtime_data_ops.py:305-339`, `342-426`). |
| `write_project_structure` | Serialized scan is trusted, including root path, files/symbols/edges, and attribution; omission of `symbols` legitimately denotes an older client. | UNTRACED | UNTRACED | Reconstructs caller payload and may rescan its `root_path` without the guard (`src/menhir/core/backend_runtime_data_ops.py:428-481`). |
| `query_structure` | `query_type` and arbitrary params are shape-valid, bounded, and safe for the graph adapter. | UNTRACED | UNTRACED | Unknown query types forward `**(params or {})` directly (`src/menhir/core/backend_runtime_data_ops.py:483-513`). |
| `BackendClient._default_headers` | Environment-backed settings contain the correct backend credential and identity metadata; an empty credential may be omitted. | Internal route UNTRACED | Internal route UNTRACED | Optional bearer plus environment-derived `x-menhir-*` identity headers (`src/menhir/core/backend_client.py:46-64`). |
| `RuntimeProvider._effective_session_id` | A supplied `caller_session` is trusted and belongs to the authenticated caller. | UNTRACED | UNTRACED | Caller session overrides process session (`src/menhir/core/backend_runtime.py:39-41`). |
| `normalize_reader_id` | Collapsing `None`, empty, and whitespace identities into shared literal `default` cannot merge unrelated readers. | UNTRACED | UNTRACED | Normalization has no authenticated provenance (`src/menhir/core/reader_identity.py:4-8`). The resulting key indexes process-global bootstrap receipts (`src/menhir/core/runtime_support.py:141-167`). |
| Background-error forwarding | `session_id` is authenticated/collision-resistant and exception text is safe to expose. | UNTRACED | UNTRACED | Message text retained up to 300 characters and bucketed solely by the supplied scope key (`src/menhir/core/backend_shared.py:25-47`). Producers use payload/session IDs (`src/menhir/core/backend_runtime_data_ops.py:409-416`, `477-481`). |
| Redaction callers | Input rows use lowercase canonical keys and redacted-field values are strings. | UNTRACED | UNTRACED | Exact key match plus string-only masking (`src/menhir/privacy.py:49-82`). Call sites remain to be enumerated. |
| Startup/provider configuration | Missing provider capability can safely degrade; exception messages do not contain credentials. | N/A | N/A | Runtime blocks only venv/Neo4j failures and records all others as degraded (`src/menhir/core/runtime.py:442-469`). Bootstrap logs adapter-construction exceptions verbatim (`src/menhir/core/bootstrap.py:181-193`). |

**Partial remote call chain:** operation method → `BackendClient._request()` → `POST /api/internal/backend/{operation}` with optional bearer and environment-derived identity headers (`src/menhir/core/backend_client.py:68-78`).  
**Partial in-process call chain:** transport constructs `RuntimeProvider` → aggregate mixin exposes both data and admin methods (`src/menhir/core/backend_runtime.py:14-29`; `src/menhir/core/backend_runtime_ops.py:5-12`). Transport construction and gate sites remain supporting-context work.

## 3. Authorization Surface — privileged actions and what gates them

No authorization decision occurs inside the 23 scope files except path policy in `ensure_ingest_path_allowed()`. `request_context.py` stores tier/auth mode, but runtime code reads tier only for `ingest_document()` and `scan_and_write_project()`. No core function compares tier for memory, namespace, conflict, scheduler, telemetry, artifact, todo, temporal, candidate, or diagnostic/configuration operations.

Privileged functions confirmed to trust transport authorization:

- **Memory and namespace mutation:** `queue_episode`, `flag_memory`, `unflag_memory`, `promote_memory`, `delete_memory`, `delete_namespace`, `enqueue_pending_episode` (`src/menhir/core/backend_runtime_data_ops.py:24-143`).
- **Filesystem/structure mutation:** `ingest_document`, `scan_and_write_project`, `write_project_structure`, `_background_symbol_rescan` (`src/menhir/core/backend_runtime_data_ops.py:305-481`).
- **Conflict and enrichment control:** `resolve_conflict_group`, `requeue_conflicts_for_llm_review`, `scan_for_conflicts`, `confirm_pending_conflicts`, `force_reset_failed_episode`, `force_release_episode_lease`, `recover_stale_enrichment_leases`, `recover_orphans` (`src/menhir/core/backend_runtime_admin_ops.py:25-161`).
- **Scheduler control:** `scheduler_force_takeover`, `scheduler_pause`, `scheduler_resume` (`src/menhir/core/backend_runtime_admin_ops.py:174-207`).
- **Telemetry write:** `record_conflict_resolution` accepts caller-selected `reviewed_by` (`src/menhir/core/backend_runtime_admin_ops.py:257-276`).
- **Configuration/telemetry reads:** `fetch_operation_stats`, failure/lifecycle/task-event reads, memory overview, circuit-breaker/cache state, and `get_provider_config` (`src/menhir/core/backend_runtime_admin_ops.py:209-319`).
- **Todo/artifact mutation:** `create_todo`, `link_artifacts`, `supersede_artifact`, `transition_artifact_status`, `relocate_artifact_source`, `close_todo`, `delete_todo`, `close_stale_todos` (`src/menhir/core/backend_runtime_admin_ops.py:321-486`).
- **Temporal/candidate mutation:** `create_temporal`, `complete_temporal`, `create_candidate`, `promote_candidate`, `reject_candidate`, `approve_candidate` (`src/menhir/core/backend_runtime_admin_ops.py:488-603`).

The internal HTTP client’s only credential behavior is bearer construction. `resolve_backend_auth_key()` prefers `agent_key` over legacy `api_key` and returns an empty string when neither exists (`src/menhir/core/backend_config.py:8-18`); `_default_headers()` then omits `Authorization` (`src/menhir/core/backend_client.py:46-51`). Server-side behavior remains to be traced.

## 4. Redaction Verification — executed adversarial inputs and real output

**NOT RUN yet.** Source behavior is fully read and the adversarial matrix is prepared:

- quoted strings and apostrophes;
- nested dicts and lists beneath redacted fields;
- non-string scalar values;
- empty string and `None`;
- oversized strings;
- keys differing only in case;
- quoted/unquoted log lines, malformed quotes, and contractions.

Static conclusion pending execution: `redact_mapping()` is shallow and case-sensitive, while `redact_text()` masks only non-empty strings (`src/menhir/privacy.py:49-82`). `redact_log_line()` is explicitly heuristic, masks only qualifying quoted substrings, and provides no hard guarantee for arbitrary log text (`src/menhir/privacy.py:103-162`).

## 5. Diagnostics Exposure — operator_diagnostics.py reachability by tier

`build_operator_diagnostics()` exposes:

- bind host, port, and loopback classification (`src/menhir/operator_diagnostics.py:42-49`, `269-277`);
- effective auth mode, presence/absence of agent/readonly/operator keys, query-auth status, and insecure override state (`src/menhir/operator_diagnostics.py:50-72`, `278-286`);
- OAuth resource-server preflight and embedded authorization-server posture, including consent-secret and trusted-proxy status (`src/menhir/operator_diagnostics.py:203-264`, `294-296`);
- MCP backend diagnostics (`src/menhir/operator_diagnostics.py:267`, `295`);
- safety warnings and all diagnostic checks (`src/menhir/operator_diagnostics.py:287-297`).

It does not directly return raw bearer keys. However, the separate `RuntimeProvider.get_provider_config()` exposes Neo4j URI/database, local LLM and embed endpoints, backend URL, providers, and model names (`src/menhir/core/backend_runtime_admin_ops.py:296-319`). Neither function has a core tier check. Reachability and required tiers from REST and MCP remain untraced.

## 6. Startup and Credential Handling — preflight fail-open/closed, bootstrap file modes and logging

### Preflight decision

Runtime preflight checks interpreter, Graphiti importability, Neo4j connectivity/schema dimensions, provider compatibility, credentials/models, and provider connectivity (`src/menhir/core/runtime_preflight.py:98-456`). `_initialize_services()` fails closed only when the expected interpreter or Neo4j is unavailable. Every other preflight failure is recorded as `degraded` and startup continues (`src/menhir/core/runtime.py:442-469`). This includes a missing Graphiti dependency, incompatible providers, absent provider key/model, and LLM/embed/reranker connectivity failures. Whether degraded service exposure is security-relevant depends on transport readiness routing; no security/auth configuration is validated by core preflight.

The stdio startup wrapper catches general initialization failure, logs/records the exception string, and does not re-raise it (`src/menhir/core/runtime.py:574-610`), so MCP lifespan may enter service despite failed bootstrap. This is currently an availability/correctness question unless a transport subsequently serves an unexpectedly unauthenticated fallback.

### Credential establishment and files

`bootstrap.py` does not write credentials or files. It passes Neo4j credentials into `Neo4jRepository`, and passes all provider configuration into client factories (`src/menhir/core/bootstrap.py:160-193`). Therefore there is no bootstrap-created credential file mode to report. Credential persistence, if any, must occur outside this scope.

### Secret/error logging

Client/adapter-construction exceptions are logged verbatim and embedded in degraded sentinel `reason` strings (`src/menhir/core/bootstrap.py:181-193`). Edge-count synchronization likewise logs and returns `str(exc)` in `edge_count_sync_error` (`src/menhir/core/bootstrap.py:299-316`). Preflight failure records include provider/base URLs, scheduler URL, and model names (`src/menhir/core/runtime_preflight.py:380-445`), while Neo4j failure annotation adds the configured Neo4j URI (`src/menhir/core/runtime_support.py:128-139`). No raw password/key is deliberately logged in scope, but downstream exception content must be probed/traced before concluding secrets cannot reach logs or remote responses.

### Local endpoint auth bypass candidate

`_should_bypass_local_auth()` trusts string prefixes rather than parsed loopback hosts (`src/menhir/core/runtime_preflight.py:94-96`), and `check_llama_connectivity()` omits bearer auth when it returns true (`src/menhir/core/runtime_preflight.py:157-166`). Execution pending.

## 7. Guard and Identity Analysis — ingest_guard.py, reader_identity.py

### Path guard

`allowed_ingest_roots()` reads `MENHIR_INGEST_ALLOWED_ROOTS`, resolves each non-empty entry, silently skips entries raising `OSError`, and falls back to resolved `cwd` when no roots survive (`src/menhir/core/ingest_guard.py:31-50`). `_is_within()` uses path equality/ancestor containment on resolved paths (`src/menhir/core/ingest_guard.py:53-54`). `ensure_ingest_path_allowed()` resolves symlinks before checking but grants unrestricted access to operator and empty/unbound tier (`src/menhir/core/ingest_guard.py:58-70`). Rejection includes the fully resolved attempted host path, tier, and controlling environment-variable name (`src/menhir/core/ingest_guard.py:71-74`).

**Protected paths:** `ingest_document()` and `scan_and_write_project()` call the guard before reading/scanning (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`).  
**Path reaching scan without the guard:** `write_project_structure()` → `_background_symbol_rescan()` (`src/menhir/core/backend_runtime_data_ops.py:428-481`).

### Reader identity

`normalize_reader_id()` strips caller input and maps `None`, empty, and whitespace-only values to shared identifier `default` (`src/menhir/core/reader_identity.py:4-8`). `_bootstrap_receipt_key()` combines that normalized ID with workspace selection, and process-global state stores last-seen bootstrap versions by this key (`src/menhir/core/runtime_support.py:141-167`). A caller who can omit or blank reader identity shares receipt state with every other default reader. Call-site and consequence tracing remain pending.

## 8. Injection and Traversal Register

| Input | Sink | Validation/confinement | Status |
|---|---|---|---|
| `path` to `ingest_document` | `read_text_utf8()` then graph write and narrative return | Guarded by request-context tier (`src/menhir/core/backend_runtime_data_ops.py:305-339`) | DRAFT M4-SEC-02 if tier can be unbound. |
| `path` to `scan_and_write_project` | `ProjectScanner.scan()` then graph write | Guarded by request-context tier (`src/menhir/core/backend_runtime_data_ops.py:342-426`) | DRAFT M4-SEC-02 if tier can be unbound. |
| `scan.root_path` with omitted `symbols` | `_background_symbol_rescan()` → `ProjectScanner.scan(root, name)` | Directory existence only; no guard (`src/menhir/core/backend_runtime_data_ops.py:428-481`) | DRAFT M4-SEC-01. |
| `query_type` and `params` | Graph adapter query method with arbitrary keyword expansion | No shape allowlist in default branch (`src/menhir/core/backend_runtime_data_ops.py:483-513`) | DRAFT — inspect adapter and transport contracts. |
| `repo_path`, `old_path`, `new_path`, repository/commit strings | Graph adapter artifact audit/relocation | No core confinement (`src/menhir/core/backend_runtime_admin_ops.py:417-470`) | DRAFT — inspect downstream filesystem/subprocess/Cypher sinks. |
| startup artifact-reconcile repo path | graph-adapter corpus audit and optional reconciliation service apply | Environment/configuration controlled; no core path confinement (`src/menhir/core/runtime.py:50-129`) | Deployment trust boundary, not remote until settings mutation path found. |
| `operation` | URL path `/api/internal/backend/{operation}` | Public mixin methods use literals; `_request` accepts arbitrary string (`src/menhir/core/backend_client.py:68-78`) | DRAFT — determine direct reachability. |
| provider base URL | `urlopen()` `/models` request | No parsed-host validation for auth bypass (`src/menhir/core/runtime_preflight.py:94-96`, `157-178`) | DRAFT M4-SEC-04. |

## 9. Information Disclosure Register

| Surface | Data exposed | Bound / redaction | Status |
|---|---|---|---|
| `get_provider_config` | Neo4j URI/database, local provider endpoints, backend URL, provider/model names | No redaction or core tier check (`src/menhir/core/backend_runtime_admin_ops.py:296-319`) | DRAFT — trace both transports. |
| operator diagnostics | Bind host/port, auth mode, key-presence booleans, override/proxy/consent posture, OAuth/MCP checks | Raw keys omitted; no core tier check (`src/menhir/operator_diagnostics.py:42-297`) | DRAFT — trace both transports. |
| Ingest guard rejection | Fully resolved host path, tier, environment-variable name | No redaction (`src/menhir/core/ingest_guard.py:71-74`) | DRAFT — trace exception payloads. |
| Ingest narrative | Up to 4,000 characters of caller-selected file content plus absolute structure path | Deliberate output, no redaction (`src/menhir/core/backend_runtime_data_ops.py:319-339`) | Expected for authorized ingest; dangerous if guard/tier bypassed. |
| Background warning path | Exception string plus project/name text | 300-character truncation only (`src/menhir/core/backend_shared.py:31-39`; `src/menhir/core/backend_runtime_data_ops.py:409-416`, `477-481`) | DRAFT — trace scope key and rendering. |
| Bootstrap/preflight logs | Exception strings, Neo4j/provider/base URLs, scheduler URL, model names | No privacy redaction in scope (`src/menhir/core/bootstrap.py:181-193`, `299-316`; `src/menhir/core/runtime_preflight.py:380-445`; `src/menhir/core/runtime_support.py:128-139`) | DRAFT — probe exception contents and remote log exposure. |
| HTTP error propagation | Internal backend status/body may enter `httpx.raise_for_status()` exception text | No local redaction (`src/menhir/core/backend_client.py:79-87`) | DRAFT — trace remote error rendering. |
| privacy display layer | Nested/non-string/case-variant content can remain visible | Shallow, case-sensitive, string-only masking (`src/menhir/privacy.py:49-82`) | DRAFT M4-SEC-03; execute. |

## 10. Bug-Class Sweep Results — command and output, or NOT RUN

DRAFT — all six repository sweeps are currently **NOT RUN**. The probe’s synthetic self-test passed before its initial commit, but repository execution and control outputs have not yet been recorded.

Static candidates identified during full reading, not yet sweep results:

1. **Duplicate definitions:** no manual duplicate observed; body-comparison probe still required across the backend family.
2. **Except-only unbound names:** no manual confirmed occurrence; pyflakes still required.
3. **CancelledError:** `_get_services()` shields the initialization task, catches only `Exception`, and performs init-task cleanup after the await (`src/menhir/core/runtime.py:552-572`). Cancellation escapes because `CancelledError` derives from `BaseException`, skipping both cleanup paths while the shielded task continues. Consequence requires execution. Background scan tasks also catch only `Exception` (`src/menhir/core/backend_runtime_data_ops.py:389-419`, `464-481`), but no required security-state reset has yet been identified.
4. **Lexicographic timestamps:** no manual confirmed occurrence; executed/static sweep required.
5. **Unread invariant constants:** `INIT_TIMEOUT` and preflight constants are read; full probe required for all constants.
6. **Keyword mismatch:** no manual confirmed occurrence; cross-file runtime-target probe required.

## 11. Disproved Candidates, with the evidence that disproved them

- **DISPROVED (for the two primary ingest methods only):** the initial client-layer observation suggested `ingest_document` and `scan_and_write_project` had no containment. Their runtime implementations do call `ensure_ingest_path_allowed()` before the filesystem sink (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). This does not disprove the unbound-tier fail-open condition or the separate rescan bypass.
- **DISPROVED — bootstrap writes credential files:** `bootstrap.py` contains collaborator assembly and schema preparation only; it performs no filesystem credential write (`src/menhir/core/bootstrap.py:1-316`). File-mode analysis is therefore not applicable to this file.
- **DISPROVED — raw keys deliberately returned by operator diagnostics:** diagnostics return key-presence booleans, not key values (`src/menhir/operator_diagnostics.py:50-57`, `278-286`). Endpoint/configuration metadata exposure still requires tier tracing.

## 12. Open Questions

- **OPEN — transport/receiver trace:** Does `/api/internal/backend/{operation}` authenticate absent/legacy/agent credentials, bind request tier for every operation, and enforce operation-specific tiers?
- **OPEN — compatibility rescan reachability:** Can either public transport or an agent-authenticated internal caller select `write_project_structure` with `symbols` omitted?
- **OPEN — identity binding:** Are header/payload user and session values tied to the authenticated client, or trusted metadata?
- **OPEN — namespace ownership:** Are namespaces access-control boundaries, and does either transport enforce ownership consistently?
- **OPEN — reader isolation:** Which call sites supply `reader_id`, and can a remote caller intentionally share `default` bootstrap receipt state?
- **OPEN — diagnostics reachability:** Which REST and MCP endpoints expose `build_operator_diagnostics()` and `get_provider_config()`, and at what tier?
- **OPEN — warning isolation:** Is background warning scope keyed by authenticated session or payload-controlled `session_id`?
- **OPEN — downstream sinks:** Inspect graph adapter paths reached by `query_structure`, artifact corpus audit, and relocation for Cypher/subprocess/filesystem injection.
- **OPEN — exception secrecy:** Can provider/client construction exceptions include API keys or authorization headers in this stack?
- **OPEN — non-security:** `BackendClient.aclose()` clears `_client` before awaiting `client.aclose()`; cancellation may leave the owned client unclosed (`src/menhir/core/backend_client.py:60-66`).
- **OPEN — non-security:** stdio runtime bootstrap swallows initialization failure and permits lifespan entry (`src/menhir/core/runtime.py:574-610`).

## 13. Coverage Table — all 23 files, measured line reconciliation against 5,097

| # | Scope file | Declared lines | Measured lines | Status | Evidence / resume note |
|---:|---|---:|---:|---|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | 703 | READ | Full read in three bounded ranges; EOF checked at 700-703. |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | 683 | READ | Full read in three bounded ranges; EOF checked at 681-683. |
| 3 | `src/menhir/core/runtime.py` | 646 | 646 | READ | Full read in three bounded ranges; EOF checked at 640-646. Startup/degradation/cancellation evidence recorded. |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | 603 | READ | Full read in three bounded ranges; EOF checked at 600-603. |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | 513 | READ | Full read in two bounded ranges; EOF checked at 510-513. |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | 456 | READ | Full read in two bounded ranges; EOF checked at 450-456. Local-auth predicate candidate recorded. |
| 7 | `src/menhir/core/bootstrap.py` | 316 | 316 | READ | Full read in two bounded ranges; EOF checked at 313-316. No credential-file writes; exception logging recorded. |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | 297 | READ | Full read in two bounded ranges; output exposure recorded. |
| 9 | `src/menhir/core/runtime_support.py` | 167 | 167 | READ | Full read; default-reader receipt sharing and URI annotation recorded. |
| 10 | `src/menhir/privacy.py` | 162 | 162 | READ | Full read; EOF checked at 155-162. Redaction candidates recorded; execution pending. |
| 11 | `src/menhir/core/backend_shared.py` | 129 | 129 | READ | Full read; EOF checked at 126-129. |
| 12 | `src/menhir/core/backend_client.py` | 102 | 102 | READ | Full read; EOF checked at 99-102. |
| 13 | `src/menhir/core/request_context.py` | 74 | 74 | READ | Full read; EOF checked at 71-74. |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | 74 | READ | Full read; EOF checked at 71-74. |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | 41 | READ | Full read; EOF checked at 38-41. |
| 16 | `src/menhir/core/backend_impl.py` | 30 | 30 | READ | Full read; EOF checked at 27-30. |
| 17 | `src/menhir/core/__init__.py` | 27 | 27 | READ | Full read; EOF checked at 24-27. |
| 18 | `src/menhir/core/backend_config.py` | 18 | 18 | READ | Full read; EOF checked at 15-18. |
| 19 | `src/menhir/__init__.py` | 16 | 16 | READ | Full read; package-version metadata only. |
| 20 | `src/menhir/main.py` | 14 | 14 | READ | Full read; deferred CLI import/dispatch only. |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | 12 | READ | Full read; EOF checked at 9-12. |
| 22 | `src/menhir/core/reader_identity.py` | 11 | 11 | READ | Full read; EOF checked at 8-11. |
| 23 | `src/menhir/__main__.py` | 3 | 3 | READ | Full read; module entry dispatch only. |
|  | **Totals** | **5,097** | **5,097** | **23/23 READ** | Exact reconciliation; no scope file remains unread. |

## 14. What Was Checked, and what could not be verified in this environment

Checked and committed: every line of every scope file; EOF boundaries and exact total reconciliation; shared contract, HTTP client, runtime assembly and lifecycle, data/admin operations, preflight/bootstrap, request context, path guard, reader normalization, diagnostics, redaction implementation, package entry points, and shared warning plumbing.

Supporting transport/auth/settings context, call-site searches, redaction execution, and all six bug-class sweeps remain. Direct unauthenticated network cloning is unavailable in this environment, so source has been read from the pinned commit through the authenticated GitHub connector. Executions will be reported with literal commands/output or as `NOT RUN`, never inferred.

## 15. Review Confidence (/100). If any scope went unread, cap it well below 80.

**Current confidence: 61/100.** Scope coverage is complete and core implementation conclusions are strong. Confidence remains limited by unresolved REST/MCP reachability, missing executed redaction results, downstream sink tracing, and six unexecuted bug-class sweeps.
