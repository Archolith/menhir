# Menhir M3 — MCP Surface Architecture Audit

**Repository:** `Archolith/menhir`  
**Audit base:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Scope:** exactly `src/menhir/mcp/**/*.py`  
**Status:** DRAFT — updated continuously during the independent pass  
**Audit type:** architecture only: layering, coupling, blast radius, dispatch boundaries, failure modes, observability, and fan-out

## Working method and evidence discipline

- The audit is pinned to the supplied commit rather than moving `main`.
- Existing functional-correctness findings for M3 are deliberately not being consulted.
- Every final claim will carry an exact `file:line` citation.
- Mechanical output will come from `.agent/audit/m3_architecture_probe.py`; any check not executed will be labeled `NOT RUN` with the reason.
- Scope coverage is not inherited from directory listings: each file receives an explicit coverage row only after it is read.
- The local shell cannot resolve GitHub in this environment. Source retrieval is therefore through the authenticated GitHub connector; this limitation does not change the pinned commit.

## 1. Executive Summary

DRAFT — first promoted results:

1. **The per-tool module split is not a runtime isolation boundary.** `server.py` invokes `register_all_tools(mcp)` at module import time; the registry eagerly imports all four tool families, concatenates their class lists, then constructs and registers every class in one unguarded loop (`src/menhir/mcp/server.py:17-19,49`; `src/menhir/mcp/tools/__init__.py:7-15,19-22`). A failing import or registration can therefore prevent the complete MCP surface from starting. The split may still provide source-ownership isolation; that verdict remains under review.
2. **Ordinary invocation failures are contained but flattened into return values.** The shared tracker wraps the runner in `asyncio.wait_for`, catches timeout and ordinary `Exception`, records/logs them, and returns mapped JSON or an `"Error: ..."` string (`src/menhir/mcp/telemetry/tracker.py:56-110`). This avoids process-wide failure but erases protocol-level failure semantics. Cancellation is not caught by those branches and propagates.

The full risk ordering remains DRAFT until all 70 files and the mechanical output are complete.

## 2. Layering Edge Table and violation judgements

DRAFT.

## 3. Blast Radius Register — in-degree per hub, what breaks on change

DRAFT.

## 4. Outward Coupling Register — `mcp/` private symbols used elsewhere

DRAFT.

## 5. Tool Dispatch Architecture — registration through tier check

### Registration path established so far

1. `src/menhir/mcp/server.py` constructs the gateway server and invokes `register_all_tools(mcp)` at import time (`src/menhir/mcp/server.py:31-49`).
2. `src/menhir/mcp/tools/__init__.py` imports four family registries, concatenates them as `ALL_TOOLS`, and iterates them without a per-tool guard (`src/menhir/mcp/tools/__init__.py:7-15,19-22`).
3. Each class is instantiated and its shared `register()` method installs a wrapper preserving the concrete endpoint signature. The complete shared-contract trace is still being reconciled.

**Interim verdict:** per-file tool modules are compile-time/source boundaries, not independent runtime components. One module import, class construction, or registration failure can abort registration of all later tools.

## 6. Failure-Mode Trace — one complete invocation

### Shared containment layer established so far

`track_mcp_call()` executes the invocation under `asyncio.wait_for` (`src/menhir/mcp/telemetry/tracker.py:44-65`). A timeout is converted to a caller-facing string/JSON value after best-effort telemetry (`src/menhir/mcp/telemetry/tracker.py:66-92`); any ordinary `Exception` is diagnosed, logged, recorded, and likewise converted (`src/menhir/mcp/telemetry/tracker.py:93-110`). The success path logs and records completion (`src/menhir/mcp/telemetry/tracker.py:112-136`).

**Interim failure-semantics result:** backend-unreachable and Neo4j exceptions that reach this wrapper are surfaced as normal tool results rather than protocol errors. `asyncio.CancelledError` is not caught by the ordinary-exception branch and propagates. Malformed-argument behavior remains to be traced through FastMCP dispatch.

## 7. Observability Assessment

The shared tracker logs `kind`, `operation`, duration, and diagnosed error on ordinary failures (`src/menhir/mcp/telemetry/tracker.py:93-101`) and records input/result sizes plus a payload preview (`src/menhir/mcp/telemetry/tracker.py:101-109,120-134`). No caller, request/session identifier, or correlation identifier is passed into this layer. This is an interim result pending inspection of the telemetry store and HTTP binding path.

## 8. Fan-Out Register

DRAFT.

## 9. `query_structure.py` Responsibility Decomposition

DRAFT.

## 10. Bug-Class Sweep Results

DRAFT. Required sweeps:

1. Duplicate definitions across files, with function bodies compared.
2. Module-level constants documenting an invariant that no code reads.
3. Cross-module private-symbol imports in both directions.

## 11. Disproved Candidates

DRAFT.

## 12. Open Questions

- **Environment:** whether the connector-reconstructed checkout can be made complete enough to execute the probe locally. Until execution succeeds, mechanical outputs remain `NOT RUN` rather than inferred.
- **Correctness/security, deliberately not pursued here:** `BaseTool.execute` only enforces the tier comparison when `get_request_tier()` returns a truthy value. This belongs to the other passes.

## 13. Coverage Table — all 70 files and line reconciliation

DRAFT. No file is marked read merely because its directory entry was enumerated.

## 14. What Was Checked, and what could not be verified in this environment

### Checked so far

- Target commit identity.
- Repository access and write permission.
- Top-level MCP layout: root shared modules, telemetry, and tool subpackages.
- Eager aggregate registration and the shared telemetry containment path.

### Not yet verified

- Full 70-file line reconciliation.
- Probe outputs.
- Runtime behavior of a complete invocation past the shared wrapper.

## 15. Review Confidence

**Current draft confidence: 15/100.** This remains low until every scope file is read and the mechanical checks execute.
