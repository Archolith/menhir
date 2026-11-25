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
| Definition generations, fencing and freshness | **IMPLEMENTED, NOT GENERALLY ACTIVATED** | `domain/projection_lifecycle.py`, `infrastructure/projection_lifecycle_repository*.py`, transaction-scoped `commit` with freshness certificates; unit/online atomicity tests | `pending(limit)` is global/unscoped and can be blocked by a corrupt/obsolete row (#403); no registered per-definition drain worker. |
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

- [ ] Record actual callers of `ProjectionRegistry`, `ScalarProjectionReconciler`, `ProjectionLifecycleRepository`, generic repair and `ViewRepository`; differentiate prod/test/script use. Include the five separate `MemoryGraphAdapter` construction sites documented in #725.
- [ ] Resolve #725 D1 **trusted in-process code vs sandbox/discovery** and D6 **remote client vs embedded runtime** before defining a public API. Resolve D2/D5 for worker and versioned pending work before A3.
- [ ] Freeze comparable scalar-state, scalar-history, counting, general recall, namespace erasure and View write snapshots. Add one end-to-end public-example baseline with an *unknown extension* gracefully absent.
- [ ] Keep #726 responsible for measuring physical/source/research/wheel LOC and repeated-semantic-mechanism count.

**Exit:** decisions recorded, tests/bench baselines reproducible, operator-facing modes enumerated.

### A1 — Complete the semantic contract without inventing another persistence stack (P1)
**Owner:** #576 M1.

- [ ] Define typed `EvidenceRef`, `DerivedProjectionCandidate`, namespace/agent/actor scoping, provenance and counterevidence, source-fingerprint receipt and explicit uncertainty/abstention.
- [ ] Make the `ProjectionDefinition`/ViewKind relationship explicit: **definition = inputs/fold/target**, **ViewKind = serialized output/write/read shape**, **runtime registration = binds both to execution**.
- [ ] Keep LLM output as an untrusted *candidate*; deterministic/approved interpretation can be materialized without promoting it to factual authority.
- [ ] Provide one external non-scalar example compiled/tested against public contracts (e.g., synthetic evidence-based timeline or research observation, no live capture).

**Exit:** outside package expresses its projection without importing scalar-specific internals; no semantic drift in built-ins.

### A2 — Make the shared lifecycle queue safe for other families (P0 gate before scheduling)
**Owners:** #403, #725 phase 1, #576 M2.

- [ ] Add `pending(definition_id=..., limit=...)` / safe `drain_pending` scoped by definition so unknown and corrupt work cannot starve other definitions.
- [ ] Quarantine/diagnose bad work and define old-version migration/retirement; fail closed on freshness.
- [ ] Prove multi-process fencing, generation replacement, absent-current targets, retry, crash mid-commit and namespace deletion behavior with **online Neo4j counterexamples**.
- [ ] Separate publishing a definition from allowing a worker to execute it.

**Exit:** foreign work and corrupt first page cannot block a healthy definition, no stale worker can commit, a host lacking an extension does not certify its work.

### A3 — One runtime extension/dispatched materializer path (P1 critical path)
**Owner:** #725 phases 2–4 + #576 M3.

- [ ] Introduce a generic reconciler/dispatcher that pairs a `ProjectionDefinition` with its authorized materializer; no wildcard dispatch for unknown definitions.
- [ ] Centralize runtime repository/adapter creation or inject one resolved per-process view-kind map through all constructors, not only one bootstrap path.
- [ ] Add default-off scheduler lane per **supported** definition, bounded batches, fenced owner and explicit repair-handler registry.
- [ ] Test no-extensions identical startup; two different extensions in one process; A has extension/B lacks it; removing/upgrading extension leaves prior work safe; two concurrent hosts; tenant-scope isolation.

**Exit:** a separately installed extension registers **and executes** without patching Menhir; all S1–S7 safety invariants from #725 pass.

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
