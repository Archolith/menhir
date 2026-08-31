---
artifact_schema: 1
artifact_uuid: f9853d8d-e8e4-4e98-bc99-a45fcfa1ed15
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 2 — runtime orchestration

## Why

The durable lifecycle repository can publish definitions, reconcile targets, issue optimistic work
tokens, atomically materialize, certify hashes, and assess freshness. Nothing in the production
runtime currently publishes the scalar definition, marks work from assertion changes, schedules
pending targets, or invokes `ScalarStateProjectionMaterializer`. Correct primitives without a host
path are not yet a usable foundation.

## Scope

Build one generic, bounded, restart-safe projection host around the promoted contracts:

- startup definition publication and validation;
- assertion-to-definition routing and old/new target reconciliation;
- bounded pending-work execution through adapter-owned materializers;
- historical/backfill discovery;
- recovery, telemetry, and operator diagnostics;
- scalar shadow operation before any production write cutover.

## Proposed design

### 1. Definition publication

At backend startup, activate lifecycle schema and publish every composed definition with an explicit
semantic hash. A lower version or same-version/different-hash collision refuses readiness. A higher
version invalidates known targets through the existing lifecycle repository and reports the count.

### 2. Dirty routing

An assertion mutation emits a domain-neutral durable event or invokes a post-persistence dispatcher
that:

1. resolves matching definitions by assertion type;
2. derives the previous and current target identities where applicable;
3. marks added/changed targets dirty;
4. reconciles removed targets as `target_present=false`;
5. records enough cursor state for missed work to be rediscovered.

Correctness must not depend solely on the immediate hook. A bounded scanner reconciles authoritative
assertions against lifecycle membership for crash recovery, imports, definition publication, and
historical backfill.

### 3. Generic worker

The worker reads `pending(limit)`, resolves each token's shared-current definition and its single
materialization adapter, and calls `ProjectionLifecycleRepository.commit`. The adapter receives only
the supplied transaction and token and returns the canonical hash of the exact installed present or
absent state. Per-target failures are isolated and visible; stale/already-completed tokens are normal
concurrency outcomes, not silent success.

Materialization adapter responsibilities:

- load authoritative assertion inputs inside the transaction;
- evaluate at an explicit time boundary;
- install, refresh, abstain, or retire exactly one target;
- compute/verify the installed-state hash;
- never commit independently or mutate another target.

### 4. Scheduler and operations

Add a bounded maintenance task behind a default-off runtime setting. Expose queue depth, oldest dirty
age, completions, stale-token races, failures by definition, definition versions, and freshness
coverage through existing diagnostics rather than a parallel admin service. Startup and shutdown
must use the canonical scheduler lifecycle.

### 5. Scalar shadow

Before enabling generic writes, publish and reconcile `typed_scalar.current_state`, evaluate desired
outcomes, and compare them with existing scalar Views through projection and realization coverage.
Shadow mode records no replacement View writes. Promotion to write mode belongs to Phase 4 after the
defined parity window.

## Risks and controls

- Lost immediate hooks: the authoritative backfill scanner guarantees eventual rediscovery.
- Duplicate workers: generation and definition fences remain at the atomic commit boundary.
- A slow extension monopolizes maintenance: bound batch size and per-definition work; retain current
  scheduler execution budgets and diagnostics.
- Definition drift across replicas: readiness binds every process to the same published version/hash
  before workers run.
- Adapter side effects escape rollback: materializers receive the transaction adapter only; tests
  inject certification failure after mutation and prove full rollback.

## Validation

- Startup: first publication, exact replay, version upgrade, lower-version refusal, hash collision.
- Routing: create, correction, supersession, target move, removal, unrelated assertion type.
- Recovery: crash before dirty mark, crash after dirty mark, missed historical assertion, partial
  backfill, restart with the same cursor.
- Concurrency: two workers, stale generation, superseded definition, completion replay, callback
  failure, receipt collision, and post-materialization certification failure.
- Isolation: per-definition failure does not hide or poison other work; namespace/subject targets do
  not cross.
- Real Neo4j: execute publication, dirty/reconcile, competing workers, rollback, receipt, and
  freshness assessment against the actual transaction engine.
- Shadow: scalar desired outcomes and installed hashes remain clean for a representative corpus and
  at least one asynchronous processing cycle.

## Exit gate

One host can restart, publish definitions, rediscover missed work, process bounded targets through a
generic adapter, and prove exact installed freshness. Scalar runs in shadow with no default behavior
change, and a test adapter demonstrates that the worker contains no scalar vocabulary.

## Docs to update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `.agent/workflows/operations_runbook.md`
- `.agent/workflows/backend-first-mcp.md`
- `.agent/default-off-features.md`
- scheduler protocol/task documentation
- `CHANGELOG.md`
