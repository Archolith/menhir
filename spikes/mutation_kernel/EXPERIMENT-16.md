# Experiment 16 — trust-resolver deployment, invalidation, and stale-worker fencing

**Tested implementation:** `b136a5c026b5f34b48ba7ded0f693edfb28e613d`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Once Experiment 15 made purpose-trust resolver semantics part of belief formation, can resolver upgrades be deployed without allowing old processes to commit obsolete Views, while also scheduling every dependent View for refold?

## Result

Yes, in the tested model.

`TrustResolverDeploymentCoordinator` adds four generic mechanisms:

```text
PurposeTrustResolverDefinition
        ↓ publish monotonically
shared trust-resolver registry epoch
        ↓
registered resolver → ProjectionTarget dependencies
        ↓ resolver version advances
per-target dirty work generation
        ↓
fresh resolver refold
        ↓
commit under resolver-registry fence
        ↓
CAS-complete work generation
```

Resolver callables remain extension-owned. Core-like deployment machinery knows only resolver ID/version, declared purposes, generic output targets, and work generations.

## Resolver definition

A `PurposeTrustResolverDefinition` declares:

- resolver ID;
- monotonically increasing version; and
- canonical purpose vocabulary.

Its declarative contract is fingerprinted. Publishing the same resolver/version with different declared purposes fails closed as a version collision.

As with projection and graph-extension definitions, Python callable semantics are not introspected. A developer changing resolver behavior without bumping the version remains a contract violation.

## Atomic publication + invalidation

Publishing a newer resolver happens in one Neo4j transaction.

The publisher:

1. write-locks the shared registry node;
2. verifies monotonic versioning;
3. advances the resolver definition and registry epoch; and
4. increments the dirty generation for every registered dependent target.

Thus a successfully published semantic change cannot become visible without its dependent targets also becoming known-dirty.

Initial publication has nothing to invalidate. In the fixture:

```text
v1 publish -> epoch 1, invalidated targets 0
v2 publish -> epoch 2, invalidated targets 1
```

## Dependency target

The test registers one generic target:

```text
view_type = investigation.ownership_conclusion_view
subject   = deployment-parcel
scope     = deployment-investigation
key       = owner
dimension = purpose:beneficial_owner
```

The deployment coordinator does not understand parcel ownership or beneficial ownership. It persists an opaque `ProjectionTarget` dependency.

A dependency may only name a purpose declared by that resolver definition.

## Stale-worker fence

A process captures a resolver-registry snapshot while resolver v1 is current.

Resolver v1 produces a beneficial-owner View naming Bob. That result is committed successfully through `run_fenced`.

A stricter resolver v2 is then published. Its new rule no longer treats the old operator-synthesis profile as direct beneficial-owner evidence. Publication advances the registry epoch and dirties the target.

The old v1 process then attempts another commit under its captured v1 snapshot.

`run_fenced` acquires a write lock on the same registry node used by publication, sees that the epoch/definition changed, raises `StaleTrustResolverRegistryError`, and **never invokes the stale write callback**.

This closes the race:

```text
v2 publisher commits first
  -> registry epoch advances
  -> v1 worker later acquires lock
  -> stale snapshot rejected
  -> obsolete View/trace write never runs
```

If a valid old worker acquired the registry lock first, publication would serialize behind it, matching the graph-deployment fence proven in Experiment 12.

## Resolver-object binding

The commit gate also verifies that the actual in-process resolver object has the same resolver ID/version as the declared definition.

A caller cannot present current v2 definition metadata while executing a v1 resolver object through the supported gate.

This does not detect a developer silently changing code while leaving the same declared version; semantic version discipline is still required.

## Refold result

The v1 fixture contains:

```text
deed -> Alice
operator synthesis -> Bob
```

Under resolver v1 for `beneficial_owner`:

```text
operator synthesis -> trusted_tool after admission clamp
deed               -> agent
result              -> View(Bob)
```

Resolver v2 deliberately requires a new stronger evidentiary-role marker that the immutable old profiles do not contain.

Under v2:

```text
operator synthesis -> agent
deed               -> agent
result              -> Abstention(contested_top_trust)
```

The fresh v2 worker captures a v2 snapshot, refolds from the same immutable Assertions/TrustProfiles, commits the Abstention through the resolver fence, and CAS-completes the dirty work generation.

The old current View is replaced by the current Abstention. `load_views()` therefore returns no active View for that slot after repair.

## Historical derivation preservation

Resolver deployment does not mutate old TrustFoldTrace records.

After v2 repair:

- the v1 trace naming resolver version 1 still exists unchanged;
- the new v2 abstention trace names resolver version 2;
- the current projection contains the v2 result only; and
- both historical derivations remain auditable.

This gives belief-history semantics without making old derived answers current forever.

## Work-generation CAS

Dirty resolver work has its own generation counter.

The test publishes v2, capturing work generation 1, then publishes v3 before generation 1 is completed. The same target advances to generation 2.

Attempting to acknowledge the old v2 generation fails closed. A stale repair cannot mark newer semantic work complete.

This is distinct from:

- resolver definition version;
- registry epoch;
- assertion identity;
- receipt identity generation; and
- trust-profile version.

## Known-dirty read window

The experiment intentionally does **not** delete the existing v1 View at publication time.

Immediately after resolver v2 is published:

```text
shared resolver = v2
dependent work  = dirty / pending
stored View      = still the prior v1 Bob View
```

This is useful for crash recovery and avoids a destructive publication step, but it exposes an important read-side question: a generic `load_views()` call can still return a View that the system already knows was derived under obsolete semantics.

Experiment 16 proves write fencing and repair scheduling. It does **not** prove safe serving during the dirty interval.

## Real-Neo4j coverage

Workflow run: `31918244280`  
Job: `95093655248`

Measured result: **72 passed, 1 warning in 22.45s** against the throwaway `neo4j:5-community` service.

The warning remains Graphiti 0.29.2's Pydantic-v2 class-config deprecation at `graphiti_core/driver/search_interface/search_interface.py:22`.

The Experiment 16 diff from the Experiment 15 documentation head contains exactly:

- `spikes/mutation_kernel/trust_deployment.py`
- `spikes/mutation_kernel/test_trust_deployment_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

A belief-forming semantic dependency needs both a definition version and projection invalidation:

```text
resolver version      what semantics should be used
registry epoch        whether this process may still commit
work generation       whether this target still requires rebuild
current projection    last completed derived answer
historical trace      how older answers were formed
```

Those are separate pieces of state.

The same generic deployment shape has now appeared independently around projection definitions, graph-extension definitions, and purpose-trust resolvers. That is evidence for a future shared semantic-definition registry primitive, but this spike still avoids prematurely refactoring the frozen experiments into one framework.

## Next pressure point

**Read-side freshness.**

A known-dirty projection should not silently look identical to a fresh current projection.

The next experiment should test a freshness-aware read contract where a dependent target can be returned as one of:

```text
fresh outcome
stale/dirty outcome with explicit metadata
unavailable-until-rebuild
```

and where callers may choose policy (`require_fresh`, `allow_stale_with_marker`) without learning extension semantics.

The critical invariant is that a stale semantic View must never be presented as fresh merely because its projection row still exists.