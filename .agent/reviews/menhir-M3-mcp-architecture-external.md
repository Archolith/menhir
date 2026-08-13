# Menhir M3 — MCP Surface Architecture Audit

**Target:** `Archolith/menhir@eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Branch:** `audit/m3-mcp-architecture-external`  
**Scope:** exactly 70 Python files under `src/menhir/mcp/`; scope manifest reconciles to 7,222 physical lines  
**Status:** DRAFT CHECKPOINT — all 70 scoped files read; mechanical probe committed but target-checkout execution remains unavailable

## 1. Executive Summary

### High — a committed write can be returned as a failed invocation

`add_memory` awaits the durable `backend.queue_episode(...)` mutation and then performs `_queue_summary(backend)` while constructing the success text (`src/menhir/mcp/tools/ingest/add_memory.py:108-133`). `_queue_summary` makes three more backend reads before it can return (`src/menhir/mcp/formatters.py:540-565`). A failure in any of those reads is caught by the shared tracker and converted into an ordinary error payload (`src/menhir/mcp/telemetry/tracker.py:52-112`). The caller therefore cannot distinguish “write failed” from “write committed, response enrichment failed”; a retry can duplicate memory.

This is a repeated architectural failure mode, not a one-off endpoint defect. `add_memory_and_track` writes, polls, and performs the same summary reads (`src/menhir/mcp/tools/ingest/add_memory_and_track.py:80-104`); `flag_memory` mutates and then rereads the node only to construct confirmation (`src/menhir/mcp/tools/ingest/flag_memory.py:36-52`); `force_reenrich` resets state and then separately enqueues and polls (`src/menhir/mcp/tools/ops/force_reenrich.py:60-88`); `force_release_enrichment_lease` releases the lease and then performs telemetry, reread, and optional requeue work (`src/menhir/mcp/tools/ops/force_release_lease.py:21-59`); scheduler pause/resume/takeover tools mutate and then fetch a status snapshot (`src/menhir/mcp/tools/ops/pause_scheduler.py:27-37`; `resume_scheduler.py:25-37`; `force_scheduler_takeover.py:28-46`).

### Medium — sibling transports form an architectural dependency cycle

The intended transport siblings are not independent. The remote API imports MCP contracts, resources, service access, and the tool registry (`src/menhir/api/mcp_remote.py:11-14`). In the reverse direction, three MCP operator tools import the API-owned `client_token_store` and call it directly (`src/menhir/mcp/tools/ops/mint_client.py:21-40`; `revoke_client.py:21-29`; `list_clients.py:21-38`). Those reverse imports are function-local, so they avoid an eager Python import-cycle failure, but the package architecture is still `api ↔ mcp`. Token administration belongs behind a service/core interface or an infrastructure port, not inside one transport and reached from another.

### Medium — the shared dispatcher cannot reliably tell an operator who failed or at what stage

The tracker logs `kind`, `operation`, duration, and error text, and stores sizes plus a payload preview (`src/menhir/mcp/telemetry/tracker.py:40-136`). It does not carry request ID, client ID, session ID, caller tier, backend mode, episode ID as a structured correlation field, or stage. Application-level errors returned as strings/JSON are recorded as successful calls—for example invalid namespace/bootstrap input in `add_memory` returns normally (`src/menhir/mcp/tools/ingest/add_memory.py:75-87`) and the tracker then executes its success path (`src/menhir/mcp/telemetry/tracker.py:113-136`). One log line can identify the tool, but not the caller or whether failure occurred before mutation, during the backend call, or after commit.

### Structural shape

The MCP server is a thin composition root (`src/menhir/mcp/server.py:25-45`). Four eager registries contain 10 ingest, 5 recall, 5 conflict, and 34 ops classes: **54 registered tools** (`src/menhir/mcp/tools/__init__.py:7-22`; group initializers). Every tool shares one inherited execution path for registration, authorization, namespace forcing, timeout/error conversion, telemetry, and warning draining (`src/menhir/mcp/contracts.py:239-379`). The per-module files provide useful schema/ownership boundaries, but not failure isolation.

All 70 scope files were read. The executable probe is committed at `.agent/audit/m3_architecture_probe.py`, but exact AST output remains `NOT RUN` because no checkout is mounted and the execution container cannot resolve GitHub. Counts explicitly identified below as “manual static manifest” were calculated from the imports read at the pinned commit, not represented as probe output.

## 2. Layering Edge Table and Violation Judgements

### 2.1 Internal package edges from MCP

Count means **distinct scoped files containing at least one import edge** to the package. Function-local imports are included; the file list makes the measurement auditable. Alias-level counts are delegated to the committed probe and remain `NOT RUN`.

| MCP imports | Distinct files | Importing files | Judgement |
|---|---:|---|---|
| `menhir.api` | 3 | `tools/ops/{mint_client,revoke_client,list_clients}.py` | **Violation.** Sibling transport dependency and reverse half of `api ↔ mcp`; all three reach API-owned concrete token storage (`mint_client.py:21-40`; `revoke_client.py:21-29`; `list_clients.py:21-38`). |
| `menhir.config` | 2 | `resources.py`, `service_access.py` | Allowed at composition/access boundaries (`resources.py:14-22`; `service_access.py:13-17`). |
| `menhir.core` | 5 | `contracts.py`, `formatters.py`, `lifecycle.py`, `service_access.py`, `tools/ops/recover_orphans.py` | Mixed. `backend_protocol` is the intended inward port; direct `backend_impl`, private runtime state, and `RuntimeProvider` type checks are violations (`contracts.py:15,357-363`; `service_access.py:15-25,243-286`; `lifecycle.py:10-39`; `recover_orphans.py:7-9,70-84`). |
| `menhir.domain` | 9 | `formatters.py`, `resources.py`, `service_access.py`, `tools/conflict/list_conflicts.py`, `tools/ingest/add_memory.py`, `tools/recall/{build_context,read_flagged_memories,recall_context_memories}.py`, `tools/ops/get_episode_trace.py` | Direction is allowed: transport depends inward on domain types/validation. The concern is breadth, not direction. |
| `menhir.infrastructure` | 5 | `contracts.py`, `server.py`, `telemetry/__init__.py`, `telemetry/tracker.py`, `tools/ops/recover_orphans.py` | Mixed. Logging/telemetry at composition is defensible; private telemetry helpers and scheduler tracing coupled directly into endpoint execution are violations (`telemetry/tracker.py:10`; `recover_orphans.py:7-9,55-104`). |
| `menhir.services` | 2 | `formatters.py`, `tools/ingest/ingest_project.py` | Direction is allowed. `ingest_project` is a positive example: orchestration is delegated to `services.project_ingest` and MCP formats a transport-neutral outcome (`ingest_project.py:7-11,60-117`). |
| `menhir.mcp` | 67 | Every scoped file except `mcp/__init__.py`, `formatters.py`, and `telemetry/tracker.py` | Internal package graph. It is deliberately centralized around tool bases/registries; see cycles and in-degree below. |

### 2.2 Third-party/transport-framework edges

- `server.py` depends on `cth_mcp_framework`, while `contracts.py` and `tools/__init__.py` type-check against `fastmcp`; `lifecycle.py` and `resources.py` type-check against `mcp.server.fastmcp` (`server.py:9-14`; `contracts.py:24-25`; `tools/__init__.py:5-13`; `lifecycle.py:14-15`; `resources.py:16-18`). This is two MCP framework surfaces, not one stable transport port.
- `service_access.py` owns `httpx` backend probing/client-mode selection (`service_access.py:11-17,122-137`). That is acceptable composition work, but it makes this module both policy and concrete transport factory.

### 2.3 Cycles

**Full static MCP graph:** one deliberate **61-module strongly connected component** is traceable without execution: `contracts` performs a function-local import of `ALL_TOOLS` (`contracts.py:43-74`); `tools/__init__.py` imports four group registries (`tools/__init__.py:7-22`); each group imports its tool modules; all 54 tool modules import `tools.base`; and `tools.base` imports `contracts` (`tools/base.py:1-7`). The component is exactly `contracts` + `tools` + `tools.base` + four group initializers + 54 tool modules = 61 modules.

**Eager module-import graph:** the back-edge from `contracts` to `tools` is function-local. Reading every eager import found no return edge into `contracts`, so the eager graph is **statically acyclic**. This is a disproved startup-cycle candidate, not an executed result; the probe separately reports full and eager SCCs when run.

**Package cycle:** `api → mcp` is eager in `api/mcp_remote.py:11-14`; `mcp → api` is function-local in the three client-token tools. This avoids immediate import recursion while retaining the architecture cycle and change blast radius.

## 3. Blast Radius Register

### 3.1 Direct in-degree

These counts are from the manually enumerated import manifest; the probe will independently calculate them when executable.

| Module | Direct in-degree (scoped files) | Full-static reverse-transitive dependents | Interpretation |
|---|---:|---:|---|
| `mcp.tools.base` | **54** | 63 | Highest direct hub. Every tool module imports its re-exported base. |
| `mcp.formatters` | **14** | 63 | Second direct hub; nearly all coupling is to leading-underscore helpers. |
| `mcp.service_access` | **13** | 65 | Third direct hub; direct callers plus `contracts` put backend/session policy on every tool path. |
| `mcp.telemetry` | 8 | not separately reconciled | Cross-cutting sidecar/tracker access. |
| `mcp.lifecycle` | 3 | not separately reconciled | Server plus two bootstrap recall tools. |
| `mcp.feedback` | 3 | not separately reconciled | Receipt rendering/rating. |
| `mcp.contracts` | 2 | 63 | Low direct degree but very high transitive blast through `tools.base` and `resources`. |
| `mcp.resources` | 1 | 1 | Large file, **not** an import hub; only `server.py` imports it in scope. |

The reverse-transitive counts use the full static graph, including function-local imports; the 61-module tool SCC makes them intentionally larger than an eager-import-only calculation.

### 3.2 What breaks if the top three change

1. **`tools.base` — 54 direct importers.** It is only a compatibility re-export (`tools/base.py:1-7`), so coupling is nominally to a stable public interface. Removing/renaming a re-export prevents every tool module from importing; behavioral changes originate in `contracts` but propagate through this shim to all 54 endpoints.
2. **`formatters` — 14 direct importers.** Importers depend on private helpers such as `_collect_episode_status`, `_queue_summary`, `_coerce_iso`, `_require_episode_uuid`, `_compact_memory_item`, and `_resolve_queue_state_filter` (`formatters.py:25-178,333-420,540-616`; import sites listed in section 10). Coupling is to internals, not a stable interface. A helper rename can prevent the complete eager tool registry from loading; a semantic change alters conflict, recall, ingest, and enrichment output together.
3. **`service_access` — 13 direct importers.** Public accessors hide ContextVars, globals, environment settings, a process-lifetime session cache, concrete `RuntimeProvider`/`BackendClient` selection, and private runtime state (`service_access.py:28-30,137-171,177-239,243-314`). The surface looks stable, but the implementation is highly stateful and concrete. Because `contracts.BaseTool.get_backend()` routes every tool through it (`contracts.py:247-248`), effective runtime blast radius is all 54 tools plus all resources.

### 3.3 The four nominated hubs

- `contracts.py`: direct in-degree 2 (`resources.py`, `tools/base.py`), but transitive blast to 63 scope files. Stable base-class API; unstable implementation coupling to concrete warning drain and policy globals (`contracts.py:239-379`).
- `service_access.py`: direct in-degree 13; mixed public interface and runtime/config internals.
- `formatters.py`: direct in-degree 14; private-helper hub.
- `resources.py`: direct in-degree 1. Its 521 lines are endpoint aggregation, not shared-plumbing fan-in. It mixes nine resources, normalization, runtime fingerprints, synchronous process/socket probes, and registration (`resources.py:1-521`), but a change does not fan directly into tools.

### 3.4 Eager registry failure domain

All four group initializers import every tool class before `register_all_tools` runs (`tools/{ingest,recall,conflict,ops}/__init__.py`; `tools/__init__.py:7-22`). One import-time exception in any tool or any shared private formatter prevents construction of the complete MCP server rather than disabling one endpoint. **Severity: Medium.**

## 4. Outward Coupling Register

### Confirmed outside-MCP use of an MCP-private symbol

| Outside importer | Private symbol | Definition | Effect |
|---|---|---|---|
| `src/menhir/api/mcp_remote.py:11` | `menhir.mcp.contracts._tier_allows` | `src/menhir/mcp/contracts.py:133-135` | API catalog filtering is coupled to a private MCP implementation helper. A rename/refactor breaks remote MCP construction; it also makes API visibility policy mirror MCP invocation policy through an undocumented seam. |

The connector’s code search was control-tested and returned empty for `register_all_tools`, which is visibly defined and called (`tools/__init__.py:17-22`; `server.py:18,44`). Therefore no search-derived absence claim is made. The committed probe scans every `src/menhir/**/*.py` import and module-alias private attribute access, records definition sites, and will determine whether the row above is exhaustive when executed. Until then, “one confirmed production instance” is the honest result, not “exactly one.”

### Reverse private coupling observed while reading MCP

- `telemetry/tracker.py:10` imports `_preview_of`, `_size_of`, and `_utc_now_iso` through `infrastructure.telemetry.store`; that module itself re-exports them from `infrastructure/telemetry/helpers.py` (`store.py:20-26`; `helpers.py:16-18,42-50`).
- `service_access.py:251-258,281-286` imports/reads `core.runtime._state`, re-exported from `core/runtime_support.py` (`core/runtime.py:22-30`; `core/runtime_support.py:95-96`).
- `lifecycle.py:10-39` aliases `core.runtime` and reaches `_state`, `_shutdown_runtime_sync`, `_remember_flagged_bootstrap_read`, and `_has_recent_flagged_bootstrap_read`.

These are package-boundary private dependencies in the opposite direction and are layering violations even though they are not part of the required outside→MCP register.

## 5. Tool Dispatch Architecture

### End-to-end trace

1. **Composition.** `server.py` creates the gateway and eagerly calls `register_all_tools` and `register_memory_resources` (`server.py:25-45`). Remote HTTP/SSE builds a separate FastMCP instance in `api/mcp_remote.py:59-116` and calls the same registration functions.
2. **Registry.** `tools/__init__.py` imports four class lists, concatenates them, instantiates each class, and invokes `.register(mcp)` (`tools/__init__.py:7-22`). Registry sizes are 10 ingest, 5 recall, 5 conflict, and 34 ops = 54 tools.
3. **Schema preservation.** `BaseTool.register` wraps `endpoint`, preserves its signature/doc metadata, sets the handler name to the MCP tool name, and passes it to `mcp.tool()` (`contracts.py:367-379`).
4. **Shared dispatch.** The handler calls `BaseTool.execute`. Its nested runner applies query-auth policy/rate budget, tier check, per-client allowlist, operator audit attempt, and pinned namespace before calling the endpoint (`contracts.py:292-356`).
5. **Service access.** The endpoint calls `self.get_backend()`, which delegates to `build_memory_backend()` and selects an in-process `RuntimeProvider` or HTTP `BackendClient` (`contracts.py:247-248`; `service_access.py:243-270`).
6. **Response/error conversion.** `track_mcp_call` applies timeout, logs/persists outcome, and converts exceptions to tool payloads (`tracker.py:40-136`). `BaseTool.execute` then drains concrete backend-client warnings outside the tracker (`contracts.py:357-366`).
7. **Remote catalog gate.** `api/mcp_remote.py` separately filters `tools/list` by caller allowlist/tier and privately imports `_tier_allows` (`api/mcp_remote.py:11-55`). Invocation authorization remains in `contracts`, so visibility and invocation policy are split across sibling transports.

### Verdict on the per-module pattern

The split is a **real endpoint-schema and ownership boundary**, not pure ceremony: each file owns a named tool, typed FastMCP signature, description, tier declaration, and endpoint-specific formatting. It also keeps most endpoint code locally understandable.

It is **not a deployment, import, authorization, telemetry, timeout, or failure-isolation boundary**. Every module is eagerly imported into one registry, and every call shares `BaseTool.execute`, `track_mcp_call`, `service_access`, and frequently private formatter helpers. The pattern buys local navigation and schema isolation; it spreads centralized policy decisions across 54 endpoint files and four registries without isolating their failures.

The three client-token modules demonstrate where the pattern becomes ceremony: each thin MCP endpoint directly reaches a sibling transport’s synchronous store rather than a service/backend interface (`mint_client.py:21-40`; `revoke_client.py:21-29`; `list_clients.py:21-38`).

## 6. Failure-Mode Trace — `add_memory`

Path: `server.py:25-45` → `tools/__init__.py:7-22` → `tools/ingest/__init__.py:3-14` → `contracts.py:367-379` → `contracts.py:292-366` → `tools/ingest/add_memory.py:48-133` → backend → `formatters.py:540-576` → `tracker.py:40-136`.

| Failure | Where it occurs | Swallowed, converted, or surfaced |
|---|---|---|
| Malformed MCP argument/schema | FastMCP before or while invoking the wrapped handler | **NOT EXECUTED.** Framework validation packaging was not inferred from comments. It may bypass `track_mcp_call`; remains Open Question. |
| Query-auth/tier/allowlist refusal | `contracts.py:292-346` | Raises `PermissionError`; `track_mcp_call` catches `Exception`, logs, persists failure, and returns error text/JSON (`tracker.py:82-112`). Query-auth usage telemetry failure is swallowed (`contracts.py:132-145`). |
| Namespace/bootstrap validation | `add_memory.py:75-87` | Returned as ordinary prose, not raised. Tracker records success and emits a “completed” log. |
| Backend not configured | `service_access.py:243-270` | `build_memory_backend` raises; tracker converts it to an error payload. |
| Backend unreachable / Neo4j down | Backend call at `add_memory.py:108-120`; diagnosis in `tracker.py:17-38` | Selected Neo4j/driver names or messages are converted to a degraded-store explanation; other exceptions become `Type: message`. |
| Backend timeout | `asyncio.wait_for` in `tracker.py:52-81` | Converted to an error payload and timeout telemetry. A synchronous call that blocks the event loop cannot be interrupted until it yields. |
| Backend returns `status=failed` | `add_memory.py:121-124` | Reduced to `Failed to store memory.` as a normal return; tracker records success and loses backend stage/detail. |
| Durable write succeeds; `_queue_summary` fails | Write at `add_memory.py:108-120`; reads at `formatters.py:540-565` | Tracker converts the whole invocation to failure after commit. Caller cannot determine retry safety. |
| Standing-failure advisory read fails | `_standing_unrecallable_count`, `formatters.py:510-537` | Broadly caught and debug-logged; response continues with cached/zero advisory. |
| Tracker telemetry SQLite error | `tracker.py:70-78,92-103,119-133` | `sqlite3.Error` is logged and result continues. A non-`sqlite3.Error` from `store.record` is not caught and can replace the tool outcome. |
| Backend warning drain fails | `contracts.py:357-366` | Runs after `track_mcp_call` has returned, outside its try/telemetry. Exception surfaces through FastMCP and can erase an otherwise successful/error-converted result. |
| Task cancellation | `tracker.py:52-112` | `asyncio.CancelledError` is not caught by `except Exception` in Python 3.12; cancellation propagates with no completed/failure telemetry from this wrapper. FastMCP’s outer handling remains unexecuted. |

### Comparison: an explicitly observable partial success

`ingest_document` writes the structural entity, then wraps semantic episode queueing in its own timeout/exception handling and returns `deferred`/`error` without erasing the first phase (`tools/ingest/ingest_document.py:54-103`). It is non-atomic, but the partial state is visible. That is the better architectural pattern for multi-phase writes.

### Additional mutation chains with the same hazard

- `resolve_conflict` reads up to 5,000 groups, mutates, writes best-effort suppression rows, rereads up to 5,000 groups, then renders confirmation (`tools/conflict/resolve_conflict.py:78-146,190-269`). Suppression failure is swallowed at debug level (`resolve_conflict.py:248-269`); post-mutation reread failure converts the whole call to failure.
- `force_reenrich` resets persistent state, then enqueues, then optionally polls (`tools/ops/force_reenrich.py:60-88`). Enqueue/poll failure can leave reset state behind while the caller sees failure.
- `force_release_enrichment_lease` releases, emits lifecycle telemetry, rereads, and optionally requeues (`tools/ops/force_release_lease.py:21-59`). Any later failure can mask the successful release; telemetry is not best-effort here.
- `recover_orphans` requires a “running” scheduler-trace write before the recovery try block and performs “ready/failed” trace writes around the operation (`tools/ops/recover_orphans.py:55-104`). Trace failure can block the operation or mask the original exception.

## 7. Observability Assessment

### What is emitted

- Failure log: MCP kind, operation, elapsed milliseconds, mapped error (`tracker.py:82-91`).
- Timeout log: operation, timeout, elapsed milliseconds (`tracker.py:60-69`).
- Success log: kind, operation, elapsed milliseconds (`tracker.py:113-119`).
- SQLite row: start/completion timestamps, duration, success flag, error, input/result sizes, payload preview (`tracker.py:70-78,92-103,119-133`).

### What is missing

No structured request/correlation ID, `client_id`, `session_id`, `user_id`, caller tier, auth mode, backend mode, episode UUID, or stage is passed into `track_mcp_call` (`tracker.py:40-51`). `service_access` already exposes request session/tier/auth context (`service_access.py:17-25,177-239`), but the tracker does not consume it.

**Operator question:** given a failed tool call and one log line, can an operator determine tool, caller, and stage?

- Tool: **yes** (`operation`).
- Caller: **no**.
- Stage: **no**; mapped error text sometimes hints Neo4j/SQLite, but cannot distinguish pre-write, backend mutation, post-write summary, warning drain, or telemetry.
- Correlation to a client-visible invocation: **no stable identifier**.

### False-success telemetry

Endpoints frequently return validation/not-found/error payloads normally; the tracker records these as successful completed calls. Confirmed examples include `add_memory` validation/status failures (`add_memory.py:75-87,121-124`), `list_conflicts` invalid status (`tools/conflict/list_conflicts.py:75-82`), and `delete_namespace` caught `ValueError` (`tools/ops/delete_namespace.py:50-57`). Operational success-rate metrics therefore measure “handler returned” more than “operation succeeded.” **Severity: Medium.**

### Swallowed observability failures

- Query-auth usage and destructive-op audit helpers catch all exceptions and do nothing (`contracts.py:132-166`).
- Session/client touch failures are swallowed (`service_access.py:157-171,300-311`).
- Recall receipt failure is debug-only and omitted from the response (`feedback.py:57-81`).
- Stale TODO diagnostics are silently dropped (`recall_context_memories.py:145-153`).
- Conflict suppression persistence is debug-only (`resolve_conflict.py:248-269`).

`get_episode_trace` is a useful post-hoc episode correlator (`tools/ops/get_episode_trace.py:20-99`), and `recover_orphans` creates a job ID for scheduler tracing (`recover_orphans.py:53-104`), but neither identifier is propagated into the generic MCP call log.

## 8. Fan-Out Register

| Location | Scaling behavior | Bound present? | Architecture assessment |
|---|---|---|---|
| `recall_context_memories.py:100-137` | Recall followed by serial `fetch_memory_by_uuid` for every relevant result; then recent fetch at `recent_limit * 3`. | MCP does not clamp `limit` or `recent_limit`. | Confirmed N+1. Backend-client latency grows linearly with requested relevant results. **Medium.** |
| `resolve_conflict.py:78-146,190-242` | Reads up to 5,000 groups to find one, mutates, rereads 5,000; `keep_both` writes every member pair via `combinations`. | Fixed 5,000 scan; pair writes are quadratic in group size. | Large scans plus sequential quadratic fan-out. **Medium/High.** |
| `query_structure.py:226-389,466-692` | Most query branches fetch complete backend lists, build all output lines in memory, and return one text payload. | Function callers capped at 20 and one downstream-file display capped at 20; most branches uncapped. | Graph/result-sized response and memory use. **Medium.** |
| `recover_orphans.py:40-104` | Fetches all session entities older than threshold, retains full candidate list even for dry run, and processes all candidates. | No `limit` in tool/protocol call. | Work and memory scale with orphan corpus. **Medium.** |
| `audit_artifact_corpus.py:45-91` | Requests a whole repository corpus audit. | Tool output truncates conflicts only if backend does; corpus scan itself is unbounded by tool input. | Deliberate full-corpus operation; needs explicit operator expectation/streaming for large repos. **Low/Medium.** |
| `get_provenance.py:39-84` | Clips each episode’s text but returns every episode, evidence row, and anchor path. | Character cap only; no item cap. | Response scales with node provenance degree. **Low/Medium.** |
| `get_artifact_relationships.py:32-56` | Renders all incoming/outgoing edges, subjects, and TODO links. | No item cap. | Response scales with artifact degree. **Low.** |
| `list_clients.py:21-38` | Calls synchronous `store.all()` and materializes every client. | No limit/pagination. | Sidecar-sized accumulation and transport-to-API coupling. **Low.** |
| `list_conflicts.py:29-74` | Passes caller `limit` directly despite docstring claiming max 200. | No MCP clamp; backend bound not credited. | Potential caller-controlled result fan-out. **Low/Medium.** |
| `run_llm_review.py:27-33`, `scan_conflicts.py:31-37`, `requeue_for_review.py:34-46` | Caller limit controls LLM review/scanning/requeue volume. | No MCP clamp. | Potential large single-call work; backend limits remain Open Question. **Medium.** |
| `get_memory_stats.py:34-43` | Seven serial backend reads; any one failure loses the full report. | Number of calls fixed at seven. | Bounded fan-out but all-or-nothing multi-source aggregation. **Low/Medium.** |
| `_queue_summary`, `formatters.py:540-565` | Three serial backend reads on every ordinary successful write. | Active rows capped at 200. | Bounded count, but post-commit and imposed on hot write path. |
| `_collect_episode_status`, `formatters.py:333-420` | Poll loop with status and queue-depth reads. | Deadline, terminal-state checks, and minimum sleep. | **Disproved as unbounded.** |
| `_session_cache`, `service_access.py:28-30,137-154` | One entry per `(user, session, client_id, client_name)`. | No expiry/size bound. | Process-lifetime identity accumulation. **Low.** |
| Query-auth event map, `contracts.py:101-127` | Event deques expire; identity keys remain. | No key eviction. | Process-lifetime distinct-key accumulation. **Low.** |

## 9. `query_structure.py` Responsibility Decomposition

The 692-line file has **eight distinct responsibilities**:

1. structural-root staleness declarations and filesystem classification — lines 9-49;
2. public function/tool schema — lines 52-98;
3. project listing, orphan/stale/coverage diagnostics, and project preflight — lines 100-224;
4. query-type dispatch and backend parameter adaptation — lines 226-389;
5. coverage qualification and unindexed-refusal policy — lines 392-463;
6. blast-radius rendering — lines 466-532;
7. affected-test rendering — lines 535-576;
8. symbol/context/overview/unknown-project rendering — lines 579-692.

**Verdict:** long but coherent around one structural-query transport. It is not a cross-domain god file. Its architecture debt is internal cohesion strain—coverage/staleness policy, backend dispatch, and multiple unbounded renderers live together—but extraction would reduce review surface rather than correct a layer violation.

Two constants at the top claim reusable stale-reason/action semantics but are unread: `STRUCT_STALE_REASON` and `STRUCT_STALE_ACTION` (`query_structure.py:16-17`). Only `STRUCT_STALE_ADVISORY` participates in behavior (`query_structure.py:18-23,181-188,213-222`).

## 10. Bug-Class Sweep Results

Probe: `.agent/audit/m3_architecture_probe.py`. It is read-only, standard-library-only, prints deterministic JSON, distinguishes full from eager import cycles, records direct/transitive in-degree, resolves private module-attribute use, compares module/method bodies without docstrings, checks duplicate tool-name literals, and control-tests visible symbols before trusting absence.

### 10.1 Duplicate definitions across files — compare bodies

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — no target checkout is mounted. Attempted checkout:
  git clone https://github.com/Archolith/menhir.git /mnt/data/menhir-m3
  fatal: unable to access 'https://github.com/Archolith/menhir.git/':
         Could not resolve host: github.com
```

Static reading result, not represented as executed probe output:

- No duplicate **module-level** function/class name was observed across the 70 scoped files.
- Repeated method names such as `endpoint`, `timeout_for`, and `error_mapper` are class-scoped dispatch hooks, not competing module definitions; their bodies intentionally differ (`contracts.py:249-379`; each tool class).
- All 54 registered tool-name literals read as distinct. The exact body-hash and duplicate-name count remains `NOT RUN` until the probe executes.

### 10.2 Module-level constants documenting an invariant nothing reads

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — same environment reason.
```

Static confirmed candidates:

1. `RATABLE_OPERATIONS` declares the four operations that supposedly mint receipts (`feedback.py:33-36`), but `mint_recall_receipt` accepts any operation and does not consult the set (`feedback.py:52-74`). No scoped importer reads it. **Low:** policy declaration can drift independently from behavior.
2. `STRUCT_STALE_REASON` and `STRUCT_STALE_ACTION` declare reason/action identifiers (`query_structure.py:16-17`), but no code in the file reads them; only the advisory string is rendered. **Low:** machine-readable invariant is documentation-only.

### 10.3 Cross-module private-symbol imports in both directions

```text
COMMAND: python .agent/audit/m3_architecture_probe.py --repo .
OUTPUT: NOT RUN — same environment reason.
```

Confirmed outside→MCP row is in section 4 (`api/mcp_remote.py:11` → `contracts.py:133-135`). Confirmed MCP→outside private references are also in section 4.

Within MCP, direct private imports are pervasive:

| Importer | Private dependency |
|---|---|
| `server.py:14-17` | `lifecycle._mcp_lifespan`, `lifecycle._state` |
| `lifecycle.py:58-59` | `service_access._normalized_backend_url` |
| `tools/conflict/list_conflicts.py:6-10` | formatter conflict/date helpers |
| `tools/conflict/resolve_conflict.py:7-8` | formatter member/count helpers |
| `tools/ingest/add_memory.py:5-7` | `formatters._queue_summary` |
| `tools/ingest/add_memory_and_track.py:5-7` | formatter polling/status/summary helpers |
| `tools/recall/read_flagged_memories.py:7-9` | formatter compact/reader helpers; lifecycle bootstrap writer |
| `tools/recall/recall_context_memories.py:7-9` | formatter compact/reader helpers; lifecycle bootstrap reader |
| `tools/recall/recall_memories.py:8-10` | `formatters._compact_scored_item` |
| `tools/ops/force_reenrich.py:5-6` | formatter polling/status/UUID helpers |
| `tools/ops/force_release_lease.py:5-7` | formatter UUID helper |
| `tools/ops/get_enrichment_status.py:5-10` | formatter date/poll/status/UUID helpers |
| `tools/ops/get_episode_trace.py:6-8` | formatter date/UUID helpers |
| `tools/ops/list_enrichment_queue.py:7-8` | formatter date/filter/stale helpers |
| `tools/ops/repair_stale_enrichment.py:5-6` | formatter date helper |
| `tools/ops/watch_enrichment.py:5-6` | formatter poll/watch/UUID helpers |

This is evidence that `formatters.py` is an internal service module in practice despite its leading-underscore API. **Severity: Low/Medium architecture debt:** either promote a supported helper interface or colocate/extract the shared behavior; current naming advertises “private” while 14 files rely on it.

### Instrument control

The repository search connector was rejected for absence claims:

```text
SEARCH: GitHub.search("register_all_tools", repository="Archolith/menhir")
OUTPUT: []
CONTROL: src/menhir/mcp/tools/__init__.py:17-22 defines register_all_tools;
         src/menhir/mcp/server.py:18,44 imports/calls it.
RESULT: search instrument discarded.
```

The revised probe has explicit controls for 70 files, 7,222 lines, visible `BaseTool`, the `tools.base → contracts` edge, and the known `api.mcp_remote → contracts._tier_allows` private import. Its syntax was checked locally with `python -m py_compile`; target-source results remain `NOT RUN`.

## 11. Disproved Candidates

1. **“`resources.py` is a shared hub.”** Disproved by direct static in-degree: only `server.py` imports it in scope (`server.py:18,44`). Its size reflects nine resource endpoints and helper accumulation, not broad inbound coupling (`resources.py:241-521`).
2. **“`contracts.py` has the highest direct in-degree.”** Disproved. Direct importers are `resources.py` and `tools/base.py`; its risk is transitive through the 54-tool shim/registry chain, not direct fan-in (`resources.py:20-22`; `tools/base.py:5-7`).
3. **“The MCP eager import graph is cyclic.”** Disproved by trace: the only return edge from contracts into the registry is function-local (`contracts.py:43-74`). The full static graph is cyclic; eager startup imports are not.
4. **“Episode polling is unbounded.”** Disproved by deadline, terminal-state checks, and sleep (`formatters.py:333-420`).
5. **“Every per-tool file is ceremony.”** Disproved: each endpoint file owns a typed signature preserved for MCP discovery (`contracts.py:367-379`). The split still provides no startup/failure isolation.
6. **“`query_structure.py` is a cross-domain god file because it is long.”** Disproved by the eight responsibility ranges in section 9; they all serve one structural-query adapter.

## 12. Open Questions

- **Exhaustive outward-private count:** the probe must run before claiming the confirmed `api/mcp_remote.py:11` instance is the only production outside→MCP private reference.
- **Malformed arguments:** FastMCP validation/error packaging and whether it enters `track_mcp_call` were not executed.
- **Cancellation packaging:** static Python behavior says cancellation bypasses the tracker; FastMCP’s outer response behavior remains unexecuted.
- **Backend clamps:** several tools pass caller limits directly. A bound is not credited unless the backend implementation is traced/executed.
- **Duplicate bodies/tool names:** manual read found no conflicting module-level duplicates; exact AST hashes/counts remain `NOT RUN`.
- **Telemetry exception classes:** tracker catches only `sqlite3.Error` around store writes; whether current store methods can emit other exception types on these paths was not executed.
- One-line pass-by only: broad query-auth fallback and empty-tier behavior are correctness/security questions, not graded here (`contracts.py:43-74,325-329`).

## 13. Coverage Table

Every scoped file was read from the pinned commit. The line values below are the supplied scope manifest, independently arithmetically reconciled to 70 files / 7,222 lines. Independent filesystem measurement is implemented in the probe but remains `NOT RUN`; the distinction is intentional.

| File | Physical lines | Status |
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
| `src/menhir/mcp/tools/conflict/__init__.py` | 14 | READ |
| `src/menhir/mcp/tools/conflict/list_conflicts.py` | 82 | READ |
| `src/menhir/mcp/tools/conflict/requeue_for_review.py` | 46 | READ |
| `src/menhir/mcp/tools/conflict/resolve_conflict.py` | 269 | READ |
| `src/menhir/mcp/tools/conflict/run_llm_review.py` | 33 | READ |
| `src/menhir/mcp/tools/conflict/scan_conflicts.py` | 37 | READ |
| `src/menhir/mcp/tools/ingest/__init__.py` | 9 | READ |
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

### Checked

- Pinned target commit and audit branch ancestry.
- Every one of the 70 scoped files; no file inherited coverage from a group initializer.
- Supporting context read but not graded as scope: `src/menhir/api/mcp_remote.py`, `src/menhir/core/backend_protocol.py`, and `src/menhir/config/settings.py`.
- All 54 tool registrations and both stdio/remote composition paths.
- One complete write invocation (`add_memory`) from registration through error/warning handling.
- All shared plumbing, every ops endpoint, fan-out candidates, private imports encountered, and `query_structure.py` responsibility ranges.
- Search instrument control; failed search discarded.
- Probe source syntax (`python -m py_compile`) and explicit built-in controls.

### Not verified in this environment

- Running `.agent/audit/m3_architecture_probe.py` against the target checkout.
- Independent filesystem measurement of 70 files / 7,222 lines.
- Exact AST alias-edge counts, SCC output, duplicate body hashes, duplicate tool-name count, and exhaustive outside-private inventory.
- FastMCP malformed-argument and cancellation response behavior.
- Runtime behavior against an unreachable backend/Neo4j/SQLite lock; failure paths are static traces, not executed reproductions.

No withheld functional-correctness report was read.

## 15. Review Confidence

**76/100.** All 70 scoped files and required supporting context were read, and the principal architecture/failure paths are fully traced with file:line evidence. Confidence is held below 80 because the required mechanical probe could not run, line counts are scope-manifest reconciliation rather than independent filesystem measurement, and the outside-private absence sweep cannot be declared exhaustive.
