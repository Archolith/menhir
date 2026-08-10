# Plan: Projection Coverage + Realization Coverage v1

**Status: READY FOR IMPLEMENTATION — 2026-07-19.**

Research inputs:

- `.agent/research/menhir-projection-coverage-audit.md`
- `.agent/research/menhir-realization-coverage.md`

Delivery order is locked:

1. Projection Coverage first — it protects the correctness of the existing ScalarStateView authority path.
2. Realization Coverage second — it adds procedural-observation metadata without changing assertion or View authority.

The work should land as two reviewable PRs from fresh `main`, each containing two medium-sized commits. Do not combine both features into one implementation PR.

---

## Governing architecture

```text
source episodes
    ↓
typed-scalar realization run
    ↓
PerceptionRealizationObservation ledger   [procedural provenance only]
    ↓
TypedAssertion / TypedAssertionHead       [interpretation authority]
    ↓
deterministic scalar fold                 [pure derived state]
    ↓
ScalarStateView                           [materialized recall surface]
    ↓
Projection Coverage audit                 [correctness oracle]
```

Load-bearing boundaries:

- `TypedAssertionHead` remains the one-current-interpretation authority for a source claim.
- `ScalarStateView` remains a rebuildable materialization of current assertions.
- Realization observations never enter `assertion_key`, never create a second current assertion, and never raise evidence tier.
- Projection Coverage recomputes desired state from durable assertions; it never trusts the View to classify the fold.
- Neither feature produces an undefined confidence score.

---

## Current code seams

Projection Coverage builds on existing code rather than introducing a parallel fold:

- `src/menhir/domain/scalar_state_fold.py`
  - `fold_assertions(rows) -> FoldResult`
  - exact contributor IDs, effective tier, episode provenance, and slot-level abstentions already exist.
- `src/menhir/services/scalar_state_service.py`
  - `fold_entity(...)`
  - `rebuild_scalar_state(...)`
  - current materializable assertion input and authoritative View reconciliation already exist.
- `src/menhir/infrastructure/typed_assertion_repository.py`
  - durable lifecycle fields: `superseded`, `binding_pending`, `projection_pending`, binding owner, namespace, source identity.
- `src/menhir/infrastructure/view_repository.py`
  - current ScalarStateView listing and retirement exist, but the current list projection is intentionally too small for a parity audit.
- `src/menhir/infrastructure/schema.py`
  - scalar-state feature-scoped activation queries and required-index checks.
- `src/menhir/memory_graph_adapter.py`
  - existing scalar-state passthrough boundary.

Realization Coverage builds beside the typed-scalar gate:

- `src/menhir/services/typed_scalar_perception.py`
  - `extract_typed_scalars_once(...)`
  - `gate_typed_scalars(...)`
  - `TypedScalarDecision`
  - `bind_and_persist_typed_scalars(...)`
- `src/menhir/domain/typed_assertion.py`
  - binding-stable `source_key` and durable assertion identity.
- `src/menhir/infrastructure/typed_assertion_repository.py`
  - current-head decision and same-version disagreement behavior.

No new fold, assertion identity, or View kind is required.

---

# PR 1 — Projection Coverage v1

Suggested branch:

```text
feat/projection-coverage-v1
```

The first PR is read-only. It reports violations and recommended repair actions but does not automatically mutate Views.

## Commit P1 — Pure projection-audit core

Suggested commit:

```text
feat(scalar-state): add projection coverage audit core
```

### New domain module

Add:

```text
src/menhir/domain/projection_coverage.py
```

Define the stable audit contract:

```text
AssertionLifecycle = CURRENT | SUPERSEDED
BindingStatus = BOUND | BINDING_PENDING | BINDING_MISMATCH
EligibilityRole = MATERIALIZABLE | BINDING_ADVISORY | RETRACTED | OPERATOR_VETOED
FoldRole = CONTRIBUTOR | NON_CONTRIBUTING_MEMBER | SLOT_ABSTENTION_MEMBER
ProjectionStatus = NOT_REQUIRED | PROJECTION_PENDING | PROJECTED | PROJECTION_ERROR

AuditFailureClassification =
    UNACCOUNTED
  | MULTIPLY_ACCOUNTED
  | INVALID_COMBINATION
  | NAMESPACE_MISMATCH
  | CORRUPT_OR_BYPASSED_WRITE_PATH

ParityViolationKind =
    MISSING_VIEW
  | ORPHANED_VIEW
  | VALUE_MISMATCH
  | VALID_AT_MISMATCH
  | CONTRIBUTOR_SET_MISMATCH
  | TIER_MISMATCH
  | PROVENANCE_MISMATCH
```

Add immutable report values:

```text
ProjectionAccountingRecord
ProjectionCoverageViolation
ProjectionParityViolation
ProjectionCoverageReport
AuditEnrichedFoldResult
```

### Eligibility seam

Define a small protocol used by the audit only:

```python
class AssertionEligibilitySource(Protocol):
    def eligibility_for(self, assertion: dict[str, Any]) -> EligibilityDecision: ...
```

Production v1 uses an empty/default implementation:

- current, bound assertions are `MATERIALIZABLE`;
- `binding_pending` or binding mismatch becomes `BINDING_ADVISORY`;
- no production retraction/veto registry is invented in this PR.

The protocol and fake tests cover `RETRACTED` and `OPERATOR_VETOED` so a later durable registry can plug in without changing the classifier.

### Audit-enriched fold

Do not change `fold_assertions`.

Build an audit-only enrichment from the exact same grouped input:

```text
slot_members        = input current materializable assertions grouped by slot
contributors        = FoldedScalarState.contributor_ids
abstention_members  = every member of a slot returned in FoldResult.abstentions
non_contributors    = slot_members - contributors - abstention_members
```

The enriched result assigns `FoldRole` without consulting any View.

### Pure classification order

Implement the researched order exactly:

```text
1. lifecycle
2. binding status
3. eligibility role
4. fold role from the deterministic fold
5. projection status from desired fold output + actual View presence
6. authority tier
7. valid-combination checks
8. audit-level and parity violations
```

Important rules:

- lifecycle uses `coalesce(superseded, false)`, never `superseded_by` existence;
- binding pending uses `coalesce(binding_pending, false)`, never a null-subject test;
- superseded assertions have no eligibility or fold role;
- an abstained slot has `projection_status=NOT_REQUIRED`;
- a View over an abstained slot directly emits `INVALID_COMBINATION` and `ORPHANED_VIEW`;
- View contributor fields never influence `FoldRole`;
- abstentions are diagnostic and are not strict persisted-View parity fields.

### Pure parity comparison

Implement a pure function over:

```text
desired: AuditEnrichedFoldResult
actual: list[ScalarStateViewAuditRow]
```

Compare per namespace-scoped slot:

- normalized scalar value;
- parsed/normalized `valid_at`;
- exact contributor assertion-ID set;
- exact effective tier;
- exact episode-provenance set.

Detect duplicate current Views for one slot before comparing fields.

### P1 unit tests

Add focused offline tests, with no Neo4j:

```text
tests/test_projection_coverage_domain.py
```

Required cases:

1. same-version non-current assertion is `SUPERSEDED` even without `superseded_by`;
2. `unbound:<source_key>` plus `binding_pending=true` is advisory;
3. View corruption cannot change contributor/non-contributor assignment;
4. no-anchor, ambiguous-anchor, and delta-on-range slots assign abstention roles;
5. abstained slot plus actual View emits both required violations;
6. missing View;
7. orphaned View;
8. duplicate current Views;
9. wrong value with correct contributors;
10. correct value with wrong contributors;
11. wrong tier;
12. wrong episode provenance;
13. namespace mismatch;
14. deterministic report ordering under shuffled input;
15. fake retraction and veto decisions remain non-materializable.

P1 must not touch Neo4j schema or production write paths.

---

## Commit P2 — Repository reads, audit service, and operator surface

Suggested commit:

```text
feat(scalar-state): wire projection coverage audit reporting
```

### Repository read surfaces

Add a purpose-built assertion query to `TypedAssertionRepository`:

```python
assertions_for_projection_audit(
    subject_uuid: str,
    *,
    namespace: str | None = None,
) -> list[dict[str, Any]]
```

It must return every durable assertion version needed for Audit A, including:

- `assertion_id`, `assertion_key`, `source_key`;
- `subject_uuid`, namespace, slot fields, operation/value;
- `valid_at`, `learned_at`, episode UUID;
- evidence tier and perceiver version;
- `superseded`, `superseded_by`;
- `binding_pending`, `projection_pending`;
- enough head/current-owner information to identify binding mismatch without a second per-row query.

Add a detailed View query to `ViewRepository` rather than widening the rebuild-oriented method:

```python
list_scalar_state_views_for_audit(
    *,
    subject_uuid: str,
    namespace: str | None = None,
) -> list[dict[str, Any]]
```

Return:

- View UUID and `view_key`;
- namespace and current marker;
- complete slot identity;
- `view_value` and `valid_at`;
- `scalar_contributors`;
- `scalar_effective_tier`;
- stored `episode_uuids`;
- retirement/supersession fields useful for diagnostics.

Do not reuse a View from another namespace to satisfy the audit.

### Audit service

Add:

```text
src/menhir/services/projection_coverage_service.py
```

Primary API:

```python
audit_entity(
    subject_uuid: str,
    *,
    namespace: str | None = None,
    eligibility_source: AssertionEligibilitySource | None = None,
) -> ProjectionCoverageReport
```

Behavior:

1. load audit assertions;
2. classify lifecycle/binding/eligibility;
3. select current materializable assertions independently of Views;
4. call the existing `fold_assertions`;
5. enrich the fold for assertion-level roles;
6. load current Views;
7. classify projection status;
8. run strict fold/View parity;
9. return one deterministic report.

Add a bounded batch API only after the entity audit is proven:

```python
audit_entities(work_items: list[tuple[str, str | None]]) -> ProjectionCoverageBatchReport
```

The batch API must isolate per-entity errors and never turn a failed audit into a clean report.

### Adapter exposure

Add internal passthroughs on `MemoryGraphAdapter`:

```text
audit_scalar_projection(...)
audit_scalar_projections(...)
```

Do not expose a public MCP tool or recall-time dependency in v1.

### Repair contract

Every violation may carry:

```text
repairable: bool
recommended_repair: rebuild_view | retire_orphan | inspect_write_path | operator_only
```

V1 is report-only:

- no automatic rebuild;
- no automatic retirement;
- no clearing of `projection_pending`;
- no mutation during audit.

Existing `ScalarStateService.rebuild_scalar_state(...)` remains the explicit repair primitive. A later hardening PR may add an operator-confirmed repair wrapper after report correctness is proven live.

### P2 tests

Add:

```text
tests/test_projection_coverage_service.py
tests/test_projection_coverage_repository_live.py
```

Offline service tests use fakes. Live Neo4j tests must cover:

- clean bound assertion and matching View;
- crash window: assertion has `projection_pending`, no View;
- crash window: View exists, marker remains pending;
- binding-pending sentinel;
- superseded assertion retained in history;
- stale contributor receipt;
- same value with changed contributor set;
- orphaned current View after slot abstention;
- two namespaces sharing one subject UUID remain isolated;
- audit performs no writes.

### Projection PR acceptance gate

Run:

```text
PYTHONPATH=src uv run pytest -m unit \
  tests/test_projection_coverage_domain.py \
  tests/test_projection_coverage_service.py -q

PYTHONPATH=src uv run pytest -m unit -q

# With the existing disposable Neo4j test fixture:
PYTHONPATH=src uv run pytest \
  tests/test_projection_coverage_repository_live.py -q

git diff --check
```

No LongMemEval or archolith-bench run is required for this correctness layer.

Exit condition:

> Given a namespace-scoped entity, Menhir can deterministically explain every assertion's lifecycle/fold/projection role and prove whether every current ScalarStateView equals the existing fold.

---

# PR 2 — Realization Coverage v1

Start from fresh `main` after Projection Coverage is reviewed and merged.

Suggested branch:

```text
feat/realization-coverage-v1
```

The first version is an internal observation and query layer. It does not automatically run multiple expensive realization families on every ingest.

## Commit R1 — Observation ledger and pure coverage derivation

Suggested commit:

```text
feat(perception): add realization observation ledger
```

### New domain module

Add:

```text
src/menhir/domain/realization_coverage.py
```

Define:

```text
RealizationDescriptor
PerceptionRealizationObservation
ObservationLifecycle = ACTIVE | SUPERSEDED | RETRACTED
MetadataStatus = COMPLETE | INCOMPLETE
RealizationCoverage
InterpretationCoverage
IdempotencyCollisionViolation
```

Base status:

```text
SINGLE_FAMILY
CROSS_REALIZATION_AGREEMENT
CROSS_FAMILY_DISAGREEMENT
```

Orthogonal flags:

```text
interpretation_disagreement
multi_fingerprint_agreement
metadata_incomplete
```

### Canonical identities

Use canonical JSON with sorted keys and explicit schema versioning:

```text
realization_fingerprint = hash(full RealizationDescriptor)

realization_family_id = hash(coarse family tuple)

observation_key = hash(
    namespace,
    source_key,
    realization_fingerprint,
    realization_run_id,
)
```

The family tuple must be explicit and versioned. It must not include incidental process/host IDs or random seed.

The immutable observation-payload hash covers at least:

- interpretation label;
- sample-result hashes;
- proposal-payload hash;
- realization fingerprint;
- realization run ID.

### New repository

Add:

```text
src/menhir/infrastructure/realization_observation_repository.py
```

Primary methods:

```python
record_observation(observation) -> ObservationWriteResult
attach_observation_target(observation_key, *, assertion_id=None, source_key=None) -> TargetWriteResult
active_observations(namespace, source_key) -> list[dict[str, Any]]
observation_history(namespace, source_key) -> list[dict[str, Any]]
transition_observation(observation_key, transition) -> TransitionResult
```

Write behavior:

- no existing key -> create one observation;
- same key plus identical immutable payload -> idempotent success, no new row;
- same key plus different immutable payload -> fail closed with `IdempotencyCollisionViolation`;
- never overwrite the existing immutable payload;
- retain both payload hashes in the structured exception/log for investigation.

Normal repeated runs and model upgrades create new keys and remain independently `ACTIVE`.

`SUPERSEDED` is reserved for an explicit correction of bad metadata or payload linkage. `RETRACTED` is an explicit source/perceiver discredit action.

Record lifecycle/metadata transitions as append-only `:RealizationObservationEvent` audit nodes while maintaining denormalized current lifecycle/metadata fields on the observation for bounded queries. A normal repeated execution must never create a supersession event for an older run.

### Graph relationships

After assertion persistence, attach:

```text
(:PerceptionRealizationObservation)-[:INTERPRETS]->(:TypedAssertion)
```

only when the observation's interpretation is the current assertion interpretation.

Otherwise attach:

```text
(:PerceptionRealizationObservation)-[:INTERPRETS_CLAIM]->(:TypedAssertionHead)
```

Observation persistence must not create or move a `CURRENT` relationship.

An observation may temporarily remain unlinked if authority persistence/linking fails. Coverage grouping uses its durable `(namespace, source_key)`; a reconciliation method may attach the missing relationship later. The observation path must never create a placeholder assertion head merely to satisfy a relationship.

### Schema

Extend scalar-state feature activation in `infrastructure/schema.py` with additive constraints/indexes:

```text
perception_realization_observation_key_unique
perception_realization_observation_source_idx
perception_realization_observation_namespace_idx
perception_realization_observation_family_idx
perception_realization_observation_fingerprint_idx
perception_realization_observation_lifecycle_idx
realization_observation_event_key_unique
realization_observation_event_observation_idx
```

Add them to `SCALAR_STATE_REQUIRED_INDEXES` because observations depend on the typed-scalar source-key identity space.

No identity-version bump and no backfill are required: this is a new additive node type.

### Pure coverage derivation

Add:

```text
src/menhir/services/realization_coverage_service.py
```

Keep derivation pure after repository load:

```text
interpretation_disagreement =
    more than one interpretation among ACTIVE + COMPLETE observations

cross_family_disagreement =
    observations from different families support different interpretations

base_status =
    CROSS_FAMILY_DISAGREEMENT, if cross_family_disagreement
    CROSS_REALIZATION_AGREEMENT, if >=2 families support one interpretation
    SINGLE_FAMILY, otherwise

multi_fingerprint_agreement =
    exists interpretation I and family F such that
    >=2 distinct fingerprints in F actively support I

metadata_incomplete =
    any ACTIVE observation is INCOMPLETE
```

When there are no `ACTIVE + COMPLETE` observations, return no current coverage result rather than inventing a family; ledger history and `metadata_incomplete` remain queryable.

### R1 tests

Add:

```text
tests/test_realization_coverage_domain.py
tests/test_realization_observation_repository.py
tests/test_realization_observation_repository_live.py
```

Required cases:

1. descriptor canonicalization is order-independent;
2. prompt patch changes fingerprint but can preserve family ID;
3. identical retry is idempotent;
4. same-key/different-payload collision fails closed;
5. repeated same-fingerprint runs remain independently active;
6. explicit correction may supersede one observation;
7. retracted observations remain in history but leave current coverage;
8. same-family agreement sets `multi_fingerprint_agreement` only;
9. same-family disagreement sets `interpretation_disagreement` but leaves `SINGLE_FAMILY`;
10. cross-family agreement;
11. cross-family disagreement;
12. incomplete metadata is excluded from family/fingerprint counts;
13. merge changes to `claim_key` do not change `(namespace, source_key)` coverage;
14. namespace isolation;
15. observation writes never change assertion-head authority.

---

## Commit R2 — Typed-scalar realization-run wiring

Suggested commit:

```text
feat(perception): record typed-scalar realization coverage
```

### Preserve the existing gate

Do not change the return behavior of `gate_typed_scalars(...)` for existing callers.

Add a wrapper contract in `typed_scalar_perception.py` or a small sibling service:

```text
TypedScalarRealizationRun
TypedScalarObservationDraft
run_typed_scalar_realization(...)
```

One realization run means:

```text
one configured k-sample gate execution
= one realization_run_id
= one observation per committed source-key decision
```

A gate batch may contain several source claims. They share one `realization_run_id`, while `source_key` keeps each observation key distinct.

The run result carries:

- descriptor, fingerprint, and family ID;
- realization run ID created before sampling;
- sample count;
- one canonical sample-result hash per sample and source key, including absent/conflicted outcomes;
- gate agreement and vote distribution;
- committed interpretation label;
- representative proposal-payload hash.

Make the existing interpretation-label helper public/stable rather than duplicating its semantics in the ledger writer.

### Persistence orchestration

Add an explicit coordinator, leaving existing calls byte-compatible when no observation repository is supplied:

```python
persist_typed_scalar_realization(
    run: TypedScalarRealizationRun,
    *,
    ...existing binding/persistence seams...,
    record_observation,
    attach_observation_target,
) -> TypedScalarRealizationPersistResult
```

For every locally committed decision:

1. write the observation idempotently;
2. run the existing binding + assertion persistence path;
3. rebuild/mark projection complete using the existing behavior;
4. attach `INTERPRETS` when the stored interpretation is current;
5. otherwise attach `INTERPRETS_CLAIM` to the existing head;
6. surface an unresolved target as a structured result, never as a silent drop.

To support correct target selection, extend `TypedAssertionRepository.record_assertion(...)` return data additively with:

```text
is_current
current_assertion_id
```

Do not change its supersession rules.

Observation creation comes before authority linkage so a crash cannot erase the fact that the realization ran. A later idempotent retry or reconciliation attaches the target. Coverage does not depend on the link being present.

### Execution scope

V1 provides an explicit internal runner used by targeted research/maintenance calls.

Out of scope:

- running several realization families synchronously on every episode ingest;
- changing the default k-sample gate;
- automatic evidence-tier upgrades;
- blocking ordinary recall based on coverage;
- public MCP/API exposure;
- universal scheduler wiring.

A later product PR may schedule cross-family runs only for selected high-value claims after cost and usefulness are measured.

### Adapter exposure

Add internal passthroughs:

```text
record_realization_observation(...)
get_realization_coverage(namespace, source_key)
get_realization_observation_history(namespace, source_key)
reconcile_realization_observation_targets(...)
```

### R2 tests

Add:

```text
tests/test_typed_scalar_realization_wiring.py
tests/test_realization_coverage_service.py
```

Required cases:

1. one k-sample run with two source claims creates two observations sharing a run ID;
2. one committed source claim creates one observation, not k observations;
3. abstained local decisions create no supporting observation;
4. same run retry creates no duplicate;
5. new gate execution with same fingerprint creates a new active observation;
6. current interpretation links via `INTERPRETS`;
7. same-version disagreement links via `INTERPRETS_CLAIM` and does not move authority;
8. binding-pending assertion may still have a realization observation but creates no View;
9. observation-link failure is surfaced and later reconciled;
10. coverage disabled/no recorder leaves existing typed-scalar behavior unchanged;
11. no realization field enters `assertion_key`;
12. no coverage result changes `ScalarStateView` output.

### Realization PR acceptance gate

Run:

```text
PYTHONPATH=src uv run pytest -m unit \
  tests/test_realization_coverage_domain.py \
  tests/test_realization_observation_repository.py \
  tests/test_realization_coverage_service.py \
  tests/test_typed_scalar_realization_wiring.py -q

PYTHONPATH=src uv run pytest -m unit -q

# With the existing disposable Neo4j test fixture:
PYTHONPATH=src uv run pytest \
  tests/test_realization_observation_repository_live.py -q

git diff --check
```

No model network calls are required; use injected deterministic samples/fakes.

Exit condition:

> Menhir can durably report how many exact configurations and coarse realization families supported or disputed a source-key interpretation without changing TypedAssertion authority or ScalarStateView output.

---

# Consolidated production gate

After both PRs are merged, run one disposable Neo4j integration scenario:

```text
1. Create one episode and entity.
2. Run realization family A; commit interpretation I.
3. Persist the assertion and rebuild its ScalarStateView.
4. Run Projection Coverage; report is clean.
5. Run family B with the same interpretation; coverage becomes CROSS_REALIZATION_AGREEMENT.
6. Corrupt only the View contributor receipt/value in the disposable graph.
7. Projection Coverage detects the mismatch while Realization Coverage remains unchanged.
8. Rebuild the View from assertions.
9. Projection Coverage becomes clean again.
10. Run family C with a competing interpretation; coverage becomes CROSS_FAMILY_DISAGREEMENT while assertion authority follows existing head rules.
```

Required invariants:

```text
wrong_view_writes = 0
assertion_authority_changes_from_coverage = 0
duplicate_observations_on_retry = 0
silent_idempotency_collisions = 0
cross_namespace_satisfaction = 0
audit_mutations = 0
```

---

# Explicit non-goals

- No new ScalarStateView kind.
- No replacement or fork of `fold_assertions`.
- No `realization_fingerprint` in assertion identity.
- No grouping by `claim_key`.
- No confidence score.
- No automatic truth selection from realization disagreement.
- No automatic evidence-tier upgrade from family agreement.
- No automatic repair in Projection Coverage v1.
- No recall ranking or retrieval-policy change.
- No LongMemEval or archolith-bench dependency for the implementation gate.
- No broad graph-schema refactor.

---

# Final implementation order

```text
P1  pure Projection Coverage domain + tests
P2  projection repository/service/adapter + live tests
---- review and merge Projection Coverage ----
R1  realization domain/repository/schema/query + tests
R2  typed-scalar run wiring + target reconciliation + live tests
---- review and merge Realization Coverage ----
combined disposable integration gate
```

Projection Coverage is the must-ship reliability layer. Realization Coverage is the subsequent observability/research layer and must remain removable without affecting assertion or View behavior.