# Menhir M3 — MCP Surface Architecture Audit

**Repository:** `Archolith/menhir`  
**Target commit:** `eebf6d6dd83f15083167bf847b639d24b953fdc9`  
**Audit branch:** `audit/m3-mcp-architecture-external`  
**Scope:** exactly `src/menhir/mcp/**/*.py`; requested baseline 70 files / 7,222 lines  
**Status:** DRAFT — evidence is being added continuously; rows not yet read remain explicitly pending  
**Audit type:** architecture only (layering, coupling, blast radius, failure modes, observability, fan-out)

## 1. Executive Summary

Work in progress. The first confirmed structural fact is that `src/menhir/mcp/server.py` is a thin composition root: it constructs the gateway server, then delegates tool and resource registration to `register_all_tools(mcp)` and `register_memory_resources(mcp)`. The architectural decision surface therefore sits primarily in package initializers, shared MCP plumbing, and individual tool modules rather than in the server entry point (`src/menhir/mcp/server.py:25-45`).

No issue severity is assigned until the complete import graph, invocation trace, and all-file coverage reconciliation are complete.

## 2. Layering Edge Table and Violation Judgements

Pending mechanical edge enumeration and source-by-source judgement. Counts will distinguish import aliases/edges from distinct importing files.

## 3. Blast Radius Register

Pending measured in-degree for:

- `src/menhir/mcp/contracts.py`
- `src/menhir/mcp/service_access.py`
- `src/menhir/mcp/formatters.py`
- `src/menhir/mcp/resources.py`

## 4. Outward Coupling Register

Pending repository-wide private-symbol import sweep in both directions. An empty result will not be accepted until the instrument is control-tested against a visible private import.

## 5. Tool Dispatch Architecture

### Confirmed composition-root path

1. `server.py` calls `create_gateway_server(...)` and assigns the result to `mcp` (`src/menhir/mcp/server.py:29-42`).
2. `server.py` invokes `register_all_tools(mcp)` (`src/menhir/mcp/server.py:44`).
3. `server.py` invokes `register_memory_resources(mcp)` (`src/menhir/mcp/server.py:45`).
4. For stdio only, `main()` binds explicit local operator trust before `run_server(mcp)` (`src/menhir/mcp/server.py:48-60`).

The registration, decorator/wrapper, tier-check, and backend-dispatch portions remain under trace.

## 6. Failure-Mode Trace

Write-tool selection pending. The final trace will enumerate registration, dispatch, tier enforcement, service access, backend call, exception conversion, response construction, cancellation, malformed input, backend-unreachable, timeout, and Neo4j-down behavior.

## 7. Observability Assessment

Pending the complete invocation trace. The final assessment will record log level, message fields, caller identity, tool identity, stage identity, and correlation identifier at each transition.

## 8. Fan-Out Register

Pending. Candidates will be admitted only with exact call paths and bounds/absence of bounds.

## 9. `query_structure.py` Responsibility Decomposition

Pending full-file read and line-range decomposition.

## 10. Bug-Class Sweep Results

### 10.1 Duplicate definitions across files, body comparison

**Status:** NOT RUN yet. Probe implementation in progress.

### 10.2 Module-level constants documenting an invariant that nothing reads

**Status:** NOT RUN yet. Probe implementation in progress.

### 10.3 Cross-module private-symbol imports in both directions

**Status:** NOT RUN yet. Probe implementation in progress.

## 11. Disproved Candidates

None recorded yet. Candidates will be added only after a complete disproof with exact evidence.

## 12. Open Questions

- **Environment:** direct `git clone https://github.com/Archolith/menhir.git` failed because the execution container could not resolve `github.com`. Source reading is proceeding through the authenticated GitHub connector at the exact commit. Unless an executable checkout can be materialized, probe output will be reported as `NOT RUN` rather than fabricated.

## 13. Coverage Table

Legend: `READ` means the complete file was read at the target commit; `PENDING` means no coverage is claimed.

| File | Measured lines | Status |
|---|---:|---|
| `src/menhir/mcp/server.py` | pending reconciliation | READ |
| All other in-scope files | pending reconciliation | PENDING |

The complete 70-row table and measured line-total reconciliation will replace this provisional table.

## 14. What Was Checked, and What Could Not Be Verified in This Environment

Checked so far:

- Target commit exists and resolves to the requested SHA.
- Audit branch is based directly on the target commit.
- The branch initially contained only this report stub; no withheld functional-correctness report was read.
- `src/menhir/mcp/server.py` was read completely.
- Scope directory structure was enumerated through GitHub at the target commit.

Not yet verified:

- Complete 70-file read.
- Mechanical probe execution against a clean checkout.
- Runtime failure behavior.
- FastMCP/framework behavior outside the repository code.

## 15. Review Confidence

**Current draft confidence: 8/100.** This is intentionally low while most scope files remain unread and all quantitative analyses remain incomplete.
