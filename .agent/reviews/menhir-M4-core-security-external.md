# Menhir M4 — Core Runtime and Backend Security Audit (External)

**Repository:** `Archolith/menhir`  
**Pinned commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m4-core-security-external`  
**Scope:** 23 files under `src/menhir/core/` and `src/menhir/` root; declared total 5,097 lines  
**Status:** DRAFT — 4/23 scope files read; transport and runtime receiver tracing still in progress

> Resume rule: start at the first `NOT READ` row in Section 13. A row changes to `READ` only in the same commit that records the evidence obtained from that file.

## 1. Executive Summary, highest-risk result first

**DRAFT boundary concern — severity pending receiver/transport trace.** The HTTP-backed core client exposes destructive and operator-like operations but carries no tier, authenticated principal, or namespace-ownership proof in the operation payload. It forwards caller-supplied identity, namespace, and filesystem path values verbatim. Examples include caller-supplied `user_id`, `session_id`, `source`, and `namespace` on episode ingestion (`src/menhir/core/backend_client_ops.py:11-41`); memory promotion/deletion and namespace deletion (`src/menhir/core/backend_client_ops.py:42-77`); and document/project paths plus caller-supplied identity (`src/menhir/core/backend_client_ops.py:205-244`). Whether this is exploitable depends on the internal backend route and both public transports, which remain unread.

## 2. Trust Boundary Register — every caller assumption, whether each transport enforces it, with the call chain

| Assuming core surface | Assumption made by the code read so far | REST enforcement | MCP enforcement | Evidence / current trace state |
|---|---|---|---|---|
| `BackendClientOpsMixin.queue_episode` | Caller has supplied an authorized identity, session, source, namespace, and bounded text/diff. | UNTRACED | UNTRACED | Values are serialized without validation or principal binding (`src/menhir/core/backend_client_ops.py:11-41`). |
| `BackendClientOpsMixin.delete_namespace` | Caller is authorized to select the namespace and to request `force`; `max_nodes` is acceptable. | UNTRACED | UNTRACED | Operation and all controls are forwarded verbatim (`src/menhir/core/backend_client_ops.py:58-77`). |
| `BackendClientOpsMixin.ingest_document` / `scan_and_write_project` | Caller is authorized to make the backend read the supplied filesystem path and to attribute the action to supplied `user_id`/`session_id`. | UNTRACED | UNTRACED | No path confinement or identity derivation occurs in this layer (`src/menhir/core/backend_client_ops.py:205-237`). |
| `BackendClient._default_headers` | Environment-backed settings contain the correct backend credential and identity metadata; an empty credential may be sent as no `Authorization` header. | Internal route UNTRACED | Internal route UNTRACED | Bearer auth is conditional, and `x-menhir-user-id`, `x-menhir-client-id`, and `x-menhir-client-name` come from settings (`src/menhir/core/backend_client.py:46-64`). |
| Background-error forwarding | The scope key used by the receiver isolates warnings to the correct caller and warning text is safe to expose. | UNTRACED | UNTRACED | Arbitrary message text is retained up to 300 characters and later drained for an outbound response (`src/menhir/core/backend_shared.py:25-47`). |

**Partial call chain established:** operation method → `BackendClient._request()` → `POST /api/internal/backend/{operation}` with optional bearer and environment-derived identity headers (`src/menhir/core/backend_client.py:68-78`). The internal route and public-transport entry points remain to be traced.

## 3. Authorization Surface — privileged actions and what gates them

No tier or identity check exists in the four files read so far. `BackendClientOpsMixin` forwards fixed operation names; authorization must therefore be supplied by the receiving route or its caller. Privileged categories already observed include:

- Memory mutation: flag, unflag, promote, delete (`src/menhir/core/backend_client_ops.py:42-57`).
- Namespace destruction including `force` (`src/menhir/core/backend_client_ops.py:58-77`).
- Conflict resolution, enrichment resets/releases, scheduler takeover/pause/resume, artifact mutation, todo deletion, and candidate approval/rejection (full receiver and transport trace pending; methods span `src/menhir/core/backend_client_ops.py:261-703`).
- Filesystem-backed ingestion and project scanning (`src/menhir/core/backend_client_ops.py:205-237`).

The only credential behavior observed is client-side bearer construction. `resolve_backend_auth_key()` prefers `agent_key` over the legacy `api_key` and returns an empty string when neither exists (`src/menhir/core/backend_config.py:8-18`); `_default_headers()` then omits `Authorization` entirely (`src/menhir/core/backend_client.py:46-51`). This is not yet classified as fail-open or fail-closed because the server route is unread.

## 4. Redaction Verification — executed adversarial inputs and real output

DRAFT — not executed yet.

## 5. Diagnostics Exposure — operator_diagnostics.py reachability by tier

DRAFT — no diagnostics source read yet.

## 6. Startup and Credential Handling — preflight fail-open/closed, bootstrap file modes and logging

DRAFT — startup files unread. Partial credential fact: the internal client resolves `agent_key` first, then legacy `api_key`, stripping whitespace (`src/menhir/core/backend_config.py:8-18`). It does not reject an absent key locally; it simply omits the bearer header (`src/menhir/core/backend_client.py:46-51`).

## 7. Guard and Identity Analysis — ingest_guard.py, reader_identity.py

DRAFT — files unread.

## 8. Injection and Traversal Register

| Input | Sink reached in code read so far | Local validation/confinement | Status |
|---|---|---|---|
| `path` to `ingest_document` | JSON payload for internal backend operation | None in client layer (`src/menhir/core/backend_client_ops.py:205-224`) | DRAFT — trace receiver filesystem use. |
| `path` to `scan_and_write_project` | JSON payload for internal backend operation | None in client layer (`src/menhir/core/backend_client_ops.py:226-237`) | DRAFT — trace receiver filesystem use. |
| `repo_path`, `old_path`, `new_path` in artifact operations | JSON payload for internal backend operation | None in client layer (`src/menhir/core/backend_client_ops.py:553-608`) | DRAFT — trace receiver filesystem/subprocess use. |
| `operation` | URL path `/api/internal/backend/{operation}` | Public methods use fixed literals; `_request` itself accepts an arbitrary string (`src/menhir/core/backend_client.py:68-78`) | DRAFT — determine whether untrusted callers can reach `_request` directly. |

## 9. Information Disclosure Register

| Surface | Data exposed | Bound / redaction | Status |
|---|---|---|---|
| Background warning header path | Runtime background error message text | Truncated to 300 characters, not redacted (`src/menhir/core/backend_shared.py:31-39`); client later parses and stores it (`src/menhir/core/backend_client.py:88-100`) | DRAFT — trace producer text, scope-key derivation, and public response/tool rendering. |
| HTTP error propagation | Internal backend response status and body may participate in `httpx.raise_for_status()` exception text | No local redaction (`src/menhir/core/backend_client.py:79-87`) | DRAFT — trace whether exception strings reach remote callers. |

## 10. Bug-Class Sweep Results — command and output, or NOT RUN

DRAFT — all six sweeps are NOT RUN against the repository snapshot. The probe’s synthetic self-test passed locally before its initial commit; repository execution awaits completion of a clean reconstructed snapshot.

## 11. Disproved Candidates, with the evidence that disproved them

DRAFT — none yet.

## 12. Open Questions

- **OPEN — transport/receiver trace:** Does `/api/internal/backend/{operation}` authenticate absent/legacy/agent credentials and derive tier independently of the requested operation?
- **OPEN — identity binding:** Are `x-menhir-user-id` and payload `user_id` checked against the authenticated client, or merely trusted metadata?
- **OPEN — warning isolation:** How is the `_push_background_error()` scope key derived, and can one client receive another client’s queued warning?
- **OPEN — non-security:** `BackendClient.aclose()` clears `_client` before awaiting `client.aclose()`; cancellation may leave the owned client unclosed (`src/menhir/core/backend_client.py:60-66`).

## 13. Coverage Table — all 23 files, measured line reconciliation against 5,097

| # | Scope file | Declared lines | Measured lines | Status | Evidence / resume note |
|---:|---|---:|---:|---|---|
| 1 | `src/menhir/core/backend_client_ops.py` | 703 | 703 | READ | Full read in three bounded ranges; EOF independently checked at lines 700-703. DRAFT auth/trust/path observations recorded in §§1-3, 8. |
| 2 | `src/menhir/core/backend_protocol.py` | 683 | — | NOT READ | Resume here. |
| 3 | `src/menhir/core/runtime.py` | 646 | — | NOT READ | — |
| 4 | `src/menhir/core/backend_runtime_admin_ops.py` | 603 | — | NOT READ | — |
| 5 | `src/menhir/core/backend_runtime_data_ops.py` | 513 | — | NOT READ | — |
| 6 | `src/menhir/core/runtime_preflight.py` | 456 | — | NOT READ | — |
| 7 | `src/menhir/core/bootstrap.py` | 316 | — | NOT READ | — |
| 8 | `src/menhir/operator_diagnostics.py` | 297 | — | NOT READ | — |
| 9 | `src/menhir/core/runtime_support.py` | 167 | — | NOT READ | — |
| 10 | `src/menhir/privacy.py` | 162 | — | NOT READ | — |
| 11 | `src/menhir/core/backend_shared.py` | 129 | 129 | READ | Full read; EOF independently checked at lines 126-129. Warning disclosure path recorded in §§2 and 9. |
| 12 | `src/menhir/core/backend_client.py` | 102 | 102 | READ | Full read; EOF independently checked at lines 99-102. Header/auth/error behavior recorded in §§2, 3, 6, 9. |
| 13 | `src/menhir/core/request_context.py` | 74 | — | NOT READ | — |
| 14 | `src/menhir/core/ingest_guard.py` | 74 | — | NOT READ | — |
| 15 | `src/menhir/core/backend_runtime.py` | 41 | — | NOT READ | — |
| 16 | `src/menhir/core/backend_impl.py` | 30 | — | NOT READ | — |
| 17 | `src/menhir/core/__init__.py` | 27 | — | NOT READ | — |
| 18 | `src/menhir/core/backend_config.py` | 18 | 18 | READ | Full read; EOF independently checked at lines 15-18. Credential precedence recorded in §§3 and 6. |
| 19 | `src/menhir/__init__.py` | 16 | — | NOT READ | — |
| 20 | `src/menhir/main.py` | 14 | — | NOT READ | — |
| 21 | `src/menhir/core/backend_runtime_ops.py` | 12 | — | NOT READ | — |
| 22 | `src/menhir/core/reader_identity.py` | 11 | — | NOT READ | — |
| 23 | `src/menhir/__main__.py` | 3 | — | NOT READ | — |
|  | **Totals** | **5,097** | **952 read / measured** | **4/23 READ** | Unread rows remain unmeasured and uncovered. |

## 14. What Was Checked, and what could not be verified in this environment

Checked and committed: full source reads of `backend_client_ops.py`, `backend_client.py`, `backend_shared.py`, and `backend_config.py`; independent EOF checks for each; preliminary trust, authorization, path, credential, and disclosure tracing. Direct unauthenticated network cloning is unavailable in this environment, so source is being read from the pinned commit through the authenticated GitHub connector. Repository-wide executions are deferred until the clean snapshot is reconstructed; they will be reported as executed output or `NOT RUN`, never inferred.

## 15. Review Confidence (/100). If any scope went unread, cap it well below 80.

**Current confidence: 14/100.** Four of 23 scope files (952/5,097 declared lines) are read; all transport enforcement and runtime receiver conclusions remain provisional.
