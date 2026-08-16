# Experiment 12 — graph extension deployment fencing

**Tested implementation:** `7c9e5ae69f56f3c3a7656598acd579ccaccc3c84`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Can a stale Menhir process continue creating entities, applying identity migrations, or materializing graph topology after a newer graph-extension definition has been published?

The graph-facing definition includes the versions of:

- extension-owned entity identity resolvers;
- extension-owned edge schemas; and
- graph materializers.

## Result

Yes, stale processes can be fenced in the tested model.

The experiment adds a shared namespace-scoped graph registry with a monotonic deployment epoch:

```text
GraphExtensionDefinition
  entity-kind contracts
  edge-kind contracts
  materializer contracts
        ↓ publish
shared graph-registry epoch
```

A process captures a `GraphRegistrySnapshot` containing the current epoch and the declared contract hashes/versions it loaded locally.

Before a graph-facing operation, `run_fenced` acquires a write lock on that shared registry node and verifies the snapshot epoch is still current.

```text
publisher commits v2 first
  -> epoch advances
  -> stale v1 process later acquires registry lock
  -> epoch mismatch
  -> operation never runs

v1 writer acquires registry lock first
  -> v1 is still shared-current
  -> its operation completes
  -> publisher waits on the same registry node
  -> publisher advances epoch afterward
```

This is conservative correctness fencing. It is not yet a throughput recommendation.

## Declarative graph contracts

`GraphExtensionDefinition` contains declarative metadata for:

- `EntityKindContract(kind_id, version)`;
- `EdgeKindContract(kind_id, version, allowed endpoint kinds, temporal/provenance flags)`; and
- `GraphMaterializerContract(materializer_id, version)`.

The shared registry stores a stable hash of that declarative contract. Publishing the same definition/version with different declarative metadata fails closed as a version collision. Publishing a newer version advances the registry epoch.

As with the projection registry, arbitrary Python resolver/materializer callables are not hashed. An extension still has a semantic obligation to bump its declared version when behavior changes.

## Stale entity resolver test

The fixture intentionally gives v1 and v2 different identity-resolution semantics for the same `deployment.person` proposal:

```text
v1 resolver -> hash("shared")
v2 resolver -> hash("v2:shared")
```

The IDs are therefore measurably different.

The test publishes v1, captures a v1 snapshot, and creates one entity successfully. It then publishes v2.

A stale v1 process using its old snapshot is rejected before `record_entity` executes, so the stale v1 identity does not appear in Neo4j. Capturing a new snapshot while still presenting local v1 definitions also fails because local contracts no longer match shared-current contracts.

After the process loads v2 and captures a v2 snapshot, entity creation succeeds and produces the v2-resolver entity ID.

## Prepared identity migration test

A migration is prepared while graph definition v1 is current. Before that prepared migration is applied, v2 is published.

Applying the prepared v1 migration under its stale graph snapshot is rejected before the migration transaction runs:

- the source receipt stays at its original identity;
- identity generation remains `0`;
- no `MutationIdentityMigration` record is created.

A refreshed v2 process must capture the v2 snapshot and re-prepare the migration under the current graph-extension contract. That fresh migration then applies normally and advances the receipt generation.

This keeps the Experiment 11 generation CAS and migration transaction semantics intact while adding a deployment-compatibility fence above them.

## Stale edge/materializer test

The fixture builds a real graph `EdgeSet` under v1, then publishes graph definition v2 before writing it.

The stale v1 materializer is rejected before `reconcile_edges` runs; the active relationship count remains zero.

The refreshed v2 process captures the v2 snapshot and materializes the same semantic relationship through the current graph schema. One active Neo4j relationship is then present.

## Registry-lock boundary

`run_fenced` deliberately does not copy every graph operation into one giant registry-aware Cypher statement. It holds a transaction/write lock on the registry node while the already-atomic normal operation runs through its existing repository path.

The operation itself therefore uses its own transaction. Correctness comes from serialization against publication, not from atomic rollback across the registry transaction and operation transaction:

- while the old writer holds the graph-registry lock, the definition publisher cannot advance the shared epoch;
- once the publisher has advanced the epoch, a stale writer cannot pass the epoch check and never invokes its operation.

A promoted implementation should make this gate/transaction model explicit rather than relying on private driver access.

## Real-Neo4j coverage

Workflow run: `31917071751`  
Job: `95090627218`

The complete branch workflow finished successfully against the throwaway `neo4j:5-community` service.

Measured result: **46 passed, 1 warning in 22.44s**. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The Experiment 12 diff from the Experiment 11 documentation head contains only:

- `spikes/mutation_kernel/graph_deployment.py`
- `spikes/mutation_kernel/test_graph_deployment_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The extension substrate now needs an additional distinct version axis:

```text
entity identity                 semantic current resolution
receipt identity_generation    per-receipt concurrency/CAS
materializer work generation   rebuild scheduling
extension definition version   semantic contract version
registry epoch                  cross-process deployment compatibility
```

These are related but not interchangeable.

A graph extension can remain domain-owned while core-like machinery owns deployment compatibility. Core needs to know that a resolver/schema/materializer contract is at version N; it does not need to know what `Person`, `Parcel`, `OWNS`, or any other domain concept means.

## Limitations

1. **Global namespace epoch.** Any graph-extension definition upgrade conservatively fences all graph-extension writes in that namespace until processes refresh. Per-extension/definition epochs could improve availability later.
2. **Callable semantics require version discipline.** Declarative contract hashing does not detect a developer changing resolver/fold code without bumping the definition version.
3. **Nested transaction/gate API is spike debt.** The current coordinator reaches through `Neo4jRepository._get_driver()` to hold the registry lock while normal repository operations use their own transactions.
4. **No automatic rollout protocol.** The spike proves stale writes can be rejected, not how processes discover/reload extension code operationally.
5. **No admission/authorization semantics yet.** A current extension version may still produce an assertion or identity action with inappropriate authority unless admission policy is independently enforced.

## Next pressure point

Authority/admission. The generic core should preserve provenance and enforce a maximum authority granted by the admission path, while each extension decides whether a source is admissible for a particular assertion type. Model-inferred personality evidence, for example, must not be able to label itself `user` or `manual`; an investigative anonymous allegation must not acquire the same evidentiary authority as a recorded deed merely because an extractor emits that label.
