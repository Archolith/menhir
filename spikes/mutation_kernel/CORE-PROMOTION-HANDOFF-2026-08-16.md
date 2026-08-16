# Menhir Core Promotion — Session Handoff

Date: 2026-08-16

Purpose: resume the core-promotion work in a new session without redoing completed archaeology or validated tranches.

## Read this first

The live repository state is **ahead of the older `CORE-PROMOTION-ROADMAP.md` status markers**. That roadmap still says Tranche 3 is next, but Tranches 3 and 4 are already implemented, validated, and represented by draft stacked PRs. Treat the PR bodies and branch heads below as the current source of truth for status.

Do **not** restart authority/admission design from scratch. Do **not** restart projection-definition registration. The active frontier is **Tranche 5: incremental projection lifecycle**.

## Repository / merge discipline

Repository: `Archolith/menhir`

`main` is still pinned at:

- `13143d8a7ef5bfb9198db48895d55c7147f43c42`

The core-promotion work is intentionally isolated from `main` while the audit/remediation program is still moving.

Rules carried forward:

- promotion, not rewrite;
- one seam per tranche;
- scalar/coding behavior remains the compatibility oracle;
- do not mix unrelated audit remediation into promotion PRs;
- no giant plugin framework;
- run focused branch-only validation before calling a tranche validated;
- temporary validation workflows must be removed from the clean tranche branch;
- keep stacked commit ancestry intact while child PRs depend on parent branches;
- avoid squash-merging a parent underneath an open child unless children are rebased/retargeted first.

## Current stacked PR chain

```text
main @ 13143d8a...
  └─ PR #11 / research/core-promotion-1
       └─ PR #12 / research/core-promotion-2
            └─ PR #13 / research/core-promotion-3
                 └─ PR #14 / research/core-promotion-4
                      └─ research/core-promotion-5   <-- ACTIVE WIP, no PR #15 yet
```

All existing promotion PRs are drafts.

### PR #11 — ViewKind registration

Title: `core: make ViewKind registration injectable`

Base: `main`

Head: `research/core-promotion-1`

Clean tested head:

- `f49ac3aebeea4ca862e1b18f3851df7095234d3f`

What it does:

- makes the existing built-in ViewKind registry injectable per `ViewRepository` instance;
- preserves the class-level built-in compatibility surface;
- existing callers/default scalar behavior remain unchanged;
- proves a test-only non-coding ViewKind can be registered without editing shared persistence.

Status: implemented + focused branch validation completed.

### PR #12 — Evidence-kind registration

Title: `core: add immutable evidence-kind registration`

Base: `research/core-promotion-1`

Head: `research/core-promotion-2`

Current docs/head:

- `08a5a3ce2fc5fecde2eaaa051e6c45c78aef1c57`

Clean tested implementation:

- `7dbadb76ab116815308d671d492c98b33b3055cd`

Validation:

- workflow run `31921918167`
- temporary validation SHA `86778149c644e401bd3e0f21b96e75b950c2b510`
- result: success

What it does:

- adds immutable `EvidenceKindRegistry` / `EvidenceKindDefinition`;
- preserves existing `ANCHOR_KINDS`, `SELF_SOURCE_KINDS`, `SOURCE_LABEL_TO_KIND`, `KIND_TO_SIGNAL`, and `DIVERSITY_FAMILY` compatibility projections;
- keeps rule-zero unknown source fallback to `agent_inference`;
- proves additive external `investigation.deed` and personality self-source registration.

Important boundary: this registry owns vocabulary metadata only. It does not own source-confidence thresholds, belief weights, admission policy, or purpose-specific trust semantics.

### PR #13 — Source-bound admission authority

Title: `Core promotion 3: source-bound admission authority`

Base: `research/core-promotion-2`

Head: `research/core-promotion-3`

Current clean head:

- `41b124f933cead626c8504c98ad76a064e004bab`

Validation:

- workflow run `31924986915`
- job `95111028140` (`focused-admission-foundation`)
- validation SHA `f3df4605dc3aaf7f2afd27941287f33740669a46`
- result: success
- real Neo4j connectivity was verified before the focused corpus ran

What it settled architecturally:

- **Do not promote the scalar `agent < trusted_tool < manual < user` ladder into a universal core trust ontology.**
- Generic core owns the trusted admission/foundation boundary; domains own authority semantics.
- `menhir.domain.admission` now provides source-bound grants, admission requests/decisions, injected authority semantics, weakest-ingress enforcement, and fail-closed handling.
- requested authority is untrusted input;
- policy can reject/lower but cannot raise beyond core ingress ceiling;
- exact source/grant set binding is enforced;
- multi-source admission remains generic even though current TypedAssertion has one source anchor.

Existing production foundation reused rather than replaced:

```text
TurnEvidence
    └─ :FOUNDS -> TypedAssertion
```

The `TurnEvidence` adapter uses the durable Menhir `turn_id` as admission source identity and deliberately does not expose prompt text as part of the admission source record.

Important non-goals already decided:

- no grant issuer/persistence schema yet;
- no scalar assertion identity migration;
- no trust-profile deployment machinery yet;
- no CF-17 remediation in this tranche;
- no scalar fold/recall behavior change.

### PR #14 — Projection-definition registration

Title: `Core promotion 4: projection definition registration`

Base: `research/core-promotion-3`

Head: `research/core-promotion-4`

Current clean head:

- `c68b45e9c84262429ff3e8860bfb3eaefb36f558`

Validation:

- workflow run `31925378158`
- job `95112016179` (`focused-projection-registry`)
- validation SHA `93040c08641a22615ccafd17798e1d828bafc25e`
- result: success
- Ruff `F` checks passed for the tranche files

What it adds:

- `menhir.domain.projection`;
- immutable `ProjectionDefinition` registration envelopes;
- extension-owned assertion type, identity/type resolver, target derivation, fold semantics, and output View vocabulary;
- generic `ProjectionTarget`;
- desired outcomes:
  - `ProjectionMaterialization`
  - `ProjectionAbstention`
  - `ProjectionRetirement`
- immutable additive `ProjectionRegistry`;
- deterministic `evaluate_projection()` with target/output/lineage validation.

Typed-scalar compatibility adapter:

- `menhir.services.scalar_projection_definition.SCALAR_STATE_PROJECTION`
- delegates actual semantics to existing production `fold_assertions()`;
- preserves materialization, abstention, expiry retirement, and no-active-assertion retirement semantics;
- live ScalarStateService/repositories/scheduler/recall are intentionally **not cut over** yet.

Hostile second-domain proof already exists in tests:

- `investigation.deed_owner`
- `investigation.statement_owner`
- `investigation.current_owner`

So Tranche 6's semantic proof has partially arrived early at the projection-contract level: investigation-specific vocabulary/fold behavior can already live outside core. Do not duplicate that proof unnecessarily.

Important Tranche 4 boundary:

> The projection registry describes **desired semantic outcomes only**. It does not own stored lifecycle, publication, dirty work, generations, fencing, or freshness.

## ACTIVE FRONTIER — Tranche 5 / `research/core-promotion-5`

Branch base:

- `research/core-promotion-4` @ `c68b45e9c84262429ff3e8860bfb3eaefb36f558`

Current head:

- `b968c3d6203360d2fb7c25d44d6bde01beafe6ff`

There is currently **no PR #15**.

There are exactly two Tranche 5 commits beyond PR #14 at the time of this handoff:

1. `70c3d3da209888d1dd511d1d38a7dcc1e52a423e` — `feat(core): expose managed Neo4j write transactions`
2. `b968c3d6203360d2fb7c25d44d6bde01beafe6ff` — `feat(core): add projection work token contracts`

### Commit 1 — managed Neo4j transaction seam

File changed:

- `src/menhir/infrastructure/neo4j.py`

Adds public:

```python
Neo4jRepository.execute_write(work)
```

Purpose:

- stop lifecycle/fencing code from reaching through `_get_driver()` directly;
- permit several graph operations plus the guarded materialization write to run in one driver-managed write transaction;
- callback must remain transaction-local/replay-safe because Neo4j may retry it.

This addresses a known spike debt: several experiments needed private-driver transaction access to prove stale-worker fencing.

### Commit 2 — lifecycle token domain contracts

New file:

- `src/menhir/domain/projection_lifecycle.py`

Adds domain-neutral lifecycle errors plus immutable `ProjectionWorkToken` carrying:

- `work_key`
- `definition_id`
- `definition_version`
- `ProjectionTarget`
- `generation`
- `target_present`
- `reason`

The token is explicitly **not** sufficient authority to write. Infrastructure must re-check it while holding the same transaction lock that guards the final materialization/reconciliation write.

Errors currently include:

- definition not published;
- stale definition;
- stale work token;
- already-completed generation;
- persisted lifecycle identity corruption.

### What is NOT done yet in Tranche 5

As of `b968c3d...`:

- no projection lifecycle repository/coordinator yet;
- no durable published-definition registry/epoch in production yet;
- no durable per-target work-generation records yet;
- no definition publication + invalidation transaction yet;
- no stale-worker final-write fence yet;
- no completion CAS yet;
- no removed-target reconciliation implementation yet;
- no backfill/discovery implementation yet;
- no freshness certificate/read policy yet;
- no Tranche 5 focused tests committed yet;
- no Tranche 5 CI validation run yet;
- no PR #15 yet.

Do **not** describe Tranche 5 as validated.

## Exact next step for the next session

Continue on `research/core-promotion-5` from `b968c3d6203360d2fb7c25d44d6bde01beafe6ff`.

First read these production files before writing:

- `src/menhir/domain/projection.py`
- `src/menhir/domain/projection_lifecycle.py`
- `src/menhir/infrastructure/neo4j.py`
- existing View persistence/reconciliation code used by scalar state
- spike experiments 5–7, 10–12, 16–17 only as evidence/reference, **not as code to copy wholesale**.

The next implementation should be the smallest durable lifecycle seam needed to prove:

```text
published ProjectionDefinition(version N)
        ↓
ProjectionTarget dirty generation G
        ↓
worker snapshots ProjectionWorkToken(N, G)
        ↓
evaluate extension-owned projection
        ↓
ONE Neo4j write transaction:
    lock/check definition + target generation
    reject stale token
    reconcile desired outcome
    mark exact generation completed
```

Required correctness properties:

1. A worker created under definition version N cannot commit after N+1 publishes.
2. A worker created for target generation G cannot commit after G+1 becomes current.
3. The stale check and materialization/reconciliation write happen under the **same transaction fence**.
4. Publication/invalidation must be crash-safe: a committed semantic definition change cannot exist without durable rediscoverable work for affected targets.
5. Completion is exact-generation CAS, not “queue became empty”.
6. Removed targets must be representable (`target_present=False`) so stale stored Views can be retired rather than silently orphaned.
7. The lifecycle layer must not interpret assertion or View payload vocabulary.
8. Existing scalar behavior stays unchanged until an explicit cutover tranche; use adapters/tests rather than rewiring live scalar service prematurely.

### Likely Tranche 5 production shape

Names are not sacred; preserve the invariants instead of forcing this exact API.

Likely pieces:

- durable published projection-definition record keyed by stable definition ID;
- durable target-work record keyed by definition + target;
- monotonic definition version / registry epoch;
- monotonic per-target generation;
- publication method that marks dependent targets dirty in the same transaction;
- work snapshot/list method producing `ProjectionWorkToken`;
- fenced completion/materialization method using `Neo4jRepository.execute_write()`;
- removed-target dirty records;
- reconciliation/materialization adapter that can consume PR #14's `ProjectionMaterialization | ProjectionAbstention | ProjectionRetirement` outcomes.

Do not add freshness certification yet unless the lifecycle implementation makes it impossible to keep separate. Experiment 17 proved freshness is a distinct read-side guarantee and the roadmap places that after reliable work/version lifecycle.

## Suggested Tranche 5 commit shape

Two commits already exist and are coherent; keep them.

Continue approximately as:

```text
70c3d3d  feat(core): expose managed Neo4j write transactions
b968c3d  feat(core): add projection work token contracts
<next>   feat(core): persist projection definition and target generations
<next>   feat(core): fence projection reconciliation by generation
<next>   test: prove projection lifecycle recovery and stale-worker fencing
<next>   docs: record tranche 5 validation and roadmap state
```

If the persistence + fenced reconciler can remain one coherent implementation commit, prefer that over artificial fragmentation.

Do **not** commit temporary validation workflow scaffolding into the clean PR history. The prior pattern was:

1. create temporary branch-only workflow;
2. run against exact frozen implementation SHA;
3. capture workflow/run/job IDs;
4. remove/reset workflow from the clean tranche branch;
5. record validation in PR/docs.

## Minimum Tranche 5 test matrix

At minimum, prove with real Neo4j where concurrency/transaction behavior matters:

- first publication creates/establishes current definition state;
- monotonic definition-version publication;
- same-version incompatible republish fails closed;
- target dirty generation increments deterministically;
- worker token for current generation succeeds exactly once;
- exact generation completion is idempotent or explicitly already-completed per contract;
- old generation token rejected after newer target invalidation;
- old definition token rejected after newer definition publication;
- stale worker callback/materialization is not allowed to commit;
- removed target creates retirement work;
- crash-recovery/discovery: durable pending work remains discoverable without relying on an in-memory queue;
- scalar compatibility remains green;
- investigation projection definition remains usable without core vocabulary changes.

A concurrency test similar to mutation-kernel Experiment 11/12 is valuable: two workers or publisher-vs-worker racing on the same target should serialize to one semantically valid result, not merely “both eventually return”.

## Architectural conclusions already settled — do not reopen casually

### The core semantic center is small

```text
Evidence
   ↓
immutable assertion/observation
   ↓
current-set / supersession
   ↓
extension-owned fold
   ↓
View | Abstention | Retirement
```

The hard generic machinery is lifecycle correctness around it:

```text
admission / provenance
        ↓
versioned semantic definition
        ↓
dirty discovery + generations
        ↓
stale-writer fencing
        ↓
projection / derivation
        ↓
freshness certification
        ↓
retrieval / bounded context
```

### Core owns correctness; extensions own meaning

Core should not know what Person, Parcel, OWNS, personality trait, source-code Symbol, or investigation hypothesis means.

Extensions should own:

- assertion vocabulary;
- identity/type interpretation;
- fold/value semantics;
- purpose-specific trust interpretation;
- target mapping;
- human/model rendering.

Core should own:

- provenance/admission envelopes;
- immutable identities and lifecycle records;
- definition registration/versioning;
- dirty work/generations;
- transactional stale-write fencing;
- rebuild/reconciliation mechanics;
- eventually freshness and bounded serving.

### “Source says X” is not “X is true”

Keep this invariant explicit through every promotion tranche. Durable source/foundation records are historical observations. Current projection topology/conclusions are rebuildable semantic state.

### Scalar authority remains an adapter/domain policy

The existing scalar tier ladder remains its SSOT. The generic admission contract has injected authority semantics and no universal authority labels/order.

### JSON/delimiting is not a semantic prompt-injection solution

Experiment 19 only proved bounded size, syntax integrity, structural provenance retention, and explicit stale policy. Do not overclaim it later.

## Mutation-kernel experiments most relevant to upcoming work

Do not redo the 19 experiments. For Tranche 5, the most relevant prior evidence is:

- Experiment 5: generational dirty-slot discovery;
- Experiment 6: projection registry/backfill;
- Experiment 7: shared registry fencing + removed-target retirement;
- Experiment 10: identity-driven scheduling + stale-worker fencing;
- Experiment 11: concurrent migration CAS/transaction authority;
- Experiment 12: graph extension deployment fencing;
- Experiment 16: semantic resolver publication + refold invalidation;
- Experiment 17: certified freshness — importantly proves that **empty work queue != proof of freshness**.

Use their invariants, not their spike schemas.

## Known repository/connector quirks

- The GitHub connector supports branch create/move and file/PR mutations but does not expose branch deletion or workflow dispatch in this environment.
- Old `research/core-promotion-1-staging*` branches remain as clutter; ignore them. They are not part of the stack.
- Validation has therefore used temporary push-triggered branch workflows and then removed them from clean heads.
- Local archive/checkout validation previously hit runtime/DNS limitations; do not convert that into a claimed test result.

## Audit/remediation interaction

Keep core-promotion work separate from active audit fixes unless a promotion invariant cannot be safely implemented otherwise.

Known example explicitly kept out of PR #13:

- CF-17 / truth admission-grounding remediation is not part of generic admission promotion.

Do not merge the stack to `main` until audit/remediation changes are reconciled against it.

## First message / action in the next session

A good continuation instruction is:

> Resume Menhir core promotion from `research/core-promotion-5` at `b968c3d6203360d2fb7c25d44d6bde01beafe6ff`. PRs #11–#14 are already draft, implemented, and focused-validated. Do not redo Tranches 1–4. Continue Tranche 5 by implementing durable projection-definition publication + per-target generations and a same-transaction stale-worker reconciliation fence using the already-added `Neo4jRepository.execute_write()` and `ProjectionWorkToken`. Keep live scalar cutover out of scope, add real-Neo4j concurrency/recovery tests, validate branch-only, then open draft PR #15 stacked on #14.

## Point-in-time warning

This handoff records repository state as observed on 2026-08-16. Before mutating anything in a new session, re-fetch:

- PR #11–#14 heads/bases;
- `research/core-promotion-5` head;
- `main` head;
- whether PR #15 has appeared since this handoff.

If any of those moved, treat the newer GitHub state as authoritative and reconcile before continuing.
