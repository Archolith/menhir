# Experiment 11 — concurrent identity migration fencing

**Tested implementation:** `0814c7652c710227ea72d6b09597a3294d5b50d9`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can two independently prepared identity migrations race on the same source receipt without one silently overwriting the other? And can a multi-receipt merge/split fail atomically if even one prepared receipt became stale?

## Result

Yes, for the tested model.

Identity migration is split into two phases:

```text
prepare
  -> resolve every source receipt and target entity
  -> capture each receipt's current identity_generation
  -> no mutation

apply_prepared
  -> one Neo4j write transaction
  -> claim/validate migration ID
  -> lock every affected receipt in stable storage-key order
  -> compare actual generation with prepared generation
  -> any mismatch: raise + rollback everything
  -> all match: move every CURRENT_IDENTITY, increment every generation,
                update entity activity, finalize migration, commit
```

Preparation is intentionally optimistic. Only the transactional apply decides whether the plan is still valid.

## Stale prepared plan

Two different plans are prepared while one receipt is at generation `0`:

```text
Plan A: old receipt -> Target A, expects generation 0
Plan B: old receipt -> Target B, expects generation 0
```

Plan A commits and advances the receipt to generation `1`. Applying the already-prepared Plan B then fails with `stale identity migration`. It creates no durable migration record, changes no identity edge, and leaves no temporary lock marker.

The losing plan can be prepared again against generation `1`, after which it may legitimately commit and advance the receipt to generation `2`.

Exact replay of the already-applied winning migration is idempotent and does not increment the receipt generation again.

## Multi-receipt atomicity

A combined plan is prepared for two receipts while both are at generation `0`. Before it applies, another migration advances only receipt A to generation `1`.

The combined plan then locks both receipts and discovers that one expected generation is stale. The transaction rolls back entirely:

- receipt A remains at the independently committed identity/generation;
- receipt B remains untouched at generation `0`;
- the combined migration record does not persist;
- no temporary receipt locks remain.

After re-preparing against the current generations (`A=1`, `B=0`), the combined migration applies both assignments in one transaction. The resulting generations are `A=2`, `B=1`.

This proves the merge/split operation is all-or-nothing in the tested model rather than a sequence of independently committed receipt moves.

## Real concurrent writers

The third test uses two Python worker threads released through a barrier. Both enter separate Neo4j write transactions with different migration IDs and different target identities, but both were prepared against the same receipt at generation `0`.

The transactions contend on the same receipt node. Exactly one acquires/commits the generation-0 migration. The other transaction later observes generation `1` and fails stale.

The test verifies:

- exactly one successful migration result;
- exactly one stale-generation failure;
- final receipt generation is exactly `1`;
- current identity is whichever target won the race;
- exactly one durable migration record exists;
- no receipt lock markers remain;
- replaying the winning migration remains idempotent.

The test does not require Target A or Target B to win. It asserts the concurrency invariant rather than scheduler-dependent ordering.

## Migration ID semantics

`apply_prepared` claims the migration ID inside the same transaction. The migration payload hash represents the semantic migration plan, not the expected generations used as execution preconditions.

Therefore:

- exact replay can be recognized after receipt generations have advanced;
- same migration ID with different semantic content fails as an ID collision;
- a stale new migration ID rolls back its provisional migration node along with its receipt locks.

## Lock ordering

Prepared receipt assignments are canonicalized by receipt storage key, and the apply transaction writes receipt lock markers in that stable order. This gives multi-receipt plans a deterministic lock order and avoids making extension-specific entity semantics part of deadlock avoidance.

The temporary lock property is transaction-scoped state in practice: successful transactions remove it before commit, while failed transactions roll the write back.

## Real-Neo4j coverage

Workflow run: `31916870695`  
Job: `95090064252`

Measured result: **43 passed, 1 warning in 19.69s** against the throwaway `neo4j:5-community` service. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The Experiment 11 diff from the Experiment 10 documentation head contains only:

- `spikes/mutation_kernel/identity_migration_fence.py`
- `spikes/mutation_kernel/test_identity_migration_fence_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The identity model now has a fairly clean concurrency contract:

```text
source receipt                    immutable evidence anchor
CURRENT_IDENTITY                  mutable semantic resolution
identity_generation               per-receipt CAS token
PreparedIdentityMigration         optimistic intent + expected generations
transactional migration apply     authoritative atomic identity change
```

The important abstraction is that **generation belongs to the durable receipt, not to the entity**. Two different historical receipts may resolve to the same current entity and still change independently later.

The experiment reinforces the transaction-API requirement exposed by Experiment 10. Both stale graph-write fencing and concurrent identity migration need a supported way to hold Neo4j locks while checking generations and applying multiple writes atomically.

## Still open

1. **Identity-definition deployment fencing.** A process with an obsolete entity resolver could still prepare a plan using old resolver semantics unless entity/edge/materializer definitions participate in the shared registry epoch.
2. **Migration authorization/admission.** The spike proves concurrency correctness, not who is allowed to merge or split identities or what evidence must support that action.
3. **Rollback/reversal semantics.** A later corrective migration can move receipts again, but an explicit operator-facing revert/history model has not been designed.
4. **Unified transaction API.** The spike still reaches through the production repository's private driver seam.
5. **Unified work registry.** Projection work and graph/identity work still use separate research schedulers.

## Next pressure point

Bring entity/edge/materializer definitions under the same deployment fencing proven for projection definitions. A stale process should not be able to create entities, prepare identity migrations, or materialize graph topology under an obsolete resolver/schema version after a newer extension definition has been published.
