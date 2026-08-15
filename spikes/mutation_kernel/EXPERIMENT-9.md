# Experiment 9 — entity identity merge/split without provenance rewrite

**Tested implementation:** `76d28464ba0e27b396d8c9b90593717389e97bd9`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can an investigative extension correct entity identity after evidence has already been stored and graph relationships materialized, without rewriting the original source receipts or immutable Assertions?

The two hostile cases are:

1. two historical entities later determined to be the same person (merge); and
2. one historical entity later determined to have conflated two people (split).

## Result

Yes, for the tested slice.

The key abstraction is to distinguish **historical entity identity** from **current receipt resolution**:

```text
immutable source receipt --EVIDENCE_FOR--> historical Entity
                       \
                        --CURRENT_IDENTITY--> current Entity
```

`EVIDENCE_FOR` never moves. An explicit extension-owned identity migration changes only the `CURRENT_IDENTITY` overlay. Old entity nodes remain in the graph as historical identity state and can be marked inactive when no receipts currently resolve to them.

## Merge

The fixture creates two independent person identities from two source records. Two equal-latest ownership Assertions therefore initially disagree and the ownership fold abstains.

An explicit merge plan maps both original source receipts to one newly resolved current person. After the merge:

- the two original Entity nodes remain present;
- both original `EVIDENCE_FOR` relationships remain unchanged;
- both receipts resolve through `CURRENT_IDENTITY` to the merged person;
- both legacy entities become inactive;
- the immutable ownership Assertions remain unchanged;
- the identity-aware ownership rebuild now sees one current owner and materializes one current `OWNS` relationship backed by both original assertion IDs.

Replaying the exact migration is idempotent. Reusing the same migration ID with different content is rejected.

## Split

The fixture first records two source observations that the original identity resolver collapses into one person. Two ownership Assertions therefore both store that old conflated entity ID, and the latest assertion materializes an `OWNS` edge from the conflated person.

A later split plan maps each original receipt to a different current person. After the split:

- the historical conflated Entity remains present but inactive;
- asking for a current identity for that old Entity *without source context* is intentionally ambiguous;
- asking through source receipt A resolves to Person A;
- asking through source receipt B resolves to Person B;
- the stored ownership Assertion still contains the old conflated entity ID;
- rebuilding that assertion through its source receipt resolves the latest owner to Person B;
- the old graph edge is retired and retained as history;
- a new current edge to Person B is materialized with the **same immutable assertion ID** as provenance.

That last point is important: identity correction changes the current interpretation of an assertion endpoint without pretending the original extraction knew the corrected identity.

## Failure behavior

If the latest relationship assertion references an entity but no matching source receipt can resolve that historical reference, the identity-aware fold abstains with `unresolved_current_owner_identity`; it does not guess a global alias.

This is especially important after a split, where a global old-ID -> new-ID redirect is inherently unsafe because the old identity now has more than one valid successor.

## Real-Neo4j coverage

The branch workflow ran the complete isolated mutation-kernel suite against the throwaway `neo4j:5-community` service at the tested commit.

Measured result: **38 passed, 1 warning in 12.36s**. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The experiment verifies:

- merge of two historical entities into one current identity;
- split of one historical identity into two receipt-specific current identities;
- immutable source receipts remain attached to their original historical entity;
- immutable Assertions are not rewritten during identity migration;
- old entity IDs can become globally ambiguous after a split;
- source-specific resolution can still be deterministic;
- current graph relationships can be rebuilt onto corrected identities;
- superseded graph topology remains historical rather than being deleted;
- exact assertion provenance survives endpoint reinterpretation;
- migration replay is idempotent;
- same migration ID + different migration content fails closed;
- all previous scalar, personality, projection, registry, deployment, and investigative graph tests remain green.

## Boundary learned

A single global alias table is not enough for durable investigative identity. Merge can often be represented as old identities converging on one current entity, but split requires **receipt-level resolution** because the correct successor depends on which original source observation is being interpreted.

This suggests a useful generic separation:

```text
Historical Entity      durable identity that existed in prior interpretations
Source Receipt         immutable evidence receipt
Current Identity       mutable resolution of that receipt
Assertion              immutable interpretation at the time it was produced
Materialized Edge      rebuildable current graph topology
```

## Still open

1. **Concurrent migration fencing.** `IdentityMigrationPlan.apply()` validates then applies in separate Neo4j calls. The tested model assumes serialized identity administration; two concurrent contradictory migration writers are not yet fenced atomically.
2. **Resolver-version deployment.** Identity-kind versions are not yet tied into the shared registry epoch used for projection definitions.
3. **Automatic dirtying.** Applying an identity migration does not yet automatically discover every View/edge materializer whose inputs reference the affected historical entities.
4. **Transitive migrations.** The overlay points receipts directly at current identities, but chained merge -> split -> merge histories and rollback semantics have not been stress-tested.
5. **Canonical entity properties.** Names, addresses, roles, and other current entity properties still need their own evidence-backed fold; identity migration only addresses which entity a receipt resolves to.
6. **Graph provenance topology.** Relationship provenance is still stored as assertion IDs on the edge rather than reified into traversable provenance nodes.

## Next pressure point

The strongest next experiment is to connect identity migration to the already-proven registry/generation scheduler: a migration should automatically dirty every projection and graph materializer whose current result depends on affected entity references, and stale pre-migration workers must not be able to restore topology built against an obsolete identity epoch.
