# Menhir M3 — MCP Surface Architecture Audit

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Branch:** `audit/m3-mcp-architecture-external`  
**Scope:** 70 Python files / requested 7,222 physical lines under `src/menhir/mcp/`  
**Status:** DRAFT; written continuously. Unread files remain `PENDING`.

## 1. Executive Summary

### Confirmed High candidate: committed `add_memory` writes can be returned as failed calls

The ordinary `add_memory` path awaits `backend.queue_episode(...)`, then calls `_queue_summary(backend)` while building its success response (`src/menhir/mcp/tools/ingest/add_memory.py:108-133`). `_queue_summary` performs three additional backend reads — queue depth, active processing rows, and scheduler status — without a catch around those reads (`src/menhir/mcp/formatters.py:540-565`).

If one of those post-write reads fails, `track_mcp_call` catches the exception and converts it into a normal error payload (`src/menhir/mcp/telemetry/tracker.py:52-112`). The durable write may have succeeded, but the caller cannot distinguish that state from a failed write. A retry can duplicate the memory. This is a response-atomicity and partial-failure architecture defect.

The server itself is thin: create gateway, register tools, register resources (`src/menhir/mcp/server.py:25-45`). Four eager registries contain 10 ingest, 5 recall, 5 conflict, and 34 ops classes: **54 registered tools** (`src/menhir/mcp/tools/__init__.py:7-22`; `tools/ingest/__init__.py:3-14`; `tools/recall/__init__.py:3-9`; `tools/conflict/__init__.py:3-9`; `tools/ops/__init__.py:3-74`).

## 2. Layering Edge Table and Judgements

Mechanical counts remain pending. Confirmed edges:

| From MCP to | Files | Judgement |
|---|---|---|
| `core.backend_protocol` | `contracts.py` | Allowed transport-to-interface edge (`contracts.py:15`). |
| `core.backend_impl` | `contracts.py`, `service_access.py` | Mixed. Provider construction belongs at composition; `contracts.py` draining warnings from the concrete implementation after every tool call leaks through the protocol boundary (`contracts.py:357-363`; `service_access.py:15-17`). |
| private `core.runtime` state/functions | `service_access.py`, `lifecycle.py` | Violation/compatibility debt; transport code binds to `_state` and private lifecycle functions (`service_access.py:251-258,281-286`; `lifecycle.py:17-39`). |
| `infrastructure.telemetry.store` private helpers | `telemetry/tracker.py` | Violation: imports `_preview_of`, `_size_of`, `_utc_now_iso` (`telemetry/tracker.py:10`). |
| `domain` / `services` | `service_access.py`, `formatters.py`, `add_memory.py` | Direction generally allowed; final edge inventory pending. |

## 3. Blast Radius Register

Measured in-degree is `NOT RUN` pending an executable checkout.

- `contracts.py`: all tools share registration, tier/client checks, namespace pinning, timeout/error conversion, telemetry, and warning append (`contracts.py:239-407`). A change affects the complete 54-tool surface.
- `service_access.py`: backend selection, caller context, namespace/tool policy, health probing, stdio trust, and session identity (`service_access.py:1-314`). Coupling is mostly to concrete providers, private runtime state, globals, and environment-derived policy.
- `formatters.py`: transforms plus queue filtering, polling, and backend-dependent diagnostics (`formatters.py:1-616`). Pure rendering is stable; polling/summary helpers are coupled to a broad backend shape.
- `resources.py`: pending full read.

All tool modules are imported eagerly before registration. One import-time failure prevents the complete MCP server from being constructed rather than disabling only that tool (`tools/__init__.py:7-22`). Provisional Medium.

## 4. Outward Coupling Register

The required outside-MCP → private-MCP sweep is pending. No absence claim is made.

Confirmed inverse private imports already found:

- `mcp/telemetry/tracker.py:10` → three private telemetry-store helpers.
- `mcp/service_access.py:251-255,281-284` → `core.runtime._state`.
- `mcp/lifecycle.py:17-39` → `_state` and three private runtime operations.

## 5. Tool Dispatch Architecture

1. `server.py` creates the gateway and calls `register_all_tools` (`server.py:29-44`).
2. `tools/__init__.py` eagerly imports four lists, concatenates them, instantiates each class, and calls `.register(mcp)` (`tools/__init__.py:7-22`).
3. `BaseTool.register` preserves the endpoint signature with `wraps`, renames the handler, and passes it to `mcp.tool()` (`contracts.py:367-379`).
4. The handler calls `BaseTool.execute`, whose runner enforces query-auth rules, tier, per-client allowlist, operator audit, and pinned namespace before the endpoint (`contracts.py:292-356`).
5. The endpoint obtains a protocol-typed backend, resolved to `RuntimeProvider` or `BackendClient` (`contracts.py:247-248`; `service_access.py:243-270`).

**Verdict:** the per-module pattern is a useful endpoint-schema and ownership boundary, not a failure-isolation boundary. Endpoint files are locally understandable, but startup, authorization, timeout, telemetry, and error semantics remain centralized.

## 6. Failure-Mode Trace — `add_memory`

Path: `server.py:29-45` → `tools/__init__.py:7-22` → `ingest/__init__.py:3-14` → `contracts.py:367-379` → `contracts.py:292-366` → `add_memory.py:48-133` → backend → `formatters.py:540-576` → `tracker.py:40-136`.

| Failure | Surface |
|---|---|
| Runtime/backend unavailable | `build_memory_backend` raises; tracker logs and converts to error text (`service_access.py:243-270`; `tracker.py:82-112`). |
| Neo4j/backend exception | `_diagnose_failure` enriches selected errors; tracker converts them to ordinary tool payloads (`tracker.py:17-38,82-112`). |
| Timeout | `asyncio.wait_for`; logged and returned as text/JSON (`tracker.py:52-81`). Synchronous blocking work cannot be interrupted until it yields. |
| Invalid namespace/bootstrap combination | Endpoint returns prose normally; telemetry records successful invocation (`add_memory.py:75-87`). |
| Backend result has `status=failed` | Reduced to `Failed to store memory.`; stage and operation identity are discarded (`add_memory.py:123-124`). |
| Post-write summary read fails | Converted to failure after the write; retry safety is unknowable (`add_memory.py:108-133`; `formatters.py:540-565`). |
| Telemetry SQLite error | `sqlite3.Error` is logged and result continues; other store exception classes can replace the outcome (`tracker.py:70-78,92-103,119-133`). |
| Malformed MCP argument / cancellation | Framework/runtime behavior still open; not asserted. |

## 7. Observability Assessment

Failure logs contain kind, operation, duration, and message; success logs contain kind, operation, duration (`tracker.py:60-69,82-91,113-119`). Persisted telemetry adds timestamps, sizes, success, error, and payload preview (`tracker.py:70-78,92-103,119-133`).

No caller, client/session, request/correlation ID, backend mode, or named stage is passed. One log line identifies the tool, but not who called it or whether failure happened before, during, or after the durable write. Provisional Medium.

Identity/audit telemetry touches also use broad best-effort swallowing (`service_access.py:157-171,300-311`; `contracts.py:132-166`), so failure of observability itself has no durable degraded signal.

## 8. Fan-Out Register

- `_queue_summary`: bounded to 200 active rows but adds three serial backend reads to every ordinary successful write (`formatters.py:540-565`).
- `_collect_episode_status`: disproved as unbounded; deadline, terminal-state checks, and sleep bound it (`formatters.py:333-420`).
- `_session_cache`: no size/expiry bound; process-lifetime growth by caller/session tuple (`service_access.py:28-30,137-154`). Provisional Low.
- Query-auth event deques expire, but the key dictionary has no eviction (`contracts.py:101-127`). Provisional Low.

## 9. `query_structure.py` Responsibilities

PENDING full 692-line read. No god-file judgement yet.

## 10. Bug-Class Sweep Results

Probe committed: `.agent/audit/m3_architecture_probe.py`.

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — direct checkout failed because the execution container could not resolve github.com; no exact checkout is mounted.
```

This applies to import graph/cycles, in-degree, boundary-private imports, duplicate-body comparison, and unread module constants.

Search instrument control:

```text
GitHub.search("register_all_tools", repo="Archolith/menhir")
OUTPUT: []
CONTROL: tools/__init__.py:17-22 defines it; server.py:18,44 imports/calls it.
VERDICT: code-search connector discarded for absence claims.
```

## 11. Disproved Candidates

- Episode polling is bounded (`formatters.py:333-420`).
- Per-tool files are not pure ceremony because each owns a typed endpoint signature preserved for MCP discovery (`contracts.py:367-379`); they simply do not provide fault isolation.

## 12. Open Questions

- One-line pass-by note: broad query-auth fallback and empty-tier behavior are correctness/security questions, not graded here (`contracts.py:42-70,325-329`).
- FastMCP pre-handler validation and cancellation behavior remain unexecuted.

## 13. Coverage Table

| Status | Files | Lines |
|---|---:|---:|
| READ | 14 | 1,927 |
| PENDING | 56 | 5,295 |
| Requested total | 70 | 7,222 |

READ: `contracts.py` 407; `formatters.py` 616; `lifecycle.py` 84; `server.py` 68; `service_access.py` 314; `telemetry/{__init__,tracker}.py` 34+136; `tools/{__init__,base}.py` 22+7; group initializers `conflict` 9, `ingest` 14, `ops` 74, `recall` 9; `tools/ingest/add_memory.py` 133.

All other scope files are `PENDING`; the final report will replace this with 70 individual rows and an independent physical-line reconciliation.

## 14. What Was Checked

Target commit and branch ancestry verified. No withheld functional-correctness report was read. Registration and one complete write path were statically traced. The search connector was control-tested and rejected. Probe syntax was checked, but its Menhir outputs are `NOT RUN`.

## 15. Review Confidence

**32/100 draft confidence.** Fifty-six files remain unread and required mechanical output is not executable in the present environment.
