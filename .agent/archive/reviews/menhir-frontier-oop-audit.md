# Menhir Frontier OOP Discipline Audit

## Audit Anchor

- Worktree: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier`
- Branch: `claude/menhir-chain-handoff-doc-7iuat2`
- HEAD: `e42e6053efd0466fa094ecea0f01f96ff1d843ce`
- Source/test freeze: `2026-07-04T03:05:49.1016668Z`
- Scope fingerprint: 205 tracked Python source/test files; aggregate SHA-256 `5c27304d717eac059b72bd166da3c9d9a12b9055fb01474d11f842a3a6b4ec8c`
- Dirty source included: `src/menhir/domain/fold_algebra.py`, `src/menhir/services/perception.py`, `tests/test_fold_algebra.py`, and `tests/test_perception_generalization.py`.
- Method: OOP pillar from `C:\Users\you\IdeaProjects\.agent\audit\ai-code-quality-guardrails`, plus the adjacent god-file/SRP checks. Performance, security, readability, SSOT, and AI-process pillars were **NOT RUN** except where direct evidence was necessary to explain an OOP boundary.

The tracked source/test fingerprint was unchanged between the opening and validation checks. Unrelated untracked design documents appeared during the review, but no source/test file changed. This audit made no source, test, or git-state changes.

## Executive Summary

Menhir has sound object-oriented building blocks, but its application and infrastructure boundaries have accumulated six concentrated OOP problems. The highest-value changes are not “make everything a class.” They are:

1. Split the 66-method backend protocol into capability-oriented interfaces and introduce request value objects for wide commands.
2. Decompose the 509-line `RecallService.recall()` transaction script into explicit pipeline collaborators while retaining one orchestration facade.
3. Separate structural graph writes from structural graph reads.
4. Replace `EpisodeRepository`'s code-reuse multiple inheritance with explicit composition.
5. Give scheduler startup and one-shot execution a public controller API instead of reaching into private members.
6. Split lifecycle policy areas behind the existing `LifecycleService` facade.

These are current design/maintainability defects, not evidence that the corresponding runtime behavior is presently broken. Targeted backend, candidate, structure, and recall tests passed. The cost is change amplification, fragile implicit contracts, and difficulty protecting invariants.

Raw OOP checklist result: **3 PASS / 5 FAIL**. The guardrails rubric therefore labels the OOP pillar **REWRITE**. The engineering disposition is **FIX-FIRST, localized**: the failures cluster in a small number of boundary/orchestration classes, so a wholesale project rewrite is neither justified nor recommended.

## Measured Object Map

AST analysis of `src/menhir` found:

- 287 classes
- 21 direct `Protocol` definitions
- 119 dataclasses
- 946 directly declared class methods

Largest OOP-relevant classes:

| Class | Location | Span | Direct methods | Assessment |
|---|---|---:|---:|---|
| `StructureGraphWriter` | `src/menhir/infrastructure/structure_queries.py:34-1418` | 1,385 lines | 31 | Mixed write/query responsibilities; finding OOP-03 |
| `RecallService` | `src/menhir/services/recall_service.py:201-1260` | 1,060 lines | 13 | Multi-stage transaction script; finding OOP-02 |
| `RuntimeProvider` | `src/menhir/core/backend_impl.py:136-1007` | 872 lines | 70 | Fat protocol implementation; finding OOP-01 |
| `LifecycleService` | `src/menhir/services/lifecycle_service.py:75-933` | 859 lines | 16 | Several lifecycle policy areas; finding OOP-06 |
| `BackendClient` | `src/menhir/core/backend_impl.py:1010-1611` | 602 lines | 71 | Fat protocol transport mirror; finding OOP-01 |
| `MemoryBackend` | `src/menhir/core/backend_protocol.py:44-550` | 507 lines | 66 | Interface-segregation failure; finding OOP-01 |
| `MaintenanceScheduler` | `src/menhir/services/maintenance_scheduler.py:50-401` | 352 lines | 26 | Internals exposed across modules; finding OOP-05 |

Large cohesive classes were not automatically reported. `McpTelemetryStore`, `GraphitiClient`, and `MemoryGraphAdapter` remain watch-list items: each is large, but the first two have defensible adapter/store boundaries and `MemoryGraphAdapter` explicitly composes repositories rather than inheriting them.

## OOP Checklist

| OOP discipline item | Result | Evidence and rationale |
|---|---|---|
| Domain models have behavior | PASS | Menhir distinguishes immutable data records from behavioral policy objects. `BeliefScorer`, oracle combiners, wardens, `WardenChain`, and `ViewKind` implementations own behavior. Frozen result/trace/request records are legitimate values, not failed entities. |
| No god classes | FAIL | `RecallService`, `LifecycleService`, `StructureGraphWriter`, and the backend implementations exceed 500 lines and combine independently changing responsibilities. See OOP-01/02/03/06. |
| Services are thin orchestrators | FAIL | `RecallService.recall()` is 509 lines and performs candidate generation, filtering, policy, enrichment, ranking, trace construction, and mutations. `LifecycleService` directly owns several distinct policy families. |
| Inheritance models subtyping | FAIL | `EpisodeRepository` inherits three repository implementations to assemble methods, not because it is substitutable for three semantic parent types. See OOP-04. |
| No domain logic in static utility classes | PASS | No `*Utils`, `*Helper`, or `*Manager` domain dumping ground was found. Pure fold/artifact functions are named domain algebra and are appropriately functional. |
| Value objects for value concepts | PASS (qualified) | The codebase uses enums and frozen dataclasses extensively for scopes, sessions, temporals, oracles, wardens, traces, and tuning. Wide backend commands still need request objects; that exception is included in OOP-01. |
| Composition over inheritance | FAIL | Most of the codebase composes collaborators correctly, but `EpisodeRepository` is an explicit code-reuse multiple-inheritance exception. See OOP-04. |
| Encapsulation boundaries respected | FAIL | Runtime startup and tests reach into `MaintenanceScheduler._jobs`, `_run_job`, and `_make_refresh_structure_graphs`; backend code imports private runtime startup. See OOP-05. |

## Findings

### OOP-01 — The backend abstraction is a god interface and transport mirror

- Severity: **High**
- Catalog: O2 (God Classes), O3 (Transaction-Script Services); SOLID ISP/DIP.
- Evidence:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\core\backend_protocol.py:44-550` declares one 66-method protocol spanning ingest, recall, structure, conflicts, scheduler control, telemetry, TODOs, temporals, candidates, and destructive operations.
  - `...\src\menhir\core\backend_impl.py:136-1007` implements that surface in the 70-method `RuntimeProvider`; `backend_impl.py:1010-1611` mirrors it in the 71-method HTTP `BackendClient`.
  - `backend_protocol.py:507-528` represents candidate creation as 13 keyword fields. The same shape is manually repeated through both implementations, `memory_graph_adapter.py:799-830`, and `candidate_repository.py:49-153`.
- Why it matters: one contract addition requires synchronized edits across protocol, runtime adapter, transport adapter, serialization, routes, tools, test doubles, and expected calls. Consumers typed as `MemoryBackend` receive destructive scheduler/namespace/candidate authority even when they need only recall.
- Current consequence: the full audit reproduced stale mocks/expectations after `include_superseded`, `occurred_at`, and temporal exports evolved. The 25 backend round-trip tests pass, proving the mirror currently works but not reducing its propagation cost.
- Strongest rebuttal: MCP tools benefit from one discoverable backend and both production implementations genuinely implement it. A single facade also hides in-process versus HTTP transport.
- Disposition: **CONFIRMED design defect**. Preserve a facade if useful, but define narrow `RecallBackend`, `IngestBackend`, `StructureBackend`, `OperationsBackend`, and review/governance capabilities. Use command/query value objects such as `CreateCandidateRequest` and `RecallRequest` at the protocol boundary; generate or centralize HTTP serialization instead of copying signatures.

### OOP-02 — `RecallService` is an orchestration object that also implements the pipeline

- Severity: **High**
- Catalog: O2, O3.
- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\recall_service.py:201-1260` is a 1,060-line class. `recall_service.py:752-1260` is a 509-line, 17-parameter method that handles pending waits, candidate generation, structural/file injection, metadata and temporal loading, membership gates, adjacency, scoring, frontier/oracle behavior, post-recall writes, and trace assembly.
- Why it matters: changing one stage requires understanding ordering and mutable dictionaries spanning the entire method. Feature flags and failure behavior compose implicitly through local variables rather than an explicit pipeline state contract.
- Test evidence: three selected recall behavior tests passed (`touches_retrieved_nodes`, edge reinforcement, and shadow failure isolation); 47 tests were deselected. The broader same-snapshot audit found extensive recall coverage.
- Strongest rebuttal: the single method makes execution order visible, avoids a framework of one-use abstractions, and has substantial unit coverage.
- Disposition: **CONFIRMED design defect**, but do not create one class per code block. Introduce a `RecallRequest` value object and a small explicit pipeline context/result. Extract only stable responsibility boundaries: candidate collection, eligibility/evidence hydration, ranking/policy, and post-read effects. Keep `RecallService` as the transaction/orchestration owner.

### OOP-03 — `StructureGraphWriter` owns both command and query models

- Severity: **High**
- Catalog: O2/G1 (Accumulator), SRP/CQS.
- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\infrastructure\structure_queries.py:34-1418` defines a 1,385-line class. Writes begin at `structure_queries.py:41-331`; the class then switches to twelve public `query_*` families at `structure_queries.py:332-1088` before returning to write helpers.
- Why it matters: graph ingestion schema changes and read/query feature changes collide in one class/file and one test fixture. The name `Writer` is false for most of its public surface.
- Test evidence: all 29 `tests/test_structure_queries.py` tests passed. This confirms behavior and makes a reader/writer split safer; it does not establish single responsibility.
- Strongest rebuttal: both sides share the structural graph schema, Neo4j dependency, and label/property vocabulary; splitting can duplicate Cypher helpers.
- Disposition: **CONFIRMED design defect**. Split `StructureGraphWriter` and `StructureGraphReader`/`StructureQueryRepository`, with a small shared schema/query-fragment module. Preserve a compatibility facade only where callers need one object.

### OOP-04 — `EpisodeRepository` uses inheritance as assembly

- Severity: **Medium**
- Catalog: O4 (Misplaced Inheritance); composition-over-inheritance checklist.
- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\infrastructure\episode_repository.py:24-32` calls itself a compatibility facade but inherits `EpisodeLifecycleRepository`, `EpisodeMaintenanceRepository`, and `EpisodeStampingRepository`. Those parents independently assume an undeclared `self.neo4j` collaborator, for example `episode_lifecycle.py:39-59`, `episode_maintenance.py:15-47`, and `episode_stamping.py:20-82`.
- Why it matters: the parent classes are behavior mixins in practice but concrete repository types in name and typing. Their real constructor/dependency contract exists only in the child. A parent method can add state or initialization and silently break the facade's method resolution/initialization contract.
- Test counter-evidence: `MemoryGraphAdapter` constructs the facade at `memory_graph_adapter.py:72-74`, and repository behavior is exercised indirectly. No direct test was found that each parent is independently constructible or that the multiple-inheritance contract is intentional.
- Strongest rebuttal: the facade is only eight lines, all parents use the same Neo4j dependency, and multiple inheritance avoids 26+ trivial delegation methods.
- Disposition: **CONFIRMED design defect**. Prefer three explicitly constructed components behind an `EpisodeRepository` facade. If delegation volume is unacceptable, define dependency-aware mixins deliberately (`Protocol`/base declaring `neo4j`) and rename them as mixins; do not present concrete repositories with hidden construction requirements.

### OOP-05 — Scheduler lifecycle crosses private object boundaries

- Severity: **Medium**
- Catalog: encapsulation boundary failure; O2 ownership spill.
- Evidence:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\core\runtime.py:161-169` reads `scheduler._jobs` and calls `_run_job` and `_make_refresh_structure_graphs` to perform initial work.
  - `...\src\menhir\core\backend_impl.py:709-715` imports private `_start_scheduler` from the runtime module when the provider needs takeover behavior.
  - `tests/test_services_pipeline.py:2157,2292,2368,2525,2599-2644` and `tests/test_structure_watcher.py:249-266` also assert scheduler internals directly.
- Why it matters: runtime, backend, and tests depend on scheduler representation rather than its public lifecycle contract. Renaming a job, changing storage, or separating scheduling from execution becomes a cross-module migration. It also obscures which object owns startup and one-shot execution.
- Test limitation: `tests/test_structure_watcher.py` did not complete within 60 seconds in this audit, and the wider combined selection timed out. No failure assertion was produced, so the timeout is recorded as **NOT VERIFIED**, not an OOP finding.
- Strongest rebuttal: the accesses are package-internal, job names are intentionally observable, and direct state assertions make scheduler tests precise.
- Disposition: **CONFIRMED design defect**. Add public methods such as `run_job_now(name)`, `registered_jobs()`, and a runtime-owned `SchedulerController.ensure_started()`. Test those contracts; keep `_JobState` private.

### OOP-06 — `LifecycleService` combines several policy families

- Severity: **Medium**
- Catalog: O2, O3.
- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\lifecycle_service.py:75-933` is an 859-line class that owns session consolidation (`94-389`), orphan recovery, compression/deletion policy (`390-676`), rehydration, conflict scanning/confirmation (`677-857`), and stale conflict resolution/suppression (`858-933`).
- Why it matters: retention/decay policy, graph summarization, rehydration, and conflict governance have different reasons to change and different failure semantics. They share one dependency set and test surface.
- Strongest rebuttal: all operations are memory lifecycle transitions, and a single service provides a coherent high-level API to runtime/scheduler callers.
- Disposition: **CONFIRMED design defect at current size**. Keep `LifecycleService` as facade/coordinator, but delegate to `ConsolidationPolicy`, `RetentionPolicy`, `RehydrationService`, and `ConflictLifecycle` only where the existing method clusters already define stable seams.

## Positive OOP Evidence

- `src/menhir/domain/warden.py:94-320`: a small structural `Warden` protocol, focused implementations, and `WardenChain` composition provide genuine substitutability.
- `src/menhir/domain/oracles.py:113-128` plus `services/retrieval_oracles.py`: focused oracle interfaces and implementations model policy variability without a deep inheritance tree.
- `src/menhir/infrastructure/view_repository.py:56-179`: `ViewKind`, `CounterKind`, and `TimelineKind` use inheritance for real behavioral subtyping.
- `src/menhir/mcp/contracts.py:87-263`: base resource/tool classes have many concrete subclasses and centralize common contracts/tier enforcement; the abstraction earns its existence.
- `src/menhir/infrastructure/memory_graph_adapter.py:63-84`: despite its large facade surface, it explicitly composes focused repositories. This is the pattern `EpisodeRepository` should follow.
- Frozen dataclasses and pure domain functions are used extensively. They are idiomatic Python value/functional design, not automatically anemic models.

## Where OOP Should Not Be Added

- `src/menhir/domain/fold_algebra.py`: deterministic fold operations are clearer as pure functions over immutable events. Add value types only when they protect a real invariant; do not introduce reducer class hierarchies.
- `src/menhir/services/perception.py`: extraction/gating is currently bench-first, production-unwired code. First settle its runtime boundary; class decomposition would not solve reachability or model-evaluation risk.
- `src/menhir/services/scoring_service.py` and oracle combiners: mathematical transformations benefit from pure, explicit functions/objects with no hidden state.
- Result/trace DTOs in `domain/recall.py`, `domain/retrieval_trace.py`, and similar modules: immutable data carriers are appropriate. Moving orchestration behavior into them would couple transport records to services.
- Cypher constants/query fragments: do not wrap each query in a class. Split repositories by responsibility and keep query text close to the owning operation.

## Verification

Executed from `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_backend_roundtrip.py -q -p no:cacheprovider
# exit 0: 25 passed, 3 warnings in 1.12s

.\.venv\Scripts\python.exe -m pytest tests/test_candidate_repository.py tests/test_add_candidate_tool.py -q -p no:cacheprovider
# exit 0: 11 passed, 2 warnings in 0.21s

.\.venv\Scripts\python.exe -m pytest tests/test_structure_queries.py -q -p no:cacheprovider
# exit 0: 29 passed, 3 warnings in 0.06s

.\.venv\Scripts\python.exe -m pytest tests/test_recall_service.py -q -p no:cacheprovider -k "touches_retrieved_nodes or increments_edge_weights_on_traversal or shadow_failure_never_breaks_recall or returns_empty_when_no_candidates"
# exit 0: 3 passed, 47 deselected, 2 warnings in 0.17s

.\.venv\Scripts\python.exe -m pytest tests/test_structure_watcher.py -q -p no:cacheprovider
# NOT COMPLETED: timed out at 60.2s without a reported failing assertion
```

The initial broad and parallel selections also timed out and are not treated as pass/fail evidence. Online Neo4j/Graphiti/provider tests, mutation testing, type checking, and runtime profiling were **NOT RUN**. AST metrics and structural graph output are supporting evidence; every finding was re-opened in current source and checked against relevant tests/call paths.

## Recommended Decision

**FIX-FIRST, localized.** Do not launch a project-wide “make it more OOP” refactor. The best return comes from capability-oriented backend contracts, explicit request values, thin orchestration around recall/lifecycle, reader/writer separation, deliberate repository composition, and a public scheduler-control boundary. Preserve the functional core and immutable data model where they already make behavior easier to verify.
