# Menhir M3 — MCP Surface Architecture Audit

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Scope:** exactly 70 files under `src/menhir/mcp/`  
**Coverage:** 70/70 READ; supplied physical-line manifest reconciled to 7,222  
**Status:** complete, with target-checkout probe output explicitly `NOT RUN`

## 1. Executive Summary

### High — a committed write can be reported as a failed invocation

`add_memory` awaits `backend.queue_episode(...)`, then performs `_queue_summary(backend)` while constructing success text (`src/menhir/mcp/tools/ingest/add_memory.py:108-133`). `_queue_summary` performs three further backend reads (`src/menhir/mcp/formatters.py:540-565`). If any fails, the shared tracker converts the entire invocation to an error response (`src/menhir/mcp/telemetry/tracker.py:52-112`). The caller cannot distinguish “write failed” from “write committed; response assembly failed”; retry can duplicate memory.

The same architectural pattern occurs in `add_memory_and_track` (`tools/ingest/add_memory_and_track.py:80-104`), `flag_memory` (`tools/ingest/flag_memory.py:36-52`), `force_reenrich` (`tools/ops/force_reenrich.py:60-88`), `force_release_enrichment_lease` (`tools/ops/force_release_lease.py:21-59`), and scheduler mutation tools that mutate then fetch status (`tools/ops/pause_scheduler.py:27-37`; `resume_scheduler.py:25-37`; `force_scheduler_takeover.py:28-46`).

### Medium — sibling transports form an architecture cycle

The remote API imports MCP contracts, resources, service access, and registries (`src/menhir/api/mcp_remote.py:11-14`). In reverse, `mint_client`, `revoke_client`, and `list_clients` import the API-owned `client_token_store` directly (`src/menhir/mcp/tools/ops/mint_client.py:21-40`; `revoke_client.py:21-29`; `list_clients.py:21-38`). Reverse imports are function-local, avoiding an eager Python cycle, but package architecture remains `api ↔ mcp`. The shared client-management capability should sit behind a service/core port.

### Medium — `recover_orphans` bypasses the backend abstraction

`RecoverOrphansTool` imports concrete `RuntimeProvider`, branches on `isinstance`, and in local mode reaches `backend.built.lifecycle_service`; remote mode calls the protocol method (`src/menhir/mcp/tools/ops/recover_orphans.py:7-9,40-104`). The supporting protocol expressly omits the callback used by the local path (`src/menhir/core/backend_protocol.py:369-378`). One transport tool therefore knows a concrete provider’s internal object graph and owns two orchestration paths.

### Medium — generic MCP telemetry lacks caller and stage correlation

The tracker records operation, kind, duration, error, sizes, and payload preview (`src/menhir/mcp/telemetry/tracker.py:40-136`). It receives no request/correlation ID, client/session identity, caller tier, backend mode, episode identifier as a structured field, or execution stage. Endpoint validation failures returned normally are recorded as successful completions, including `add_memory` validation and backend `status=failed` paths (`src/menhir/mcp/tools/ingest/add_memory.py:75-87,121-124`; `src/menhir/mcp/telemetry/tracker.py:113-136`).

The composition root is thin (`src/menhir/mcp/server.py:25-45`). Four eager registries hold 10 ingest, 5 recall, 5 conflict, and 34 ops classes: **54 tools** (`src/menhir/mcp/tools/__init__.py:7-22`; group initializers). All share the same dispatcher, policy, service access, timeout conversion, telemetry, and warning drain (`src/menhir/mcp/contracts.py:239-379`).

## 2. Layering Edge Table and Violation Judgements

Counts are distinct scoped files with at least one static import; function-local and `TYPE_CHECKING` imports are included. Exact alias-edge output is delegated to the probe and remains `NOT RUN`.

| MCP imports | Files | Judgement |
|---|---:|---|
| `menhir.api` | 3 | **Violation.** The three client-management tools reach a sibling transport’s concrete store (`mint_client.py:21-40`; `revoke_client.py:21-29`; `list_clients.py:21-38`). |
| `menhir.config` | 2 | Allowed composition/config edge (`resources.py:14-22`; `service_access.py:13-17`). |
| `menhir.core` | 5 | Mixed. `backend_protocol` is allowed; `backend_impl`, private runtime state, and concrete-provider branching are violations (`contracts.py:15,357-363`; `service_access.py:15-25,243-286`; `lifecycle.py:10-39`; `recover_orphans.py:7-9,70-84`). |
| `menhir.domain` | 9 | Allowed inward dependency on domain types and validation. |
| `menhir.infrastructure` | 5 | Mixed. Composition logging is defensible; private telemetry helpers and endpoint-level scheduler tracing leak infrastructure details (`telemetry/tracker.py:10`; `recover_orphans.py:7-9,55-104`). |
| `menhir.services` | 2 | Allowed. `ingest_project` delegates orchestration and formats a transport-neutral result (`ingest_project.py:7-11,60-117`). |
| `menhir.mcp` | 67 | Internal graph concentrated around bases, registries, formatter helpers, and service access. |

Framework coupling is split across `cth_mcp_framework`, `fastmcp`, and `mcp.server.fastmcp` (`server.py:9-14`; `contracts.py:24-25`; `tools/__init__.py:5-13`; `lifecycle.py:14-15`; `resources.py:16-18`).

### Cycles

The full static MCP graph contains a statically traceable 61-module SCC: `contracts` function-locally imports `ALL_TOOLS` (`contracts.py:43-74`); the tool registry imports four group registries (`tools/__init__.py:7-22`); those import 54 tool modules; each imports `tools.base`; `tools.base` imports `contracts` (`tools/base.py:1-7`). Count: `contracts` + `tools` + `tools.base` + 4 group initializers + 54 tool modules = 61.

The eager import graph is statically acyclic because the return edge from `contracts` to the registry is function-local. This is a static trace, not executed SCC output.

At package level, `api → mcp` is eager (`api/mcp_remote.py:11-14`), while `mcp → api` is function-local in the three client-management tools.

## 3. Blast Radius Register

Manual distinct-file in-degree, with full-static reverse-transitive dependents where reconciled:

| Module | Direct | Reverse-transitive | What breaks |
|---|---:|---:|---|
| `mcp.tools.base` | **54** | 63 | Re-export removal/rename prevents all tool modules loading (`tools/base.py:1-7`). |
| `mcp.formatters` | **14** | 63 | Conflict, ingest, recall, and ops depend on leading-underscore helpers, not a supported interface (`formatters.py:25-178,333-420,540-616`). |
| `mcp.service_access` | **13** | 65 | Session/context policy, cache, concrete provider selection, and private runtime fallback affect every backend-using path (`service_access.py:28-30,137-239,243-314`). |
| `mcp.telemetry` | 8 | not separately reconciled | Cross-cutting sidecar/tracker access. |
| `mcp.lifecycle` | 3 | not separately reconciled | Server plus bootstrap recall tools. |
| `mcp.feedback` | 3 | not separately reconciled | Recall receipt creation/rating. |
| `mcp.contracts` | 2 | 63 | Low direct fan-in; high transitive reach through `tools.base` and resources. |
| `mcp.resources` | 1 | 1 | Large aggregate, not an inbound hub. |

`resources.py` is therefore a disproved hub candidate: only `server.py` imports it in scope (`server.py:18,44`). All tool classes are eagerly imported before registration; one import-time failure prevents complete MCP construction instead of disabling one endpoint (`tools/__init__.py:7-22`). **Medium.**

## 4. Outward Coupling Register

Confirmed outside-MCP use of an MCP-private symbol:

| Importer | Symbol | Definition | Impact |
|---|---|---|---|
| `src/menhir/api/mcp_remote.py:11` | `menhir.mcp.contracts._tier_allows` | `src/menhir/mcp/contracts.py:133-135` | Remote catalogue filtering depends on an undocumented MCP implementation helper. |

The repository search connector returned no result for visible `register_all_tools`; control evidence is `tools/__init__.py:17-22` and `server.py:18,44`. The search instrument was discarded. Because the exhaustive probe could not run, this report says **one confirmed instance**, not “exactly one.”

Reverse private coupling found while reading MCP:

- `telemetry/tracker.py:10` imports `_preview_of`, `_size_of`, `_utc_now_iso` through `infrastructure.telemetry.store`; definitions originate in `infrastructure/telemetry/helpers.py` (`store.py:20-26`; `helpers.py:16-18,42-50`).
- `service_access.py:251-258,281-286` reads `core.runtime._state`, re-exported from `core/runtime_support.py` (`core/runtime.py:22-30`; `core/runtime_support.py:95-96`).
- `lifecycle.py:10-39` reaches runtime `_state` and private lifecycle helpers.

## 5. Tool Dispatch Architecture

1. `server.py` creates the gateway and registers tools/resources (`server.py:25-45`); remote HTTP/SSE uses separate FastMCP instances and the same registries (`api/mcp_remote.py:59-111`).
2. `tools/__init__.py` concatenates four class lists, instantiates each class, and calls `.register(mcp)` (`tools/__init__.py:7-22`).
3. `BaseTool.register` preserves endpoint signature/doc metadata, renames the handler to the MCP name, and passes it to `mcp.tool()` (`contracts.py:367-379`).
4. `BaseTool.execute` applies query-auth policy, tier, client allowlist, operator audit attempt, and pinned namespace before dispatch (`contracts.py:292-356`).
5. Endpoints obtain `RuntimeProvider` or `BackendClient` through `service_access` (`contracts.py:247-248`; `service_access.py:243-270`).
6. `track_mcp_call` applies timeout, logging, persistence, and exception conversion (`tracker.py:40-136`); warning drain runs afterward outside that wrapper (`contracts.py:357-366`).
7. Remote `tools/list` filtering separately duplicates visibility policy and privately imports `_tier_allows` (`api/mcp_remote.py:11-55`).

**Verdict:** per-tool modules are real endpoint-schema and ownership boundaries, not pure ceremony. They are not import, deployment, authorization, timeout, telemetry, or failure-isolation boundaries. The client-management modules are the clearest ceremony case because each thin endpoint directly reaches a sibling transport’s store.

## 6. Failure-Mode Trace — `add_memory`

Path: `server.py:25-45` → `tools/__init__.py:7-22` → `tools/ingest/__init__.py:3-14` → `contracts.py:367-379` → `contracts.py:292-366` → `tools/ingest/add_memory.py:48-133` → backend → `formatters.py:540-576` → `tracker.py:40-136`.

| Failure | Result |
|---|---|
| Malformed MCP argument | **NOT EXECUTED.** FastMCP validation packaging and tracker participation remain Open Question. |
| Query-auth/tier/allowlist refusal | `PermissionError` is logged/persisted and converted by tracker (`contracts.py:292-346`; `tracker.py:82-112`). |
| Namespace/bootstrap invalid | Normal prose return; tracker records success (`add_memory.py:75-87`). |
| Runtime/backend unavailable | `service_access` raises; tracker converts (`service_access.py:243-270`). |
| Neo4j unavailable | Selected driver/message patterns map to a degraded-store explanation (`tracker.py:17-38,82-112`). |
| Timeout | `asyncio.wait_for`; logged/persisted and returned (`tracker.py:52-81`). Blocking synchronous work cannot be interrupted until it yields. |
| Backend returns `status=failed` | Reduced to `Failed to store memory.` normal return; tracker records success (`add_memory.py:121-124`). |
| Write commits; summary read fails | Whole invocation becomes failure after commit (`add_memory.py:108-120`; `formatters.py:540-565`). |
| Advisory count read fails | Swallowed and debug-logged; response continues (`formatters.py:510-537`). |
| Tracker persistence fails | `sqlite3.Error` is logged and ignored; another exception class can replace the tool outcome (`tracker.py:70-78,92-103,119-133`). |
| Warning drain fails | Runs after tracker, outside its handling (`contracts.py:357-366`). |
| Cancellation | Python 3.12 cancellation is not caught by `except Exception`; outer FastMCP behavior was not executed. |

`ingest_document` is the positive comparison: structural-write success is reported separately from semantic queue timeout/error (`tools/ingest/ingest_document.py:54-103`).

## 7. Observability Assessment

One generic tracker log identifies the tool, but not caller, request, deployment mode, or stage. Normal error payloads count as successful calls, including `add_memory` validation/status failures (`add_memory.py:75-87,121-124`), invalid conflict status (`tools/conflict/list_conflicts.py:75-82`), and caught namespace deletion errors (`tools/ops/delete_namespace.py:50-57`). Metrics therefore measure “handler returned” more than “operation succeeded.” **Medium.**

Observability failures are often silent/best-effort: query-auth/destructive audit (`contracts.py:132-166`), session/client touches (`service_access.py:157-171,300-311`), recall receipts (`feedback.py:57-81`), stale TODO warning (`recall_context_memories.py:145-153`), and conflict suppression (`resolve_conflict.py:248-269`). `get_episode_trace` is useful post-hoc (`tools/ops/get_episode_trace.py:20-99`) but does not supply generic call correlation.

## 8. Fan-Out Register

| Location | Scaling / judgement |
|---|---|
| `recall_context_memories.py:100-137` | Serial `fetch_memory_by_uuid` per relevant result plus recent fetch at `recent_limit * 3`; no MCP clamp. N+1. **Medium.** |
| `resolve_conflict.py:78-146,190-242` | Up to 5,000-group scan before and after mutation; `keep_both` writes every pair. Large fixed scan plus quadratic pair fan-out. **Medium/High.** |
| `query_structure.py:226-389,466-692` | Most branches fetch/render complete result lists in memory; mostly uncapped. **Medium.** |
| `recover_orphans.py:40-104` | Fetches, retains, and processes every old session entity; no tool/protocol limit. **Medium.** |
| `audit_artifact_corpus.py:45-91` | Deliberate whole-repository scan with no tool-side work bound. **Low/Medium.** |
| `get_provenance.py:39-84` | Text clipped per episode, but all episodes/evidence/anchors returned. **Low/Medium.** |
| `get_artifact_relationships.py:32-56` | All relationship rows returned; no item cap. **Low.** |
| `list_clients.py:21-38` | Synchronous full materialization; no pagination. **Low.** |
| `list_conflicts.py:29-74` | Caller limit passed directly despite documented max 200. **Low/Medium.** |
| `run_llm_review.py:27-33`; `scan_conflicts.py:31-37`; `requeue_for_review.py:34-46` | Caller controls review/scan/requeue volume; backend bound unverified. **Medium.** |
| `get_memory_stats.py:34-43` | Seven serial reads; one failure loses full report. **Low/Medium.** |
| `_queue_summary`, `formatters.py:540-565` | Three serial post-write reads; bounded to 200 active rows but creates response-atomicity risk. |
| `_collect_episode_status`, `formatters.py:333-420` | **Disproved unbounded:** deadline, terminal checks, and minimum sleep. |
| `_session_cache`, `service_access.py:28-30,137-154` | Process-lifetime caller/session entries; no expiry/size bound. **Low.** |
| Query-auth event map, `contracts.py:101-127` | Deques expire; identity keys do not. **Low.** |

## 9. `query_structure.py` Responsibility Decomposition

Eight coherent responsibilities:

1. staleness declarations/filesystem classification — lines 9-49;
2. public function/tool schema — 52-98;
3. project listing and orphan/stale/coverage diagnostics — 100-224;
4. query dispatch/backend parameter adaptation — 226-389;
5. coverage qualification/unindexed refusal — 392-463;
6. blast-radius rendering — 466-532;
7. affected-test rendering — 535-576;
8. symbol/context/overview/unknown-project rendering — 579-692.

**Verdict:** long, but cohesive around one structural-query transport; not a cross-domain god file. `STRUCT_STALE_REASON` and `STRUCT_STALE_ACTION` at `query_structure.py:16-17` are unread; only `STRUCT_STALE_ADVISORY` affects output (`query_structure.py:18-23,181-188,213-222`).

## 10. Bug-Class Sweep Results

Probe: `.agent/audit/m3_architecture_probe.py`. It is read-only, standard-library-only, imports no Menhir code, distinguishes full/eager imports, computes direct/transitive in-degree, resolves private imports/attributes, compares bodies without docstrings, checks duplicate tool-name literals, and reports statically unread constants.

### Instrument control — executed

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --self-test
OUTPUT: SELF-TEST PASS: relative imports; local/TYPE_CHECKING context; function-default loads; dotted private module attributes; body hashes; SCCs
```

The repository search connector failed its control:

```text
SEARCH: register_all_tools in Archolith/menhir
OUTPUT: []
CONTROL: tools/__init__.py:17-22 defines it; server.py:18,44 imports/calls it.
RESULT: connector search discarded for absence claims.
```

### Duplicate definitions across files

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — target checkout unavailable. Checkout attempt:
  fatal: unable to access 'https://github.com/Archolith/menhir.git/':
         Could not resolve host: github.com
```

Static read found no duplicate module-level function/class name or duplicate registered tool-name literal. Repeated class methods such as `endpoint` are dispatch hooks with intentionally different bodies. Exact body hashes remain `NOT RUN`.

### Unread invariant constants

Same target command/output: **NOT RUN**. Static confirmed candidates:

- `RATABLE_OPERATIONS` declares four receipt operations (`feedback.py:33-36`), but `mint_recall_receipt` accepts any operation and never checks the set (`feedback.py:52-74`). **Low.**
- `STRUCT_STALE_REASON` and `STRUCT_STALE_ACTION` are documentation-only (`query_structure.py:16-17`). **Low.**

### Cross-module private imports

Same target command/output: **NOT RUN**. Confirmed boundary rows are in section 4. Within MCP, private imports include server/lifecycle state (`server.py:14-17`; `lifecycle.py:58-59`) and formatter helpers imported across conflict, ingest, recall, and ops (`tools/conflict/list_conflicts.py:6-10`; `resolve_conflict.py:7-8`; `tools/ingest/add_memory.py:5-7`; `add_memory_and_track.py:5-7`; `tools/recall/read_flagged_memories.py:7-9`; `recall_context_memories.py:7-9`; `recall_memories.py:8-10`; `tools/ops/force_reenrich.py:5-6`; `force_release_lease.py:5-7`; `get_enrichment_status.py:5-10`; `get_episode_trace.py:6-8`; `list_enrichment_queue.py:7-8`; `repair_stale_enrichment.py:5-6`; `watch_enrichment.py:5-6`). `formatters.py` is an internal service interface in practice despite private naming. **Low/Medium.**

## 11. Disproved Candidates

1. `resources.py` is a shared hub — disproved; only `server.py` imports it in scope (`server.py:18,44`).
2. `contracts.py` has highest direct in-degree — disproved; its blast is transitive.
3. The eager MCP import graph is cyclic — disproved by trace; the return edge is function-local (`contracts.py:43-74`).
4. Episode polling is unbounded — disproved (`formatters.py:333-420`).
5. Every per-tool module is ceremony — disproved by preserved typed endpoint schemas (`contracts.py:367-379`), though no failure isolation is gained.
6. `query_structure.py` is a cross-domain god file — disproved by section 9’s cohesive ranges.

## 12. Open Questions

- Is `api/mcp_remote.py:11` the only production outside→MCP private reference? Exhaustive probe output is `NOT RUN`.
- How does FastMCP package malformed arguments and propagated cancellation?
- Do backend implementations clamp direct caller limits in conflict/review/scan tools?
- What exact duplicate-body/tool counts does the probe produce on a clean checkout?
- Can tracker persistence emit non-`sqlite3.Error` exceptions on current paths?
- Pass-by only: query-auth fallback and empty-tier behavior are correctness/security scope, not graded here (`contracts.py:43-74,325-329`).

## 13. Coverage Table

Every row was read at the pinned commit. Counts are the corrected supplied manifest; independent filesystem measurement is `NOT RUN`.

| File | Lines | Status |
|---|---:|---|
| `src/menhir/mcp/__init__.py` | 1 | READ |
| `src/menhir/mcp/contracts.py` | 407 | READ |
| `src/menhir/mcp/feedback.py` | 95 | READ |
| `src/menhir/mcp/formatters.py` | 616 | READ |
| `src/menhir/mcp/lifecycle.py` | 84 | READ |
| `src/menhir/mcp/resources.py` | 521 | READ |
| `src/menhir/mcp/server.py` | 68 | READ |
| `src/menhir/mcp/service_access.py` | 314 | READ |
| `src/menhir/mcp/telemetry/__init__.py` | 34 | READ |
| `src/menhir/mcp/telemetry/tracker.py` | 136 | READ |
| `src/menhir/mcp/tools/__init__.py` | 22 | READ |
| `src/menhir/mcp/tools/base.py` | 7 | READ |
| `src/menhir/mcp/tools/conflict/__init__.py` | 9 | READ |
| `src/menhir/mcp/tools/conflict/list_conflicts.py` | 82 | READ |
| `src/menhir/mcp/tools/conflict/requeue_for_review.py` | 46 | READ |
| `src/menhir/mcp/tools/conflict/resolve_conflict.py` | 269 | READ |
| `src/menhir/mcp/tools/conflict/run_llm_review.py` | 33 | READ |
| `src/menhir/mcp/tools/conflict/scan_conflicts.py` | 37 | READ |
| `src/menhir/mcp/tools/ingest/__init__.py` | 14 | READ |
| `src/menhir/mcp/tools/ingest/add_candidate.py` | 123 | READ |
| `src/menhir/mcp/tools/ingest/add_memory.py` | 133 | READ |
| `src/menhir/mcp/tools/ingest/add_memory_and_track.py` | 104 | READ |
| `src/menhir/mcp/tools/ingest/close_memory.py` | 33 | READ |
| `src/menhir/mcp/tools/ingest/delete_memory.py` | 39 | READ |
| `src/menhir/mcp/tools/ingest/flag_memory.py` | 52 | READ |
| `src/menhir/mcp/tools/ingest/ingest_document.py` | 103 | READ |
| `src/menhir/mcp/tools/ingest/ingest_project.py` | 117 | READ |
| `src/menhir/mcp/tools/ingest/promote_memory.py` | 50 | READ |
| `src/menhir/mcp/tools/ingest/unflag_memory.py` | 35 | READ |
| `src/menhir/mcp/tools/ops/__init__.py` | 74 | READ |
| `src/menhir/mcp/tools/ops/add_todo.py` | 84 | READ |
| `src/menhir/mcp/tools/ops/audit_artifact_corpus.py` | 91 | READ |
| `src/menhir/mcp/tools/ops/close_stale_todos.py` | 47 | READ |
| `src/menhir/mcp/tools/ops/close_todo.py` | 29 | READ |
| `src/menhir/mcp/tools/ops/delete_namespace.py` | 57 | READ |
| `src/menhir/mcp/tools/ops/force_reenrich.py` | 88 | READ |
| `src/menhir/mcp/tools/ops/force_release_lease.py` | 59 | READ |
| `src/menhir/mcp/tools/ops/force_scheduler_takeover.py` | 46 | READ |
| `src/menhir/mcp/tools/ops/get_artifact.py` | 75 | READ |
| `src/menhir/mcp/tools/ops/get_artifact_relationships.py` | 56 | READ |
| `src/menhir/mcp/tools/ops/get_client_context.py` | 71 | READ |
| `src/menhir/mcp/tools/ops/get_enrichment_status.py` | 109 | READ |
| `src/menhir/mcp/tools/ops/get_episode_trace.py` | 99 | READ |
| `src/menhir/mcp/tools/ops/get_memory_stats.py` | 155 | READ |
| `src/menhir/mcp/tools/ops/get_provenance.py` | 84 | READ |
| `src/menhir/mcp/tools/ops/get_todo.py` | 78 | READ |
| `src/menhir/mcp/tools/ops/link_artifacts.py` | 54 | READ |
| `src/menhir/mcp/tools/ops/list_artifact_questions.py` | 60 | READ |
| `src/menhir/mcp/tools/ops/list_artifacts.py` | 71 | READ |
| `src/menhir/mcp/tools/ops/list_clients.py` | 38 | READ |
| `src/menhir/mcp/tools/ops/list_enrichment_queue.py` | 66 | READ |
| `src/menhir/mcp/tools/ops/list_todos.py` | 65 | READ |
| `src/menhir/mcp/tools/ops/mint_client.py` | 40 | READ |
| `src/menhir/mcp/tools/ops/pause_scheduler.py` | 37 | READ |
| `src/menhir/mcp/tools/ops/rate_recall.py` | 123 | READ |
| `src/menhir/mcp/tools/ops/recover_orphans.py` | 104 | READ |
| `src/menhir/mcp/tools/ops/relocate_artifact_source.py` | 100 | READ |
| `src/menhir/mcp/tools/ops/repair_stale_enrichment.py` | 67 | READ |
| `src/menhir/mcp/tools/ops/resume_scheduler.py` | 37 | READ |
| `src/menhir/mcp/tools/ops/revoke_client.py` | 29 | READ |
| `src/menhir/mcp/tools/ops/supersede_artifact.py` | 41 | READ |
| `src/menhir/mcp/tools/ops/transition_artifact.py` | 50 | READ |
| `src/menhir/mcp/tools/ops/view_entropy.py` | 65 | READ |
| `src/menhir/mcp/tools/ops/watch_enrichment.py` | 64 | READ |
| `src/menhir/mcp/tools/recall/__init__.py` | 9 | READ |
| `src/menhir/mcp/tools/recall/build_context.py` | 121 | READ |
| `src/menhir/mcp/tools/recall/query_structure.py` | 692 | READ |
| `src/menhir/mcp/tools/recall/read_flagged_memories.py` | 67 | READ |
| `src/menhir/mcp/tools/recall/recall_context_memories.py` | 174 | READ |
| `src/menhir/mcp/tools/recall/recall_memories.py` | 162 | READ |
| **Total** | **7,222** | **70/70 READ** |

## 14. What Was Checked, and What Could Not Be Verified

Checked: pinned commit/branch ancestry; every scoped file; required supporting context; both composition paths; 54 registrations; complete `add_memory` path; shared-plumbing blast radius; private dependencies; fan-out; `query_structure.py`; failed-search control; probe syntax and `--self-test`; branch diff restricted to the report and probe.

Not verified: target-checkout probe JSON/count output; independent filesystem 70/7,222 measurement; exhaustive outside-private absence; executed FastMCP malformed-argument/cancellation behavior; live backend/Neo4j/SQLite failure reproductions.

No withheld functional-correctness report was read.

## 15. Review Confidence

**78/100.** All scope and supporting context were read; principal architecture and failure paths are traced with exact file:line evidence; probe self-controls passed. Confidence remains below 80 because the target-checkout probe could not run, physical-line reconciliation is manifest-based, and the outward-private absence sweep cannot be declared exhaustive.
