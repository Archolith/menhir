# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root; declared total 5,097 lines  
**Status:** DRAFT — 14/23 scope files read; transport tracing and runtime/startup review remain

> Resume rule: start at the first `NOT READ` row in Section 13. A row changes to `READ` only in the same commit that records the evidence obtained from that file.

## 1. Executive Summary, highest-risk result first

### DRAFT M4-SEC-01 — guarded ingest has an unguarded compatibility rescan path (severity pending reachability)

`ingest_document()` and `scan_and_write_project()` resolve the request tier and call `ensure_ingest_path_allowed()` before reading or scanning (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). However, the separately exposed `write_project_structure()` accepts a caller-supplied scan dictionary; when the `symbols` key is absent, it takes the caller-controlled `root_path` from that dictionary and schedules `_background_symbol_rescan()` (`src/menhir/core/backend_runtime_data_ops.py:428-443`). The rescan checks only `os.path.isdir(root)` and invokes `ProjectScanner.scan(root, name)` without the ingest guard (`src/menhir/core/backend_runtime_data_ops.py:453-481`). If an agent-tier remote caller can select `write_project_structure`, this restores the arbitrary host-directory scanning capability the guard is intended to prevent. Transport/internal-route reachability is not yet traced, so this remains DRAFT.

### DRAFT M4-SEC-02 — an unbound tier deliberately disables path containment (severity pending transport binding proof)

The request-tier context defaults to the empty string (`src/menhir/core/request_context.py:14-20`, `71-74`). The guard treats both an empty tier and `operator` as unrestricted (`src/menhir/core/ingest_guard.py:58-68`). Thus, any ingest call chain that reaches core without first binding a tier fails open. The two primary ingest methods do consult the context, but neither verifies that a transport bound it (`src/menhir/core/backend_runtime_data_ops.py:317-319`, `354-360`). Both transport bind/reset paths remain to be traced.

### DRAFT architectural result — authorization is absent from the shared backend contract

`MemoryBackend` carries no authenticated principal, tier, or ownership proof in any method signature; it accepts caller-selected user/session/namespace/path values throughout (`src/menhir/core/backend_protocol.py:43-683`). The concrete `RuntimeProvider` composes both data and admin mixins into one object and stores only process/caller session objects, not an authorization policy (`src/menhir/core/backend_runtime_ops.py:5-12`; `src/menhir/core/backend_runtime.py:14-41`). Every authorization conclusion therefore depends on transport-side gates.

## 2. Trust Boundary Register — every caller assumption, whether each transport enforces it, with the call chain

| Assuming core surface | Assumption made by core | REST enforcement | MCP enforcement | Evidence / current trace state |
|---|---|---|---|---|
| `queue_episode` | `user_id`, `session_id`, source, namespace, evidence UUID, and payload size are already authorized and valid. | UNTRACED | UNTRACED | Creates a new session directly from caller strings and queues content without binding it to request identity (`src/menhir/core/backend_runtime_data_ops.py:24-52`). |
| Memory/todo/artifact/candidate mutation methods | Caller tier is sufficient for the selected operation and UUID; namespace ownership was checked elsewhere. | UNTRACED | UNTRACED | Runtime methods call services/adapters directly; no tier/identity lookup (`src/menhir/core/backend_runtime_data_ops.py:54-139`; `src/menhir/core/backend_runtime_admin_ops.py:14-603`). |
| `delete_namespace` | Caller may select namespace, `max_nodes`, and `force`; namespace ownership is not needed or was checked already. | UNTRACED | UNTRACED | Only protects the shared/default namespace and node-count blast radius; no tier or owner check (`src/menhir/core/backend_runtime_data_ops.py:82-139`). |
| `ingest_document` / `scan_and_write_project` | Transport bound an accurate request tier; supplied identity and project name are legitimate; input size is acceptable. | UNTRACED | UNTRACED | Tier comes only from a context variable; `session_id` and `user_id` remain payload-controlled (`src/menhir/core/backend_runtime_data_ops.py:305-339`, `342-426`). |
| `write_project_structure` | Serialized scan is trustworthy, including root path, files/symbols/edges, and attribution; omission of `symbols` legitimately denotes an older client. | UNTRACED | UNTRACED | Reconstructs caller payload and can rescan its `root_path` without the guard (`src/menhir/core/backend_runtime_data_ops.py:428-481`). |
| `query_structure` | `query_type` and arbitrary params are shape-valid, bounded, and safe for the graph adapter. | UNTRACED | UNTRACED | Unknown query types forward `**(params or {})` directly (`src/menhir/core/backend_runtime_data_ops.py:483-513`). |
| `BackendClient._default_headers` | Environment-backed settings contain the correct backend credential and identity metadata; an empty credential may be omitted. | Internal route UNTRACED | Internal route UNTRACED | Optional bearer and environment-derived `x-menhir-*` identity headers (`src/menhir/core/backend_client.py:46-64`). |
| `RuntimeProvider._effective_session_id` | A supplied `caller_session` is trusted and belongs to the authenticated caller. | UNTRACED | UNTRACED | Caller session overrides process session (`src/menhir/core/backend_runtime.py:39-41`). |
| `normalize_reader_id` | Collapsing `None`, empty, and whitespace identities into the shared literal `default` cannot merge unrelated readers. | UNTRACED | UNTRACED | Normalization is caller-input-based and has no provenance (`src/menhir/core/reader_identity.py:4-8`). |
| Background-error forwarding | `session_id` is an authenticated, collision-resistant scope key and exception text is safe to expose. | UNTRACED | UNTRACED | Message text retained up to 300 characters and bucketed solely by supplied scope key (`src/menhir/core/backend_shared.py:25-47`). |

**Partial remote call chain:** operation method → `BackendClient._request()` → `POST /api/internal/backend/{operation}` with optional bearer and environment-derived identity headers (`src/menhir/core/backend_client.py:68-78`).  
**Partial in-process call chain:** transport constructs `RuntimeProvider` → aggregate mixin exposes both data and admin methods (`src/menhir/core/backend_runtime.py:14-29`; `src/menhir/core/backend_runtime_ops.py:5-12`). The transport construction sites are still unread.

## 3. Authorization Surface — privileged actions and what gates them

No authorization decision occurs in any of the 14 scope files read so far. `request_context.py` stores tier/auth mode, but the runtime uses tier only for the two guarded ingest methods. No core function compares tier for memory, namespace, conflict, scheduler, telemetry, artifact, todo, temporal, or candidate operations.

Privileged functions already confirmed to trust transport authorization:

- **Memory and namespace mutation:** `queue_episode`, `flag_memory`, `unflag_memory`, `promote_memory`, `delete_memory`, `delete_namespace`, `enqueue_pending_episode` (`src/menhir/core/backend_runtime_data_ops.py:24-143`).
- **Filesystem/structure mutation:** `ingest_document`, `scan_and_write_project`, `write_project_structure`, and its `_background_symbol_rescan` (`src/menhir/core/backend_runtime_data_ops.py:305-481`).
- **Conflict and enrichment control:** `resolve_conflict_group`, `requeue_conflicts_for_llm_review`, `scan_for_conflicts`, `confirm_pending_conflicts`, `force_reset_failed_episode`, `force_release_episode_lease`, `recover_stale_enrichment_leases`, `recover_orphans` (`src/menhir/core/backend_runtime_admin_ops.py:25-161`).
- **Scheduler control:** `scheduler_force_takeover`, `scheduler_pause`, `scheduler_resume` (`src/menhir/core/backend_runtime_admin_ops.py:174-207`).
- **Telemetry write:** `record_conflict_resolution` accepts caller-selected `reviewed_by` (`src/menhir/core/backend_runtime_admin_ops.py:257-276`).
- **Todo/artifact mutation:** `create_todo`, `link_artifacts`, `supersede_artifact`, `transition_artifact_status`, `relocate_artifact_source`, `close_todo`, `delete_todo`, `close_stale_todos` (`src/menhir/core/backend_runtime_admin_ops.py:321-486`).
- **Temporal/candidate mutation:** `create_temporal`, `complete_temporal`, `create_candidate`, `promote_candidate`, `reject_candidate`, `approve_candidate` (`src/menhir/core/backend_runtime_admin_ops.py:488-603`).

The internal HTTP client’s only credential behavior is bearer construction. `resolve_backend_auth_key()` prefers `agent_key` over legacy `api_key` and returns an empty string when neither exists (`src/menhir/core/backend_config.py:8-18`); `_default_headers()` then omits `Authorization` (`src/menhir/core/backend_client.py:46-51`). Server-side fail-open/fail-closed behavior remains unread.

## 4. Redaction Verification — executed adversarial inputs and real output

DRAFT — `privacy.py` unread and adversarial execution not yet run.

## 5. Diagnostics Exposure — operator_diagnostics.py reachability by tier

DRAFT — `operator_diagnostics.py` unread. A separate runtime metadata operation already returns internal connection/configuration data without a core tier check: Neo4j URI/database, local LLM/embed URLs, backend URL, provider kinds, and model names (`src/menhir/core/backend_runtime_admin_ops.py:296-319`). Transport tier remains untraced.

## 6. Startup and Credential Handling — preflight fail-open/closed, bootstrap file modes and logging

DRAFT — startup files unread. Partial credential fact: the internal client resolves `agent_key` first, then legacy `api_key`, stripping whitespace (`src/menhir/core/backend_config.py:8-18`). It does not reject an absent key locally; it omits the bearer header (`src/menhir/core/backend_client.py:46-51`).

## 7. Guard and Identity Analysis — ingest_guard.py, reader_identity.py

### Path guard

`allowed_ingest_roots()` reads `MENHIR_INGEST_ALLOWED_ROOTS`, resolves each non-empty entry, silently skips entries raising `OSError`, and falls back to resolved `cwd` when no roots survive (`src/menhir/core/ingest_guard.py:31-50`). `_is_within()` uses path equality/ancestor containment on resolved paths (`src/menhir/core/ingest_guard.py:53-54`). `ensure_ingest_path_allowed()` resolves symlinks before checking but grants unrestricted access to both operator and an empty/unbound tier (`src/menhir/core/ingest_guard.py:58-70`). Its rejection message includes the fully resolved attempted host path, tier, and controlling environment-variable name (`src/menhir/core/ingest_guard.py:71-74`).

**Protected paths:** `ingest_document()` and `scan_and_write_project()` call the guard before reading/scanning (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`).  
**Path reaching scan without the guard:** `write_project_structure()` → `_background_symbol_rescan()` (`src/menhir/core/backend_runtime_data_ops.py:428-481`).

### Reader identity

`normalize_reader_id()` strips caller input and maps `None`, empty, and whitespace-only values to the shared identifier `default` (`src/menhir/core/reader_identity.py:4-8`). No signature, authenticated client ID, or request context participates. Call-site impact remains to be traced.

## 8. Injection and Traversal Register

| Input | Sink | Validation/confinement | Status |
|---|---|---|---|
| `path` to `ingest_document` | `read_text_utf8()` then graph write and narrative return | Guarded by request-context tier (`src/menhir/core/backend_runtime_data_ops.py:305-339`) | DRAFT M4-SEC-02 if tier can be unbound. |
| `path` to `scan_and_write_project` | `ProjectScanner.scan()` then graph write | Guarded by request-context tier (`src/menhir/core/backend_runtime_data_ops.py:342-426`) | DRAFT M4-SEC-02 if tier can be unbound. |
| `scan.root_path` with omitted `symbols` | `_background_symbol_rescan()` → `ProjectScanner.scan(root, name)` | Directory existence only; no guard (`src/menhir/core/backend_runtime_data_ops.py:428-481`) | DRAFT M4-SEC-01. |
| `query_type` and `params` | Graph adapter query method with arbitrary keyword expansion | No shape allowlist in default branch (`src/menhir/core/backend_runtime_data_ops.py:483-513`) | DRAFT — inspect adapter and transport contracts. |
| `repo_path`, `old_path`, `new_path`, repository/commit strings | Graph adapter artifact audit/relocation | No core confinement (`src/menhir/core/backend_runtime_admin_ops.py:417-470`) | DRAFT — inspect downstream filesystem/subprocess/Cypher sinks. |
| `operation` | URL path `/api/internal/backend/{operation}` | Public mixin methods use literals; `_request` accepts arbitrary string (`src/menhir/core/backend_client.py:68-78`) | DRAFT — determine direct reachability. |

## 9. Information Disclosure Register

| Surface | Data exposed | Bound / redaction | Status |
|---|---|---|---|
| `get_provider_config` | Neo4j URI/database, local provider endpoints, backend URL, provider and model names | No redaction or core tier check (`src/menhir/core/backend_runtime_admin_ops.py:296-319`) | DRAFT — trace both transports. |
| Ingest guard rejection | Fully resolved host path, tier, environment-variable name | No redaction (`src/menhir/core/ingest_guard.py:71-74`) | DRAFT — trace exception payloads. |
| Ingest narrative | Up to 4,000 characters of caller-selected file content plus absolute structure path | Deliberate output, no redaction (`src/menhir/core/backend_runtime_data_ops.py:319-339`) | Expected for authorized ingest; dangerous if guard/tier bypassed. |
| Background warning path | Exception string plus caller-controlled project/name text | 300-character truncation only (`src/menhir/core/backend_shared.py:31-39`; `src/menhir/core/backend_runtime_data_ops.py:409-416`, `477-481`) | DRAFT — trace scope key and rendering. |
| HTTP error propagation | Internal backend status/body may enter `httpx.raise_for_status()` exception text | No local redaction (`src/menhir/core/backend_client.py:79-87`) | DRAFT — trace remote error rendering. |

## 10. Bug-Class Sweep Results — command and output, or NOT RUN

DRAFT — all six repository sweeps are NOT RUN. The probe’s synthetic self-test passed locally before its initial commit; repository execution awaits completion of a clean reconstructed snapshot.

Static candidates seen during reading, not yet sweep results:

- `scan_and_write_project._do_write()` and `_background_symbol_rescan()` catch `Exception`, so `asyncio.CancelledError` escapes their handlers (`src/menhir/core/backend_runtime_data_ops.py:389-419`, `464-481`). Whether this skips a required reset/cleanup is not yet established.
- No duplicate definition was observed in the 14 files read manually, but this is not an executed duplicate-body sweep.

## 11. Disproved Candidates, with the evidence that disproved them

- **DISPROVED (for the two primary ingest methods only):** the initial client-layer observation suggested `ingest_document` and `scan_and_write_project` had no containment. Their runtime implementations do call `ensure_ingest_path_allowed()` before the filesystem sink (`src/menhir/core/backend_runtime_data_ops.py:305-319`, `342-360`). This does not disprove the unbound-tier fail-open condition or the separate rescan bypass.

## 12. Open Questions

- **OPEN — transport/receiver trace:** Does `/api/internal/backend/{operation}` authenticate absent/legacy/agent credentials, bind request tier for every operation, and enforce operation-specific tiers?
- **OPEN — compatibility rescan reachability:** Can either public transport or an agent-authenticated internal caller select `write_project_structure` with `symbols` omitted?
- **OPEN — identity binding:** Are header/payload user and session values tied to the authenticated client, or trusted metadata?
- **OPEN — namespace ownership:** Are namespaces access-control boundaries, and does either transport enforce ownership consistently?
- **OPEN — reader isolation:** Which call sites pass values into `normalize_reader_id`, and can a remote caller force `default`?
- **OPEN — warning isolation:** Is background warning scope keyed by authenticated session or by payload-controlled `session_id`?
- **OPEN — downstream sinks:** Inspect graph adapter paths reached by `query_structure`, artifact corpus audit, and relocation for Cypher/subprocess/filesystem injection.
- **OPEN — non-security:** `BackendClient.aclose()` clears `_client` before awaiting `client.aclose()`; cancellation may leave the owned client unclosed (`src/menhir/core/backend_client.py:60-66`).

## 13. Coverage Table — all 23 files, measured line reconciliation against 5,097

| # | Scope file | Declared lines | Measured lines | Status | Evidence / resume note |
|---:|---|---:|---:|---|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | 703 | READ | Full read in three bounded ranges; EOF checked at lines 700-703. |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | 683 | READ | Full read in three bounded ranges; EOF checked at lines 681-683. Contract lacks auth context. |
| 3 | `src/menhir/core/runtime.py` | 646 | — | NOT READ | Resume here. |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | 603 | READ | Full read in three bounded ranges; EOF checked at lines 600-603. Privileged/admin/config surface recorded. |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | 513 | READ | Full read in two bounded ranges; EOF checked at lines 510-513. Guard and bypass candidates recorded. |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | — | NOT READ | — |
| 7 | `src/menhir/core/bootstrap.py` | 316 | — | NOT READ | — |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | — | NOT READ | — |
| 9 | `src/menhir/core/runtime_support.py` | 167 | — | NOT READ | — |
| 10 | `src/menhir/privacy.py` | 162 | — | NOT READ | — |
| 11 | `src/menhir/core/backend_shared.py` | 129 | 129 | READ | Full read; EOF checked at lines 126-129. Warning disclosure path recorded. |
| 12 | `src/menhir/core/backend_client.py` | 102 | 102 | READ | Full read; EOF checked at lines 99-102. Header/auth/error behavior recorded. |
| 13 | `src/menhir/core/request_context.py` | 74 | 74 | READ | Full read; EOF checked at lines 71-74. Empty tier default recorded. |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | 74 | READ | Full read; EOF checked at lines 71-74. Fail-open empty-tier branch recorded. |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | 41 | READ | Full read; EOF checked at lines 38-41. No auth policy stored. |
| 16 | `src/menhir/core/backend_impl.py` | 30 | 30 | READ | Full read; EOF checked at lines 27-30. Compatibility re-exports only. |
| 17 | `src/menhir/core/__init__.py` | 27 | 27 | READ | Full read; EOF checked at lines 24-27. Exports only. |
| 18 | `src/menhir/core/backend_config.py` | 18 | 18 | READ | Full read; EOF checked at lines 15-18. Credential precedence recorded. |
| 19 | `src/menhir/__init__.py` | 16 | — | NOT READ | — |
| 20 | `src/menhir/main.py` | 14 | — | NOT READ | — |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | 12 | READ | Full read; EOF checked at lines 9-12. Composes data + admin into one provider. |
| 22 | `src/menhir/core/reader_identity.py` | 11 | 11 | READ | Full read; EOF checked at lines 8-11. Empty identity collapse recorded. |
| 23 | `src/menhir/__main__.py` | 3 | — | NOT READ | — |
|  | **Totals** | **5,097** | **3,020 read / measured** | **14/23 READ** | 2,077 lines remain unread and unmeasured. |

## 14. What Was Checked, and what could not be verified in this environment

Checked and committed: full source reads and independent EOF bounds for 14 scope files; shared contract, HTTP client, runtime mixin assembly, data/admin operations, request context, ingest guard, reader normalization, credential resolver, and shared warning plumbing. Direct unauthenticated network cloning is unavailable, so source is being read from the pinned commit through the authenticated GitHub connector. Repository-wide executions are deferred until the clean snapshot is reconstructed; they will be reported as executed output or `NOT RUN`, never inferred.

## 15. Review Confidence (/100). If any scope went unread, cap it well below 80.

**Current confidence: 42/100.** Fourteen of 23 scope files (3,020/5,097 lines) are fully read. Core implementation conclusions are strong; transport reachability, startup behavior, diagnostics, redaction, and all executed sweeps remain unresolved.
