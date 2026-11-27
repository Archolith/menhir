# ADR 0011 — Deterministic, Versioned, and Replayable Ingestion

- **Status:** PROPOSED (2026-11-26). Target architecture; implementation and rollout remain open.
- **Date:** 2026-11-26
- **Deciders:** ctharvey (review pending)
- **Related:** [ADR 0003](0003-event-fold-view-projection-boundary.md),
  [ADR 0004](0004-single-runtime-owner-and-backend-first-access.md),
  [ADR 0005](0005-core-enforced-namespace-isolation.md),
  [ADR 0006](0006-recoverable-sagas-for-cross-store-mutations.md),
  [ADR 0007](0007-evidence-gated-default-off-feature-activation.md),
  [ADR 0009](0009-source-bound-admission-authority.md),
  [ADR 0010](0010-deterministic-canonical-self-identity.md),
  [#469](https://github.com/Archolith/menhir/issues/469).

## Context

Menhir's application and graph have evolved at different rates. New writers, perception rules,
temporal semantics, typed assertions and Views can be introduced without proving that previously
ingested evidence received the same capabilities. A successful application upgrade or a READY
episode is not proof of semantic migration completeness.

Concrete examples include pre-P3 temporal edges without world-end stamps (#268), missing View
embeddings observed in production (#468), and scalar coverage/repair gaps identified by code review
(#929). The extent of historical capability debt is not yet measured. #376 proposes a complementary
fold-parity audit. The existence of these issues does not establish that every old episode needs a
repair.

A full LongMemEval-S ingestion is also expensive: the owner reported approximately USD 6 for 30
cases, or USD 100 for 500 by linear extrapolation. Repeating all model work on each release is
unsustainable for iterative development and undesirable for users with long-lived graphs.
Issue #469 tracks the implementation of selective re-extraction and migration.

## Decision drivers

- Stable, reproducible semantic state from the same evidence and accepted observations.
- Model uncertainty must be contained at explicit interpretation boundaries, not spread into
  deterministic state transitions.
- Preserve correct work across releases; avoid paying again for unchanged stages.
- An upgrade must account for historical evidence, not just new writes.
- Missing coverage must be visible, and absence of a derived artifact must not imply absence of a fact.
- Correction, erasure, isolation, authority and recovery guarantees must survive replay.

## Decision

Menhir adopts versioned, dependency-aware ingestion and treats graph migration readiness as part
of release readiness. **Memory evolves because evidence, accepted observations, or rules change,
not because the same model call happens to be sampled again.**

### 1. Determinism is a semantic contract

For identical retained evidence, accepted observations, effective rules/configuration, relevant
dependency state, ordering and explicit evaluation time, deterministic processing must produce
equivalent semantic results. Equivalence covers facts, identities, provenance, authority,
temporal validity and derived state, not incidental database IDs or operational telemetry.
Stable identities required by existing contracts remain stable.

LLMs and embedding providers can be nondeterministic even with identical requests. Their
outputs are observations or proposals, not authority. Record the accepted observation and its
provenance when policy permits. Replay it rather than resampling it unless explicitly invalidated.
Deterministic admission and domain invariants decide what it can support (ADR 0009); stable
reproduction is not evidence of factual correctness. Correction remains possible through explicit
supersession, evidence changes or reviewed migration. ADR 0003's deterministic fold boundary
is retained, not relaxed. Existing model-dependent resolution and consolidation decisions are
explicit interpretation seams to inventory and constrain, not newly authorized authority.

### 2. Ingestion is a graph of independently versioned stages

A stage declares its semantic input/output contract, dependencies, compatibility identifier,
and replay or repair behavior. Prefer deterministic transformations and small independently
replayable units. Illustrative boundaries (not a required physical implementation or strict sequence):

- Source capture and provenance.
- Perception: extraction of candidate entities, relationships and typed observations.
- Identity and relationship resolution, admission and temporal interpretation.
- Embedding/index generation.
- Consolidation and reconciliation.
- Deterministic folds, counts and materialized Views.

Split further where meaningful cost, change frequency or ownership boundaries warrant it.
Model-dependent substeps should have separately reusable outputs when safe. Dependencies may span
episodes, entities, namespaces (only where already explicitly permitted), graph snapshots and
time. Cross-episode stateful work needs a defined causal order and conservative invalidation;
do not assume all steps are pure functions of one episode or that a cyclic dependency can be
replayed in arbitrary order. Feedback requires an explicit bounded reconciliation contract.

Persist sufficient compatible intermediates, stage receipts and provenance to resume or migrate
without repeating successful work. Record model/provider identity, prompt and contract versions,
effective settings, input/dependency fingerprints, and outcome. Reuse only when the actual
input and dependency contract matches. Model aliases alone are insufficient proof of equivalence.
A transport or refactor change need not invalidate semantic outputs.

Retention is subordinate to privacy and erasure. Do not require indefinite storage of every raw
prompt, response, secret or rejected claim; retain the minimum permitted replay material and record
when it is unavailable. Cached observations and backups must remain namespace-fenced and subject
to deletion and restore safeguards. They are not independent semantic evidence.

### 3. Release transitions preserve valid work

Each affected capability has a compatibility decision and supported migration path. Prefer, in
order:

1. Reuse compatible output unchanged.
2. Apply deterministic schema/data transformations.
3. Rebuild affected downstream projections or indexes; re-embed only where needed.
4. Re-evaluate only affected observations or source episodes at the earliest invalid stage.
5. Propagate invalidation through all dependent work and verify the resulting state.

New capabilities may require scanning retained source evidence even where no prior extraction
exists. A selector based solely on old entities cannot prove coverage of previously missed facts.
If a safe bounded selector or compatibility proof is unavailable, expand the affected scope or
mark it unknown; a full ingest is an explicitly approved last resort, never the default.

Maintain a versioned manifest recording producing release, dependency versions, stage versions,
relevant configuration and migration lineage. The application owns schema definitions; the
manifest records compatibility, not a second hand-maintained schema. Track application version
separately from graph schema and per-capability realization versions. A mixed-version graph must
not claim a single fully current semantic version.

### 4. Coverage is a first-class graph property

For each enabled capability, track coverage of the relevant source/work units with explicit
outcomes: not evaluated/unknown, evaluated with no applicable output, complete, pending/stale,
failed or blocked. Disabled capabilities are distinct from completed capabilities. A successful
evaluation producing zero outputs is a valid recorded outcome, not a missing receipt.

Coverage must be tied to the relevant source/dependency revision and capability version.
A cursor or READY status alone is not proof of complete processing. The coverage audit should
report per-capability denominators, missing work, failed work, and deterministic versus paid
repair paths. The first adoption step is a read-only historical coverage census; legacy records
without adequate evidence are unknown, not presumed complete or retrospectively declared failed.

### 5. Migration readiness is a mandatory release gate

Before cutting each release, classify changes against supported predecessor graphs:
compatible/no migration, deterministic migration, re-embedding, targeted model replay, or
explicitly unsupported. A no-change classification needs evidence, not an empty changelog.

A release that changes stored semantics must include tested migration code and/or an explicit,
safe compatibility path, including any new capability backfill or a supported deferred mode.
The release must establish:

- Upgrade tests against a populated previous supported graph (and supported skipped-version
  paths or a documented sequential upgrade route), not only empty installs.
- A dry-run plan, affected coverage and estimated resource/model cost before execution.
- Idempotent replay and interruption/restart tests, provenance/identity preservation, and
  equivalence checks against clean derivation on representative controls.
- A documented compatibility/read policy, rollout order, migration recovery and rollback
  constraints, and operator-visible progress and outstanding work.
- No unsafe automatic paid work: respect explicit cost budgets and approved scope.

Mandatory readiness does not mean every user must finish all optional backfills before using
unaffected capabilities. Required incompatible state must be gated until repaired; deferred
work is marked pending and cannot masquerade as current or complete. Existing authority and
erasure invariants cannot be waived by deferral. Feature activation remains governed by ADR
0007. A release with no safe path for required data is blocked, rather than silently stranding it.

Adoption of the automated release gate is tracked in #469; until implemented, the repository
must not claim it already enforces this contract. The migration implementation and any transition
plan for legacy releases require review.

### 6. Repair is governed, recoverable and inspectable

Preserve original episode identity, source time and provenance. Remove or supersede only the
affected support, never unrelated support for shared entities. Rebuild the dependency closure and
publish current results only after validation. A failed or interrupted migration must be distinguishable
from a complete one; coordinate with runtime writers and use recoverable mutation protocols
(ADRs 0004 and 0006). A restore or replay cannot resurrect erased evidence or bypass admission,
namespace or lifecycle restrictions. Rollback is conditional on those constraints, not guaranteed
for irreversible operations.

### 7. Benchmarking uses the same compatibility contract

Keep immutable permitted benchmark snapshots and stage manifests. Retrieval-only experiments
reuse compatible ingestion. Migrated runs are explicitly labeled and validated on a representative
fresh-ingest control subset; fresh model observations may differ, so define semantic/quality
tolerances before evaluating. Periodically check migration drift against clean ingestion and retain
a held-out final evaluation. Report actual calls, costs, coverage and unresolved differences.
A migrated run is not silently reported as a fresh end-to-end benchmark.

## Considered alternatives

### Re-ingest all evidence on every release

Rejected as the default. It discards compatible work, increases cost and can change correct
observations solely through model variance. Retained as an explicit fallback where safe selective
replay is impossible.

### Only migrate the database schema

Rejected. Valid labels, indexes and constraints do not establish that old evidence has passed
new semantic processing or that Views are current.

### Cache the finished graph alone

Rejected. Final state omits the boundaries necessary to repair one stage economically and
identify the dependencies of a changed interpretation.

### Keep sampling models until results agree

Rejected as the general replay contract. Agreement can be a perception policy, but it is
neither evidence authority nor a substitute for preserving an accepted result.

## Consequences

- Changes to ingestion contracts carry a compatibility, coverage and migration obligation.
- Deterministic replay becomes an independently testable property, reducing recurring model
  spend and isolating stage-level quality regressions.
- Storage, dependency accounting, privacy-safe caching and recovery impose additional engineering
  cost. Prefer existing graph, journal, projection and repair infrastructure.
- Some historical coverage may be unrecoverable because evidence was not captured or was erased;
  this must be reported honestly, not reconstructed from speculation.

## Evidence and implementation tracking

- [#469 — Version-aware ingestion migration](https://github.com/Archolith/menhir/issues/469)
- [#423 — Supported-only cleanup/re-derivation](https://github.com/Archolith/menhir/issues/423)
- [#376 — Scalar projection audit](https://github.com/Archolith/menhir/issues/376)
- [#268 — Legacy temporal stamps](https://github.com/Archolith/menhir/issues/268)
- [#468 — View embedding gaps](https://github.com/Archolith/menhir/issues/468)
- [#929 — Incomplete scalar work](https://github.com/Archolith/menhir/issues/929)

## Open owner decisions

- Exact stage granularity, durable receipt format, retention budgets and compatibility fingerprint
  mechanism after auditing existing infrastructure.
- Supported release upgrade window, transitional adoption of the gate, and rollout of legacy
  capability backfills after the read-only census.
- Migration cost limits, control-sample policy, production rollout and recovery operating envelope.

## Non-goals

This ADR does not prescribe a new graph database, one storage format for every stage, byte-identical
fresh LLM outputs, unlimited retention, automatic feature activation, paid benchmark execution,
or an immediate production migration. It does not authorize erasure reversal or weaken existing
source-bound authority. Implementation is owned by #469 and reviewed follow-up work.
