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

DRAFT. No structural verdict has been promoted yet.

## 2. Layering Edge Table and violation judgements

DRAFT.

## 3. Blast Radius Register — in-degree per hub, what breaks on change

DRAFT.

## 4. Outward Coupling Register — `mcp/` private symbols used elsewhere

DRAFT.

## 5. Tool Dispatch Architecture — registration through tier check

DRAFT.

## 6. Failure-Mode Trace — one complete invocation

DRAFT.

## 7. Observability Assessment

DRAFT.

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

## 13. Coverage Table — all 70 files and line reconciliation

DRAFT. No file is marked read merely because its directory entry was enumerated.

## 14. What Was Checked, and what could not be verified in this environment

### Checked so far

- Target commit identity.
- Repository access and write permission.
- Top-level MCP layout: root shared modules, telemetry, and tool subpackages.

### Not yet verified

- Full 70-file line reconciliation.
- Probe outputs.
- Runtime behavior of a complete invocation.

## 15. Review Confidence

**Current draft confidence: 5/100.** This is intentionally low until every scope file is read and the mechanical checks execute.
