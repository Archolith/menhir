# Experiment 10 — identity-driven graph scheduling and stale-worker fencing

**Tested implementation:** `f53ce2905f985be7c4248e5fb2b1e9e261699477`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can a durable entity-identity correction automatically invalidate only the graph materializers that depend on that historical identity, while preventing a worker that derived topology before the correction from restoring an obsolete edge afterward?

This is the graph/identity equivalent of the projection-generation race already tested for Views.

## Result

Yes, for the tested model, but the experiment exposed an important implementation boundary: **freshness must be represented as explicit state and checked under a real transaction boundary.** Several plausible predicate-only Cypher guards did not fail closed in the compound graph reconciliation query and were rejected by the test.

The successful model has two independent mechanisms:

```text
identity migration
  -> durable receipt -> current-identity change
  -> scheduler snapshot diff
  -> extension maps changed historical identities to affected graph slots
  -> graph-work generation increments
```

and:

```text
worker derives graph result
  -> captures receipt-local identity generation
  -> transaction write-locks same receipt
  -> compares actual vs captured generation
  -> mismatch: raise + rollback
  -> match: reconcile graph topology in same transaction
```

The scheduler is for eventual repair/discovery. The transaction guard is for stale-write correctness. Neither substitutes for the other.

## Crash-recoverable dirty discovery

`IdentityDependencyScheduler` stores a durable snapshot of each relevant immutable receipt's:

- historical/original entity ID;
- source key; and
- current identity ID.

On a later sync it diffs that snapshot against current receipt resolution. The extension owns the dependency mapper. The investigative ownership mapper asks which immutable ownership Assertions still reference one of the changed historical person IDs and returns only those ownership materializer slots.

The reference test creates two unrelated ownership slots and changes only one owner's identity. Only the affected parcel's ownership slot is dirtied.

Work uses the familiar generation model:

```text
identity state changes
  -> slot generation N
  -> rebuild
  -> projected_generation N
```

A no-change sync manufactures no work.

## Receipt-local identity generation

Identity endpoint equality is semantic state, not a good concurrency token. The successful design adds a monotonic `identity_generation` to each immutable entity receipt.

Bootstrap gives existing receipts generation `0` without counting bootstrap as an identity change. Reassigning that receipt's `CURRENT_IDENTITY` increments the generation in the same Neo4j statement:

```text
original receipt -> historical owner   generation 0
first correction -> Corrected Owner A  generation 1
second correction -> Corrected Owner B generation 2
```

Exact migration replay returns before changing identity state, so replay does not increment the generation.

A worker derives its graph result from the source receipt and captures the exact generation it observed. The entity ID remains useful semantic/audit data; the generation is the freshness/CAS token.

## Failed guard approaches

These failures are retained as experiment evidence rather than hidden.

### 1. Endpoint-equality guard in compound Cypher

The first implementation attempted to verify that each guarded receipt still had the `CURRENT_IDENTITY` entity the worker expected before continuing the same large Cypher reconciliation statement.

Result: **39 passed, 1 failed**. The stale worker was incorrectly accepted.

### 2. Stricter exact endpoint matching

The guard was reduced to exact receipt/current-identity matching and matched-row counting.

Direct test diagnostics proved that immediately after migration:

- the exact guarded receipt was the intended receipt;
- its `CURRENT_IDENTITY` had changed to the corrected person; and
- the stale worker still held the old person ID.

The compound reconciliation statement still accepted the stale worker.

Result remained **39 passed, 1 failed**.

### 3. Receipt-generation predicate in compound Cypher

The model then introduced `identity_generation`. Direct diagnostics proved the stale worker held generation `0` while the exact receipt stored generation `1` after migration. A predicate/`EXISTS` guard at the front of the compound write still failed to fence the stale reconciliation.

Result again: **39 passed, 1 failed**.

These runs falsified the proposed implementation pattern: **do not rely on a preceding Cypher row predicate to serve as the CAS boundary for this multi-stage relationship reconciliation query.**

## Successful guard: explicit transaction and receipt write lock

The final implementation uses the production repository's private driver seam only inside the isolated spike to prove the needed transaction semantics.

Inside one Neo4j write transaction:

1. guarded receipts are sorted by storage key;
2. the worker writes a temporary guard token property to each receipt, acquiring a write lock on the same durable nodes identity migration updates;
3. the transaction returns each receipt's current `identity_generation`;
4. Python inside the transaction compares actual generation to the worker's captured generation;
5. a mismatch raises `ValueError`, rolling back the whole transaction;
6. if all generations match, graph edge reconciliation occurs in that same transaction;
7. the temporary guard token is removed before commit.

Identity migration increments the receipt generation while writing that same receipt node. Therefore the two operations serialize:

- **migration committed first:** worker locks the receipt, observes the newer generation, and aborts before topology changes;
- **worker locks first:** worker legitimately serializes before the migration; migration waits, then commits and the durable scheduler detects the identity change and dirties the affected graph slot.

This is stronger and conceptually simpler than treating entity-ID equality as a concurrency primitive.

## Investigative fixture result

The fixture starts with one immutable ownership Assertion whose historical owner is `Legacy Owner`.

```text
generation 0 -> active Legacy Owner OWNS Parcel

identity correction A -> receipt generation 1
  stale gen-0 worker rejected
  scheduler dirties ownership slot
  fresh rebuild -> Corrected A OWNS Parcel

identity correction B -> receipt generation 2
  stale gen-1 worker rejected
  scheduler dirties same ownership slot at next work generation
  fresh rebuild -> Corrected B OWNS Parcel
```

The relationship history contains all three materialized owners. Only Corrected B is current. **Every historical/current relationship carries the same immutable ownership Assertion ID as provenance.** The Assertion itself is never rewritten to pretend it originally named Corrected A or B.

## Real-Neo4j coverage

Workflow run: `31916697397`  
Job: `95089575883`

The branch workflow completed successfully against the throwaway `neo4j:5-community` service.

Measured result: **40 passed, 1 warning in 13.90s**. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The final Experiment 10 diff from the Experiment 9 documentation head contains only:

- `spikes/mutation_kernel/identity_generation.py`
- `spikes/mutation_kernel/identity_generation_guard.py`
- `spikes/mutation_kernel/identity_scheduler.py`
- `spikes/mutation_kernel/investigation_identity_dependencies.py`
- `spikes/mutation_kernel/test_identity_scheduler_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The abstraction now needs three distinct concepts rather than one overloaded version number:

```text
entity identity      semantic meaning: which entity does this receipt refer to now?
identity generation  concurrency state: has that receipt resolution changed since I derived?
work generation      scheduling state: has this materializer slot been rebuilt for latest inputs?
```

Conflating these would make either audit history or concurrency semantics ambiguous.

The experiment also exposes a likely core API requirement: a promoted generic persistence layer needs a **supported transaction callback/boundary**. Extension-neutral coordinators must be able to lock/verify durable freshness tokens and update current projections/topology atomically without reaching through `Neo4jRepository._get_driver()` or other private fields.

## Still open

1. **Concurrent identity migration fencing.** Migration itself still performs validation/replay/write as separate repository calls. Two conflicting migration writers can race before either reaches the receipt-generation update.
2. **Multi-receipt migration atomicity under contention.** A merge/split plan should lock all affected receipts in deterministic order and either apply completely or not at all.
3. **Identity-definition deployment fencing.** Entity-kind/resolver versions are not yet integrated with the shared registry epoch from Experiment 7.
4. **Unified scheduler.** View dirty work, registered projection work, and identity-driven graph work are still separate spike implementations that may share a smaller generic work contract.
5. **Supported transaction API.** The successful experiment proves the need but currently uses private driver access as spike-only debt.

## Next pressure point

Fence identity migration itself. Two plans may both be prepared against receipt generation `0`; after plan A advances the receipt to generation `1`, plan B must fail closed rather than silently overwrite A. A multi-receipt merge/split must also acquire its receipt locks in stable order and commit atomically.
