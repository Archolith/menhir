---
artifact_schema: 1
artifact_uuid: 6d97ada4-6753-4dd1-9746-e2ef9e8b5702
artifact_type: plan
artifact_status: PROPOSED
---

# Menhir foundation Phase 4 — developer surface and cutover

## Why

After two hostile domains prove the contracts, Menhir needs a small stable way to author, register,
test, operate, and version extensions. Production activation must then replace compatibility paths
without creating duplicate Views, losing work, or allowing mixed versions to bypass the lifecycle
fence.

## Scope

- promote only the interfaces proven by scalar, investigation, and personality;
- provide deterministic startup registration, compatibility rules, documentation, examples, and a
  test kit;
- shadow, backfill, activate, and eventually consolidate the generic projection runtime;
- preserve existing data and writers throughout mixed-version operation.

Dynamic discovery, third-party code sandboxing, marketplace distribution, and physical
default-namespace key canonicalization remain out of scope.

## Developer surface

### 1. Public API

Create one documented public extension namespace that exports only the stable contracts required by
the proofs: evidence-kind definitions, admission request/policy types, assertion envelope/codec
protocols, projection definition/outcomes/targets, View-kind registration, materializer/hash
protocols, and host composition validation. Tests reject examples that import private infrastructure
modules.

### 2. Registration model

Use an explicit host-supplied extension descriptor only if the proofs require grouping. It may name
an extension ID/version and return immutable registrations; it does not discover packages or execute
request-provided code. Startup emits a deterministic registration digest and refuses collisions,
missing dependencies, unsupported host API versions, or inconsistent projection/materializer pairs.

### 3. Authoring kit

Ship:

- a minimal extension template;
- investigation and personality examples;
- fake and real-Neo4j lifecycle harnesses;
- fold-law, replay, namespace, admission-ceiling, stale-worker, and freshness assertions;
- a compatibility matrix and version-bump guide;
- operations guidance for queue/freshness diagnostics and safe disablement.

## Production invariant

Every production worker materializing a registered projection target must use the shared-current
definition, current target generation, authoritative namespace identity, and one atomic lifecycle
commit; rejection leaves the prior certified projection and all immutable evidence/assertions
intact.

Authority: published definition state plus `ProjectionWorkState` at the atomic commit boundary.
Observable refusal: stale, unpublished, mismatched, corrupt, or already-completed work returns a
typed lifecycle failure and performs no partial projection mutation.

## Rollout stages

### Expand

- deploy public composition and generic scheduler default-off;
- publish definitions and run read-only desired-state/freshness audits;
- retain every existing scalar/event writer and physical View key;
- add metrics and an emergency disable switch that stops new generic work without deleting state.

Stop condition: all replicas publish the same registration digest and shadow audits run without
cross-namespace or duplicate-current corruption. Rollback: disable generic scheduling; no writer has
changed.

### Backfill

- reconcile every authoritative assertion target in bounded resumable batches;
- retain before-images/cursors needed to prove coverage and rerun safely;
- certify absent targets as well as present Views;
- compare generic desired outcomes/hashes with existing scalar results.

Stop condition: target census is complete and repeated backfill produces zero new unexplained work.
Failure response: stop at the durable cursor, repair the adapter/definition, bump the definition when
semantics changed, and resume. Never rewrite receipts to conceal drift.

### Drain

- enable generic scalar materialization for a bounded cohort while old readers remain compatible;
- identify every old writer, scheduler, script, repair command, and independently deployed process;
- prevent old versions from creating state outside the new lifecycle before disabling them.

Stop condition: immutable deployed-version evidence shows no old writer remains and non-lifecycle
write telemetry stays at zero through representative traffic and one asynchronous cycle. Rollback:
disable the cohort and restore the old writer from the retained release; preserve generic receipts
for audit.

### Verify

- hold projection and realization coverage clean through the agreed observation window;
- exercise create, correction, removal, replay, restart, version upgrade, stale worker, and namespace
  alias cases with disposable subjects;
- compare recall-visible surfaces and provenance before/after activation.

Stop condition: zero unexplained parity/freshness failures and no duplicate-current Views. A failed
window restarts after remediation; elapsed time alone never passes the gate.

### Enforce

- make the lifecycle path authoritative for promoted definitions;
- refuse direct production writes that bypass the required generation/definition fence;
- keep compatibility reads while legacy data spellings remain.

Rollback: roll forward by re-enabling the last certified writer version or restore the prior release
only if its writer fence remains valid. Do not run mixed authoritative writers.

### Contract

- remove entity-wide scalar rebuild and duplicate closed registration paths only after telemetry
  proves they are unused;
- archive migration tools and compatibility documentation with an explicit terminal status;
- consider physical default-namespace key migration as a separate approved plan, not cleanup hidden
  in this contraction.

## System-boundary and proof requirements

The writer census includes repositories, services, maintenance tasks, ingest workers, repair and
backfill scripts, direct Neo4j helpers, deployment jobs, and external processes sharing the graph.
Use structural queries plus source/AST search and maintain a durable census test where stable. Track
manual administrative access and other services separately; source tests cannot prove those paths.

Concurrency proof must cover stale/superseded workers, definition publication races, retries,
certification failure after materialization, and shutdown/restart during a batch. Deployed proof must
name immutable release IDs for every replica and worker. If a live violation cannot be exercised
safely, record it as unproven rather than inferring behavior from source.

## Exit gate

An extension outside core can be authored from public documentation, validated at startup, tested
against the standard harness, and operated through existing diagnostics. Production projections are
created only by the fenced lifecycle path, shadow/backfill evidence is clean, rollback is tested,
and compatibility code is removed only after its measured drain.

## Docs to create or update

- public extension guide and API reference
- extension template and testing guide
- compatibility/versioning policy
- `.agent/architecture.md` and `.agent/data_models.md`
- operations and migration runbooks
- default-off feature registry and production acceptance report
- package exports/metadata and `CHANGELOG.md`
