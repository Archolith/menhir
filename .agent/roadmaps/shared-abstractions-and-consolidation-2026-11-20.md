# Roadmap 2 — Shared Abstractions & Menhir Consolidation
**Date:** 2026-11-20  
**Baseline:** `Archolith/menhir` main `017b07cc4fc8e6507b441963d8910879744bc5c4` (code assessment; documentation-only audit commit)  
**Purpose:** Consolidate the *mechanisms* that persist, update, invalidate, recover and expose derived state, while keeping domain-specific semantics in their owning modules. This is a **second roadmap** beside the cross-project [Experience & Identity P0 roadmap #718](https://github.com/Archolith/menhir/issues/718); it is not a rewrite of that roadmap or a substitute for existing implementation trackers.

## Why this roadmap

The existing code has real generic primitives, but they are not a complete, production-wired extensibility platform. Repeating orchestration across scalar, counting, experience, context and identity would add duplication. Reducing raw lines alone is not the objective: reduce the number of *independent lifecycle implementations* that must be understood and tested.

The primary implementation owners already exist:
- [#576](https://github.com/Archolith/menhir/issues/576) — derived-View contracts, evidence, materialization and lifecycle
- [#725](https://github.com/Archolith/menhir/issues/725) — extensibility and remote library API
- [#726](https://github.com/Archolith/menhir/issues/726) — duplicate/reachability audit and quantified impact
- [#403](https://github.com/Archolith/menhir/issues/403) — corrupt pending-queue work
- [#239](https://github.com/Archolith/menhir/issues/239) — lease/heartbeat invariant audit
- [Context #13](https://github.com/Archolith/archolith-context/issues/13), [Identity #1](https://github.com/Archolith/identity/issues/1) — prospective consumers

**Do not create substitute implementations of #576/#725.** This roadmap coordinates prerequisites, rollout and consolidation.

## Current-state scorecard (source-verified, not percentage complete)

| Layer | Status | Actual code / evidence | Missing or caveat |
|---|---|---|---|
| Shared View storage and kind semantics | **SHIPPED for built-in kinds** | `infrastructure/view_models.py` `ViewKind`; `view_write_repository.py` single writer for built-in counter/timeline/scalar kinds; versioning and provenance; `tests/infrastructure/test_view_kind_registration.py` | Runtime-wide external registration not threaded through all repository constructors; special scalar writer/read paths still exist. |
| Domain-neutral projection contract | **IMPLEMENTED, LIBRARY-LEVEL** | `domain/projection.py`: `ProjectionDefinition`, `ProjectionRegistry`, `ProjectionTarget`, materialize/abstain/retire, `evaluate_projection`; test `tests/domain/test_projection_registry.py` | No external package executes registered definitions end to end; `DerivedProjectionDefinition`, `EvidenceRef`, `DerivedProjectionCandidate` from #576 remain proposed, not checked in. |
| Definition generations, fencing and freshness | **IMPLEMENTED, NOT GENERALLY ACTIVATED** | `domain/projection_lifecycle.py`, `infrastructure/projection_lifecycle_repository*.py`, transaction-scoped `commit` with freshness certificates. Scoped pending reads and corrupt-generation quarantine landed in #730 and #731. | No runtime worker drains those queues. Unscoped pending remains a diagnostic read. |
| Scalar adapter into generic contract | **PROTOTYPED/TESTED** | `services/scalar_projection_definition.py` and `services/scalar_projection_reconciliation.py`; `infrastructure/scalar_projection_materializer.py` | `ScalarProjectionReconciler` has no observed production constructor/caller in `src` or `scripts`; live `ScalarStateService` continues its specialized fold/rebuild path. |
| Generic View repair receipts | **IMPLEMENTED, LIMITED WIRING** | `services/view_projection_repair.py`, `infrastructure/view_projection_repair.py` with token, lease, fence and conditional completion tests | Repair repository explicitly not registered as independently leased always-on worker; handler semantics are scalar-specific today. |
| Multi-consumer extension registration | **NOT BUILT** | `view_kind_registry.py` resolves injected *instance-local* kinds | No single runtime-wide extension factory/dispatcher, `menhir.extensions` registration, missing-extension behavior or cross-process extension execution (#725). |
| Derived evidence dependency / erasure propagation | **PARTIAL, EXISTING VIEW SAFETY ONLY** | Current View provenance and erasure/fence work exist | General versioned dependency DAG, bounded invalidation, cycle detection, immutable revisions and safe current pointers for **arbitrary** derived views not delivered (#576). |
| Library API | **EXISTING LARGE INTERNAL BACKEND, NOT PUBLIC SDK** | `core/backend_protocol.py` and HTTP `core/backend_client.py` | ~90-method internal `MemoryBackend`, full server dependencies and dotenv side effects; curated independently installable client, `py.typed` and stability policy absent (#725). |
| Horizontal infrastructure consolidation | **RESEARCH ONLY** | `MemoryGraphAdapter` and multiple lease mechanisms; [reachability triage](https://github.com/Archolith/menhir/blob/main/.agent/reviews/menhir-redundancy-reachability-triage-2026-11-20.md) | Do not flatten distinct safety boundaries based on line count; establish clone/ownership evidence first (#726/#239). |

**Status vocabulary:** SHIPPED = exercised by existing production route; IMPLEMENTED = code and tests present but not all runtime paths wired; PROPOSED = issue/doc design only. Existing test files are evidence of intended behavior, not proof of latest green CI.

## Dependency map

```text
A0 freeze production baseline and extension decisions (D1, D6 especially)
  ├── A1 define neutral evidence-backed derived projection contracts
  └── A2 repair/scoped pending queue (#403) and lifecycle invariants
      └── A3 generic dispatcher + registry + execution worker (#725)
          └── A4 source dependency/erasure/replay safety (#576)
              └── A5 two distinct consumer canaries
                  ├── A6 incrementally converge existing scalar/counting implementations
                  └── A7 release a deliberately small library/extension API
A8 horizontal code consolidation (#726, #239) runs independently after invariants
```

The first **go/no-go point** is an externally authored projection that can register, execute, survive restart, respect tenancy and erasure, and *not* corrupt other definitions' pending work.

## Phased work and gates

### A0 — Inventory, decisions and compatibility baseline (P0 prerequisite; no behavior change)
**Owners:** #725 phase 0, #576 M0, #726.

- [x] Inventory the projection callers and the five runtime adapter construction sites. See the Phase 0 census below.
- [x] Record the owner's D1 and D6 approvals in [#725](https://github.com/Archolith/menhir/issues/725). Hosts explicitly register trusted in-process extensions. The initial SDK uses the remote `BackendClient`.
- [x] Record D2 and D5 in [#725](https://github.com/Archolith/menhir/issues/725). Menhir runs one scheduler lane per registered definition. Extensions explicitly migrate or retire older work. Without either action, that work stays pending for review.
- [ ] Resolve D3, D4, and D7 for schemas, compatibility, and packaging.
- [ ] Freeze comparable scalar-state, scalar-history, counting, general recall, namespace erasure and View write snapshots. Add one end-to-end public-example baseline with an *unknown extension* gracefully absent.
- [ ] Keep #726 responsible for measuring physical/source/research/wheel LOC and repeated-semantic-mechanism count.

**Exit:** decisions recorded, tests/bench baselines reproducible, operator-facing modes enumerated.

#### Phase 0 census

Checked against `main` at `4794b41f`. Source searches covered constructor references, projection queue methods, and the `ProjectionWorkState` and `ProjectionDefinitionState` labels in `src/menhir` and `scripts`.

| Path | Current behavior |
|---|---|
| [Projection registry](../../src/menhir/domain/projection.py) | The registry indexes definitions and creates a new registry through `with_definition`. No runtime entry point constructs and supplies one to a worker. |
| [Production scalar state](../../src/menhir/infrastructure/memory_graph_adapter.py) | The adapter constructs `ScalarStateService` over its typed assertions and built-in View operations. This is the existing runtime fold path. |
| [Projection queue](../../src/menhir/infrastructure/projection_lifecycle_repository_writes.py) and [scalar reconciler](../../src/menhir/services/scalar_projection_reconciliation.py) | The repository implements definition publication and work writes. The scalar reconciler can dirty assertions, drain one definition, and commit materializations. No runtime or operator script constructs the reconciler or wires a producer to those queue writes. The [realization coverage adapter](../../src/menhir/infrastructure/realization_coverage_repository.py) wraps the repository for reads. |
| [Scheduled View repair](../../src/menhir/services/scheduler_tasks.py) | The scalar consolidation lane calls `MemoryGraphAdapter.run_view_projection_repairs` for configured namespaces. The adapter builds a repair service with a scalar rebuilder. This path consumes View repair receipts. |
| [View repository](../../src/menhir/infrastructure/view_repository.py) | The adapter owns a `ViewRepository`. The [prototype scalar materializer](../../src/menhir/infrastructure/scalar_projection_materializer.py) also constructs one over its transaction. The [kind resolver](../../src/menhir/infrastructure/view_kind_registry.py) can resolve instance-local injected kinds. |

The runtime adapter is constructed in five places:

1. [Core bootstrap](../../src/menhir/core/bootstrap.py) in `build_memory_services`, used by the regular MCP runtime and CLI callers.
2. [Candidate read-only runtime](../../src/menhir/core/runtime.py) in `_initialize_services`.
3. [HTTP server lifespan](../../src/menhir/api/server_support.py) in `build_runtime_lifespan`.
4. [CLI hooks](../../src/menhir/cli/bootstrap.py) in `build_hook_services`.
5. [Standalone Explorer](../../src/menhir/explorer/app.py) in `create_app`.

The LME repair, fold replay, and Hook Center smoke scripts also construct adapters. Runtime wiring must account for the five product constructors and explicitly decide the behavior of those operator and test tools. A separately installed extension still needs a host registration path, an authorized materializer and worker, and evidence that namespace scope and erasure hold across restart.

The focused offline baseline passed 62 tests and skipped 11 graph-backed cases. It covered projection registry and lifecycle contracts, View kind registration, scalar materialization and reconciliation, pending queue reads, and View repair. Full built-in behavior snapshots and no-extension startup parity are still part of A0.

### A1 — Complete the semantic contract without inventing another persistence stack (P1)
**Owner:** #576 M1.

- [ ] Define typed `EvidenceRef`, `DerivedProjectionCandidate`, namespace/agent/actor scoping, provenance and counterevidence, source-fingerprint receipt and explicit uncertainty/abstention.
- [ ] Make the `ProjectionDefinition`/ViewKind relationship explicit: **definition = inputs/fold/target**, **ViewKind = serialized output/write/read shape**, **runtime registration = binds both to execution**.
- [ ] Keep LLM output as an untrusted *candidate*; deterministic/approved interpretation can be materialized without promoting it to factual authority.
- [ ] Provide one external non-scalar example compiled/tested against public contracts (e.g., synthetic evidence-based timeline or research observation, no live capture).

**Exit:** outside package expresses its projection without importing scalar-specific internals; no semantic drift in built-ins.

### A2 — Make the shared lifecycle queue safe for other families (P0 gate before scheduling)
**Owners:** #403, #725 phase 1, #576 M2.

- [x] Scope pending reads before the limit and have scalar reconciliation use that API. [#730](https://github.com/Archolith/menhir/pull/730)
- [x] Quarantine corrupt generations on scoped reads and keep freshness checks fail-closed. [#731](https://github.com/Archolith/menhir/pull/731)
- [x] Keep older pending generations for review at version publication. Ordinary target writes cannot replace them and workers cannot certify them.
- [x] Add internal operations for an extension to migrate or retire an exact retained generation under #725 D5. The current worker still materializes and certifies the new generation.
- [ ] Prove multi-process fencing, generation replacement, absent-current targets, retry, crash mid-commit and namespace deletion behavior with **online Neo4j counterexamples**.
- [ ] Separate publishing a definition from allowing a worker to execute it.

**Exit:** foreign work and corrupt first page cannot block a healthy definition, no stale worker can commit, a host lacking an extension does not certify its work.

### A3 — One runtime extension/dispatched materializer path (P1 critical path)
**Owner:** #725 phases 2–4 + #576 M3.

- [x] Introduce an [internal reconciler](../../src/menhir/services/projection_reconciliation.py) that binds a `ProjectionDefinition` and version to its host-supplied materializer. The scalar reconciliation prototype uses it. Recovery filters by definition and version before the batch limit, and certification uses the existing transactional fence.
- [x] Centralize product graph adapter and View repository construction. HTTP, hook and Explorer can share a validated registry. A source-level test guards the construction paths.
- [x] Add one [maintenance lane](../../src/menhir/services/maintenance_scheduler.py) per explicitly registered projection definition and version. Each lane has a separate lease and loop, runs every 30 seconds, and takes up to 100 items per batch. An empty registration set adds no jobs.
- [x] Test empty registration, two definitions in one scheduler, a restart with newly registered work, and lease takeover during a batch.
- [ ] Test extension upgrades and removal across concurrent hosts and verify tenant-scope isolation and erasure for external materializers.

**Exit:** a separately installed extension registers **and executes** without patching Menhir; all S1–S7 safety invariants from #725 pass.

An HTTP host can pass explicit extensions to `create_app(extensions=...)` or supply a prevalidated `RuntimeExtensions` registry. A host with standalone hook or Explorer surfaces can supply that same registry to `build_hook_services(extensions=...)` and Explorer's `create_app(extensions=...)`. The first graph assembly binds the registry for the life of the process, including when it starts with built-in kinds only. Later surfaces inherit that registry and reject a conflicting one. Build the registry before the first graph assembly and restart the process to select a different one. The shipped MCP stdio process proxies the HTTP backend and opens no graph of its own.

Extensions supply synchronous materializers and View read fields. Authors scope every materializer read and write to the work token's target and namespace, and apply the host's caller policy before queuing work. Extension Views use kind-scoped storage keys. Built-in keys retain their existing format.

The [decision timeline example](../../examples/decision_timeline.py) accepts synthetic dated decisions with source references. Its explicit producer queues a rebuild in the same transaction as a source update. Source erasure removes the selected decision and the project's historical timeline Views in that namespace, then queues a rebuild. The live test boots the HTTP runtime, checks correction and erasure in one namespace without changing another, and verifies that work queued while stopped drains after restart. This example uses an operator-only View and an extension-owned source-erasure operation. Menhir-wide namespace erasure for arbitrary extension data and the public extension conformance kit remain open.

Projection leases are separate from the general maintenance lease and include the installed definition version. A backend holding the general lock cannot block an installed projection on another backend sharing the lease store. Each lane starts one transaction at a time with a 30-second Neo4j transaction timeout per attempt. Neo4j checks revocation before statements, transaction attempts, and retries, and an already-dispatched transaction can finish under the work-generation fence after lease loss. An orderly stop of an owned lane waits for its in-flight callback. Deployments with separate lease stores still rely on the durable Neo4j generation fence.

Version publication retains the exact outgoing pending generation and requeues settled targets. Versionless diagnostic reads can see valid retained work, and freshness reports it as stale with `version_review_required`. The internal lifecycle repository can migrate that generation in place, preserving target identity and membership, or queue removal of that target. Decisions require the shared-current definition and an exact eligible retained token. A regular scoped worker finishes the new generation. Host-facing upgrade orchestration and mixed-version rollout verification remain open.

### A4 — Derived provenance, dependency invalidation and erasure (P0 correctness prerequisite)
**Owner:** #576 M2–M4.

- [ ] Immutable revisions/current-pointer semantics with complete contributor and counterevidence lineage.
- [ ] Bounded dependency traversal, cycle detection, correction/supersession/erasure propagation, and deterministic replay from admitted versioned interpretation receipts.
- [ ] Partial/missing evidence must never be rendered as authoritative, and unknown handlers fail closed.
- [ ] Tenant and actor isolation across source, derived View, read service and downstream consumers.

**Exit:** source retraction invalidates only descendants, an erased source cannot survive inside derived materialization, rebuild yields the same current state, no built-in recall/scalar regressions.

### A5 — Demonstrate actual reuse through consumers (P1 value gate)
**Owners:** #576 M5, Context #13; Identity #1, Menhir #718 later.

- [ ] Canary #1: a synthetic third-party projection using only public registration, provenance, worker and read contracts.
- [ ] Canary #2: Context's `ContextRegion` projections using bounded async derivation and deterministic task-time selection; identity projections follow once Experience and policy gates are ready.
- [ ] Track round-trip runtime overhead, reliability, memory/cost, source revocation, cold start/restart and model-swap outcomes relative to current flat Menhir recall.
- [ ] Default-off throughout canary. Reject over-generalization if only one narrow family benefits.

**Exit:** two distinct use cases run with **no additional bespoke lifecycle manager or authoritative memory store**.

### A6 — Reduce duplicated specialization *after* canaries (P1/P2)
**Owner:** #576 migration tranche, informed by #726.

- [ ] Move scalar_state and scalar_history **incrementally** through shared dispatcher and lifecycle, not a wholesale rewrite; preserve `current_authority()`, source-of-truth log and recall behavior.
- [ ] Assess counting/timeline consolidation for genuinely common dirty/rebuild/retry patterns; preserve domain folds.
- [ ] Audit `MemoryGraphAdapter` (294 methods, 252 forwarding-style) for compatibility-only APIs; delete or generate **only after callers and type safety are proven**. Do not treat a forwarding method as equivalent to redundant semantics.
- [ ] Treat [#313](https://github.com/Archolith/menhir/issues/313) scalar label migration and [#376](https://github.com/Archolith/menhir/issues/376) parity auditing as independent safeguards, not required first to prove the abstraction.

**Exit:** measured decrease in independent lifecycle/repair paths, parity in tests/benchmarks and supported external API, quantified code reduction **or documented reason to retain code**.

### A7 — Small independent client and extension SDK (P2 after a real consumer)
**Owner:** #725 phases 5–6.

- [ ] Public API contracts for evidence writes, projection registration/monitoring, bounded reads, status and erasure; separate read-only vs authorizing capabilities.
- [ ] A small remote client without Graphiti/FastAPI/Neo4j/.env side effects; `py.typed`, version negotiation and compatibility policy.
- [ ] Test with `archolith-bench` and an external extension; embedded mode only with separately proven scheduler/graph ownership.

**Exit:** installing the client need not install a server; a second consumer does not require core source edits.

### A8 — Horizontal consolidation outside projection scope (parallel P2)
**Owners:** #726, #239 and existing individual trackers.

- [ ] AST reachability + semantic clone report separately categorizes **deleted source lines**, **moved out of wheel**, and **abstracted but still-present code**.
- [ ] Lease/heartbeat variants receive an invariant/counterexample table; unify only materially identical mechanisms. Preserve transaction and liveness semantics.
- [ ] Retire/rewire unreachable governance in #660/#566 with evidence; resolve inert deletion policy #656 separately.
- [ ] Do not merge OAuth, context/request plumbing or unrelated façade/transport implementations merely because they look similar.

**Exit:** scope-limited, safe refactors with measured maintenance benefits and no correctness/safety regression.

## Common risk gates

1. A materialized summary is **not** a source fact.
2. An absent extension must neither execute, discard nor certify that extension's queue.
3. Foreign or corrupt work cannot starve a working definition.
4. One semantic generation receives at most one accepted certified state, even under crashes/concurrent writers.
5. Version bumps, erasure, revocation and namespace isolation are tested online.
6. No regression in existing scalar/counting/recall or default startup, including when **zero extensions** are configured.
7. Measure task correctness, API surface size, independent implementations, operational risk, additional maintenance code, latency and costs—**not only LOC**.

## Scope and interpretation

This roadmap coordinates work. It is **not** a statement that #576 milestones are complete or that #725's extension phases have shipped. It intentionally avoids inventing a % complete. The most important near-term dependency is **A2 + A3**, as contracts and tested lifecycle primitives do not on their own establish external execution. Related cross-project Experience/Identity implementation is owned by [#718](https://github.com/Archolith/menhir/issues/718).
