# Menhir redundancy and reachability triage — 2026-11-20

**Audit target:** [`Archolith/menhir` main at `e997b3c2315b7fafa0982001eddb7d7921c954c9`](https://github.com/Archolith/menhir/commit/e997b3c2315b7fafa0982001eddb7d7921c954c9)  
**Status:** **Targeted, evidence-backed triage, not a full import-graph or executable duplicate-code audit.**  
**Method:** complete GitHub Git-tree metadata; read the relevant production source and manifest through GitHub; targeted symbol/import searches; count physical Python lines for specified candidate files. No local checkout, tests, runtime trace or whole-tree AST scan was available in this review. Distinguish **production-reachable**, **test-/script-only candidates**, **unreachable functions**, and **responsibility overlap**; do not use a filename or lack of one grep hit as proof of dead code.

## Executive assessment

Menhir's size is mostly a product of its test suite, many feature boundaries, and a broad compatibility façade. The strongest **plausible package-size** reduction found here is **5,317 Python lines in 18 lab/research candidate modules** (subject to complete import-graph and dynamic-entrypoint verification). Moving these files out of the installable package would **not** reduce repository lines unless genuinely retired. Separately, [#660](https://github.com/Archolith/menhir/issues/660) previously estimated about **700 lines** of unreachable governance behavior; current symbol checks still find the relevant code, but this review did not independently recompute that overlapping estimate. The largest maintainability hotspot is the deliberate, extensive delegation surface of `MemoryGraphAdapter`.

**Do not quote a repository-wide redundancy percentage from this review.** There is no measured semantic clone scan, all-root import reachability graph, or runtime coverage on this revision.

## Exact Python inventory from Git tree

| Location | Files | Total bytes |
|---|---:|---:|
| `src/` | **581** | 7,336,939 |
| `tests/` | **1,028** | 10,866,798 |
| `scripts/` | **123** | 1,066,594 |
| Other Python (`.agent/`, `deploy/`, `integrations/`, root smoke test) | **12** | — |
| **All Python files** | **1,744** | — |

There are **no identical Python Git blob SHA pairs** in the complete tree. This rules out byte-for-byte copied files on this revision, **not** repeated functions, overlapping subsystems or common templates. The previously quoted ~456K Python lines are estimates and were not recomputed as a full-tree physical LOC measurement here.

## Finding A — staged labs: distinguish live Explorer from script/test-only

An initial filename-driven set contained **22 Python files / 6,369 physical lines**:

- `src/menhir/explorer/extraction_lab*.py` and `shadow_*_lab.py`
- `src/menhir/services/research_scalar*.py`

**Correction to a naïve “all labs are dead” interpretation:** `explorer/app.py` explicitly imports `extraction_lab.ExtractionLabRequest` and `run_extraction_lab` and exposes extraction-lab HTTP routes. `extraction_lab.py` imports the models, prompts and scoring modules. These **four files total 1,052 lines and are demonstrably live through Explorer**:

| Runtime-wired lab file | Physical lines |
|---|---:|
| `explorer/extraction_lab.py` | 382 |
| `explorer/extraction_lab_models.py` | 178 |
| `explorer/extraction_lab_prompts.py` | 231 |
| `explorer/extraction_lab_scoring.py` | 261 |

**Remaining 18 files: 5,317 lines** of stage-/script-/test-only **candidates**, not established delete targets. Search found `extraction_lab_metadata_production` used from scripts/tests; `extraction_lab_context_selection` referenced from a runner and tests; `research_scalar*` modules are interdependent and are covered by tests. Some have intentionally usable scripts, and relocation must update those callers. Existing [#72](https://github.com/Archolith/menhir/issues/72) is the proper tracker, but its dated “~13 / ~2,000 lines” inventory should be refreshed using this newer, broader candidate set **without removing the four live Explorer components**.

**Disposition:** move only confirmed script/test-only files into a separate research package/dir if packaging simplicity is worth the import migration. Do not delete useful experiments merely to optimize LOC.

## Finding B — unreachable or inert governance code

Recent [#660](https://github.com/Archolith/menhir/issues/660) identifies approximately 700 lines of unreachable recall-governance code. The following elements still exist in this snapshot, and targeted symbol search found no production consumers beyond definitions or compatibility exports:

| Candidate | Current code | Evidence status |
|---|---|---|
| `StructureTemporalOracle` | `domain/structure_temporal.py` (170-line module) | Code-search results only source definition, test, docs |
| `MemoryOracleService` | `services/memory_oracle_service.py` (78 lines) | Definition and tests, no observed runtime construction |
| `answer_windowed_count` | `services/windowed_recall.py` (88 lines) | Definition and tests, no observed runtime caller |
| `WeightedOracleCombiner` | `domain/oracle_combiner.py` (specific class, not entire 201-line file) | No observed non-test construction |
| `ExhaustionWarden` | `domain/warden.py` + `domain/exhaustion.py` | Not in the observed default chain; other wardens are live |
| `BeliefScorer` / `currentness_bucket` | `domain/belief.py` (partial 416-line module) | Search finds tests and an export through `domain/__init__.py`—**public API compatibility must be checked** |

**Disposition:** use [#660](https://github.com/Archolith/menhir/issues/660) together with [#566](https://github.com/Archolith/menhir/issues/566) to decide **wire vs retire**, then use a full root/import graph plus public/export compatibility check. Do **not** delete a whole module merely because one class in it is unreachable.

The independent [#656](https://github.com/Archolith/menhir/issues/656) flags a correctness/operational problem: `LifecycleDecayMixin.should_delete` unconditionally returns `False`, while deletion-path scaffolding survives. Re-enable behind a gated evaluation or remove the inert path only with a documented retention decision. This is **more urgent than its line-count savings**.

## Finding C — very large façade, but intentional layers

[`infrastructure/memory_graph_adapter.py`](https://github.com/Archolith/menhir/blob/e997b3c/src/menhir/infrastructure/memory_graph_adapter.py): **2,877 physical lines**. Targeted declaration analysis found:

- **294 class-level methods**
- **252 methods containing `return self._<repository>.<method>(...)`-style forwarding**
- **199** forwarding-style method spans of at most 10 lines, covering about **952 physical lines**, including formatting/blank space
- Delegation fans out to episode, consolidation, query, View, artifact, TODO, structural, temporal, evidence and other repositories.

The top-of-file contract explicitly calls the class a façade retaining the signatures of an earlier monolith. **This is a large compatibility/public API surface, not 2,877 lines of redundant business logic**. Collapsing it casually would likely harm callers and weaken layering. A later compatibility census could identify direct repository consumers and whether facade methods can be generated or retired, with migration and type safety preserved.

Other large source files warrant a **complexity** review, not a dead-code finding:

| File | Physical lines |
|---|---:|
| `services/typed_scalar_rules.py` | 3,138 |
| `infrastructure/memory_queries.py` | 2,777 |
| `infrastructure/structure_queries.py` | 2,739 |
| `services/recall_pipeline.py` | 2,236 |
| `infrastructure/graphiti_extraction_policy.py` | 2,318 |

These five plus the façade account for **16,085 lines**. High concentration alone does not prove duplicated algorithms or dead code.

## Finding D — repeated ownership protocols, without proof of duplicate semantics

[#239](https://github.com/Archolith/menhir/issues/239) documents about eight independently implemented lease / heartbeat mechanisms, including scheduler leases, saga gates and heartbeats, episode ownership, graph ownership and snapshot leases. Shared code might be possible, but merging them **before** verifying CAS tokens, lease loss, cross-process fencing and clock invariants risks introducing safety bugs. Recommended order: invariants table → counterexample tests → merge only proven-equivalent mechanics.

## False positives checked

- `services/view_projection_repair.py` versus `infrastructure/view_projection_repair.py`: service dispatches claims; repository owns durable CAS/leases. **Layering, not a duplicate.**
- `api/auth_mode.py` versus `config/auth_mode.py`: API file is a small compatibility re-export; config owns policy. **Intentional alias.**
- `core/request_context.py` versus `api/request_context.py`: caller/session propagation versus HTTP request-ID middleware. **Distinct responsibilities.**
- `services/legacy_unmerge_coordinator.py` / `domain/legacy_snapshot.py`: names look legacy, but search shows active merge-recoverability/script references. **Do not label dead.**
- [#399](https://github.com/Archolith/menhir/issues/399) already retired multiple measured losing/neutral feature-flag branches. **Do not count already-deleted code as future savings.**

## Suggested execution order

1. **Correctness first (P0/P1):** resolve inert GONE/deletion behavior ([#656](https://github.com/Archolith/menhir/issues/656)) and dead/producers-not-wired oracle paths ([#566](https://github.com/Archolith/menhir/issues/566), [#660](https://github.com/Archolith/menhir/issues/660)).
2. **Package hygiene (P1):** update [#72](https://github.com/Archolith/menhir/issues/72) with an actual root/import graph. Keep the 1,052-line live extraction-lab core; relocate only verified staging modules (up to 5,317 candidate lines).
3. **Full reproducible audit (P1):** on a checked-out revision, scan all Python modules with AST imports, `importlib`/registry/callable-name references and package/public exports. Seed roots from `pyproject.toml`, `menhir.main`, `menhir.api.server`, `menhir.mcp.server`, Explorer, CLI, scheduler, scripts and consumer integrations. Classify **live**, **scripts-only**, **test-only**, **externally supported library**, **unknown dynamic**, **confirmed unreachable**.
4. **Semantic similarity (P1):** run a normalized AST/function-clone scan separately for production and tests; report candidate clone clusters with real examples, not only percentages. Verify each one before consolidating. Test similarity is not automatically redundancy.
5. **Maintainability (P2):** assess façade method callers, deprecation policy and value of generated typed delegation. Audit lease invariants first; consolidate only safe equivalences.

## Measurement contract for a follow-up complete audit

Report **two different numbers**: (a) Python lines safely deletable without changing supported behavior, and (b) lines that can move *out of the production wheel* while remaining in research/scripts/tests. Do not combine them. Every high-confidence dead-code finding needs symbol references, rooted reachability, test coverage and a planned public-API migration or explicit proof of no callers. Require tests and CI before removal.

**Conclusion:** Concrete evidence supports at least a few hundred lines of likely retireable governance paths, up to 5,317 lines of stage-only package-hygiene candidates, and a larger amount of façade/ownership complexity requiring a compatibility and invariant audit. It does **not** support claiming a double-digit percentage of production code can be deleted safely.
