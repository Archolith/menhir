# Menhir M3 — MCP Surface Architecture Audit

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Branch:** `audit/m3-mcp-architecture-external`  
**Scope:** 70 Python files / requested 7,222 physical lines under `src/menhir/mcp/`  
**Status:** DRAFT; written continuously. Unread files remain `PENDING`.

## 1. Executive Summary

### Confirmed High candidate: committed writes can be returned as failed invocations

The ordinary `add_memory` path awaits `backend.queue_episode(...)`, then calls `_queue_summary(backend)` while building its success response (`src/menhir/mcp/tools/ingest/add_memory.py:108-133`). `_queue_summary` performs three additional backend reads without a catch around those reads (`src/menhir/mcp/formatters.py:540-565`). If one fails, `track_mcp_call` catches it and converts it into a normal error payload (`src/menhir/mcp/telemetry/tracker.py:52-112`). The durable write may have succeeded, but the caller cannot distinguish it from a failed write; retry can duplicate memory.

The pattern recurs: `add_memory_and_track` writes, polls, then performs the same summary calls (`tools/ingest/add_memory_and_track.py:80-104`); `flag_memory` mutates and then fetches the node only to construct confirmation (`tools/ingest/flag_memory.py:36-52`); `resolve_conflict` mutates, writes best-effort suppression rows, then rereads up to 5,000 groups for confirmation (`tools/conflict/resolve_conflict.py:108-146,190-242`). These are response-atomicity and partial-failure architecture defects.

The server is a thin composition root (`mcp/server.py:25-45`). Four eager registries contain 10 ingest, 5 recall, 5 conflict, and 34 ops classes: **54 registered tools** (`mcp/tools/__init__.py:7-22`; group initializers). All use one inherited execution path for registration, authorization, namespace forcing, timeout/error conversion, telemetry, and warning draining (`mcp/contracts.py:239-379`).

## 2. Layering Edge Table and Judgements

Mechanical counts remain pending. Confirmed edges:

| From MCP to | Files already traced | Judgement |
|---|---|---|
| `core.backend_protocol` | `contracts.py` | Allowed transport-to-interface edge (`contracts.py:15`). |
| `core.backend_impl` | `contracts.py`, `service_access.py` | Mixed. Provider construction belongs at composition; `contracts.py` draining concrete implementation warnings after every call leaks through the protocol boundary (`contracts.py:357-363`; `service_access.py:15-17`). |
| private `core.runtime` state/functions | `service_access.py`, `lifecycle.py` | Violation/compatibility debt (`service_access.py:251-258,281-286`; `lifecycle.py:17-39`). |
| `infrastructure.telemetry.store` private helpers | `telemetry/tracker.py` | Violation: imports `_preview_of`, `_size_of`, `_utc_now_iso` (`telemetry/tracker.py:10`). |
| `domain` / `services` | multiple | Direction generally allowed. `ingest_project.py` is a positive example: orchestration is delegated to `services.project_ingest` and MCP only formats its transport-neutral outcome (`tools/ingest/ingest_project.py:7-11,60-117`). |

## 3. Blast Radius Register

Measured direct in-degree is `NOT RUN` pending an executable checkout.

- `contracts.py`: full 54-tool dispatch policy plus resource execution (`contracts.py:178-407`). Its public base classes are stable interfaces, but implementation reaches concrete warning state.
- `service_access.py`: backend selection, request context, namespace/tool policy, probing, stdio trust, session identity (`service_access.py:1-314`). Coupling is mostly concrete/private/global.
- `formatters.py`: pure transforms mixed with polling and backend-dependent queue diagnostics (`formatters.py:1-616`).
- `resources.py`: nine resource classes plus normalizers, host process/socket probes, runtime fingerprints, and registration (`resources.py:1-521`). `DependencyHealthResource` performs a synchronous socket probe from an async endpoint; first metadata access can perform synchronous `git rev-parse` with a two-second timeout (`resources.py:27-51,211-239,273-283`).

All tool modules are imported eagerly before registration. One import-time failure prevents complete MCP construction rather than disabling one tool (`tools/__init__.py:7-22`). Provisional Medium.

## 4. Outward Coupling Register

The required outside-MCP → private-MCP sweep is pending; no absence claim is made.

Confirmed inverse/private dependencies already found:

- `mcp/telemetry/tracker.py:10` imports three private telemetry-store helpers.
- `mcp/service_access.py:251-255,281-284` reads `core.runtime._state`.
- `mcp/lifecycle.py:17-39` aliases/calls private runtime state and operations.
- `tools/recall/read_flagged_memories.py:7-9` and `recall_context_memories.py:7-9` import leading-underscore helpers from `formatters` and `lifecycle`.

## 5. Tool Dispatch Architecture

1. `server.py` creates the gateway and calls `register_all_tools` (`server.py:29-44`).
2. `tools/__init__.py` eagerly imports four lists, concatenates them, instantiates each class, and calls `.register(mcp)` (`tools/__init__.py:7-22`).
3. `BaseTool.register` preserves endpoint signature with `wraps`, renames the handler, and passes it to `mcp.tool()` (`contracts.py:367-379`).
4. The handler calls `BaseTool.execute`; its runner applies query-auth rules, tier, per-client allowlist, operator audit, and pinned namespace before the endpoint (`contracts.py:292-356`).
5. The endpoint obtains a protocol-typed backend, resolved to `RuntimeProvider` or `BackendClient` (`contracts.py:247-248`; `service_access.py:243-270`).

**Verdict:** useful endpoint-schema and ownership boundary, not failure isolation. Endpoint modules are locally understandable, but startup, policy, timeout, telemetry, and error semantics remain centralized.

## 6. Failure-Mode Trace — `add_memory`

Path: `server.py:29-45` → `tools/__init__.py:7-22` → `ingest/__init__.py:3-14` → `contracts.py:367-379` → `contracts.py:292-366` → `add_memory.py:48-133` → backend → `formatters.py:540-576` → `tracker.py:40-136`.

| Failure | Surface |
|---|---|
| Runtime/backend unavailable | `build_memory_backend` raises; tracker logs and converts to text (`service_access.py:243-270`; `tracker.py:82-112`). |
| Neo4j/backend exception | `_diagnose_failure` enriches selected errors; tracker returns ordinary tool payload (`tracker.py:17-38,82-112`). |
| Timeout | `asyncio.wait_for`; logged and returned as text/JSON (`tracker.py:52-81`). Synchronous blocking work cannot be interrupted until it yields. |
| Invalid namespace/bootstrap | Endpoint returns prose normally; telemetry records invocation success (`add_memory.py:75-87`). |
| Backend returns `status=failed` | Reduced to `Failed to store memory.`; stage and operation identity discarded (`add_memory.py:123-124`). |
| Post-write summary fails | Converted to failure after commit; retry safety unknowable (`add_memory.py:108-133`; `formatters.py:540-565`). |
| Telemetry SQLite error | `sqlite3.Error` logged and result continues; other store exception classes can replace outcome (`tracker.py:70-78,92-103,119-133`). |
| Malformed MCP argument / cancellation | Framework/runtime behavior still open; not asserted. |

`ingest_document` provides a useful contrast: structural document write and semantic-episode queue are explicitly separate; the second phase is caught and reported as deferred/error without erasing the first phase (`tools/ingest/ingest_document.py:54-103`). It is non-atomic but observable.

## 7. Observability Assessment

Failure logs contain kind, operation, duration, message; success logs contain kind, operation, duration (`tracker.py:60-69,82-91,113-119`). Persisted telemetry adds timestamps, sizes, success, error, payload preview (`tracker.py:70-78,92-103,119-133`).

No caller, client/session, request/correlation ID, backend mode, or named stage is passed. One line identifies the tool, but not caller or whether failure happened before, during, or after mutation. Provisional Medium.

Identity/audit telemetry touches also broadly swallow failure (`service_access.py:157-171,300-311`; `contracts.py:132-166`). `recall_context_memories` silently drops its stale-todo diagnostic failure (`tools/recall/recall_context_memories.py:145-153`).

## 8. Fan-Out Register

| Location | Scaling behavior | Assessment |
|---|---|---|
| `recall_context_memories.py:100-137` | Recall followed by serial `fetch_memory_by_uuid` per result, then recent fetch at `recent_limit * 3`; caller limits are not clamped here. | Confirmed N+1, backend-client latency proportional to requested results. Medium. |
| `resolve_conflict.py:78-146,190-242` | Reads up to 5,000 groups to find one, mutates, rereads 5,000. `keep_both` writes every pair via `combinations`; other actions write per removed node. | Large fixed scans plus quadratic sequential pair fan-out. Medium/High architecture risk. |
| `query_structure.py:226-389,466-692` | Most branches fetch full backend lists with no MCP-side limit, accumulate strings in memory, and render all entries. Only function callers and one affected-file display are capped. | Unbounded result/response scaling with graph size. Medium. |
| `_queue_summary` (`formatters.py:540-565`) | Three serial backend reads; active rows capped at 200. | Bounded count, but imposed on every ordinary successful write and after commit. |
| `_collect_episode_status` (`formatters.py:333-420`) | Deadline, terminal checks, sleep. | Disproved as unbounded. |
| `_session_cache` (`service_access.py:28-30,137-154`) | No size/expiry bound. | Process-lifetime caller/session accumulation. Low. |
| Query-auth event map (`contracts.py:101-127`) | Events expire per deque; identity keys do not. | Process-lifetime distinct-key accumulation. Low. |

## 9. `query_structure.py` Responsibility Decomposition

The file has **eight distinct responsibilities**:

1. structural-root staleness constants and filesystem checks — lines 9-49;
2. public function/tool schema — 52-98;
3. project listing, orphan/stale/coverage diagnostics, project preflight — 100-224;
4. query-type dispatch and backend parameter adaptation — 226-389;
5. coverage qualification and unindexed refusal policy — 392-463;
6. blast-radius rendering — 466-532;
7. affected-test rendering — 535-576;
8. symbols/context/overview/unknown-project rendering — 579-692.

**Verdict:** long but coherent around one structural-query transport. It is not a cross-domain god file. Its main architectural debt is mixing coverage/staleness policy and many unbounded renderers into the tool class; extraction would reduce review surface but not change domain boundaries.

## 10. Bug-Class Sweep Results

Probe committed: `.agent/audit/m3_architecture_probe.py`.

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — direct checkout failed because the execution container could not resolve github.com; no exact checkout is mounted.
```

This applies to import graph/cycles, in-degree, boundary-private imports, duplicate-body comparison, and unread module constants.

Search control failed and the connector was discarded for absence claims:

```text
GitHub.search("register_all_tools", repo="Archolith/menhir")
OUTPUT: []
CONTROL: tools/__init__.py:17-22 defines it; server.py:18,44 imports/calls it.
```

## 11. Disproved Candidates

- Episode polling is bounded (`formatters.py:333-420`).
- Per-tool files are not pure ceremony: each owns a typed signature preserved for MCP discovery (`contracts.py:367-379`), although they provide no startup/failure isolation.
- `query_structure.py` is not a general god file; all eight responsibilities serve one query adapter.

## 12. Open Questions

- One-line pass-by: broad query-auth fallback and empty-tier behavior are correctness/security questions, not graded here (`contracts.py:42-70,325-329`).
- FastMCP pre-handler validation and cancellation behavior remain unexecuted.
- Backend-side clamps may bound some caller-provided limits; no MCP-side bound is credited unless traced.

## 13. Coverage Table

| Status | Files | Lines |
|---|---:|---:|
| READ | 36 | 4,883 |
| PENDING | 34 | 2,339 |
| Requested total | 70 | 7,222 |

Complete groups read: top-level MCP except no remaining top-level files; telemetry; all tool/package initializers; all ingest tools; all recall tools; all conflict tools. Remaining scope is the 34 ops tool modules. The final report will replace this summary with 70 individual rows and an independent physical-line reconciliation.

## 14. What Was Checked

Target commit and branch ancestry verified. No withheld functional-correctness report was read. Registration, all ingest/recall/conflict endpoints, resources, and one complete write path were statically traced. Search was control-tested and rejected. Probe syntax was checked, but its Menhir outputs are `NOT RUN`.

## 15. Review Confidence

**58/100 draft confidence.** All shared plumbing and 36/70 files are read; the 34 ops endpoints, supporting transport files, complete edge counts, and executable probe output remain.
