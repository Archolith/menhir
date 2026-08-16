# Experiment 17 — certified read-side projection freshness

**Tested implementation:** `5a93ce1bc708488d46996fdd2431bd209c9daa84`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

After Experiment 16 proved resolver upgrades can invalidate dependent projections and fence stale writers, how can a reader distinguish a genuinely current projection from a physically present but semantically obsolete row?

A critical constraint is that freshness cannot be inferred from the scheduler alone. A worker may acknowledge a work generation incorrectly or crash between writing and certifying a projection.

## Result

A projection can be classified safely using a durable freshness certificate over four independent facts:

```text
current semantic-definition version
+ exact completed work generation
+ exact persisted projection hash
+ derivation identity
= certified semantic freshness
```

The successful read contract exposes three states:

```text
fresh
stale       (only when the caller explicitly allows stale-with-marker)
unavailable (strict policy or missing/unregistered projection)
```

The generic reader does not interpret extension semantics. It operates on resolver definitions, generic `ProjectionTarget`s, work generations, projection hashes, and derivation IDs.

## Freshness certificate

`ProjectionFreshnessCertificate` binds:

- resolver ID;
- resolver version;
- generic projection target;
- work generation;
- exact current projection hash; and
- derivation ID.

Certification succeeds only if all of the following are true in Neo4j:

1. the resolver definition/version is still shared-current;
2. the target is a registered dependency of that resolver;
3. the work row belongs to that resolver version and exact generation;
4. the expected outcome maps to that exact target;
5. a current `MutationProjection` exists at that slot; and
6. its persisted `projection_hash` exactly equals the hash of the supplied derived outcome.

Only then does certification set:

```text
completed_generation
certified_resolver_version
certified_projection_hash
certified_derivation_id
```

This means a completion acknowledgement is also tied to the actual projection bytes/structure that were derived.

## Normal lifecycle

The real-Neo4j test executes:

```text
resolver v1 publish
  -> register target
  -> write v1 View
  -> certify resolver v1 / generation 0 / exact v1 hash
  -> require_fresh read = fresh

resolver v2 publish
  -> target generation becomes 1
  -> old v1 View remains physically present
  -> require_fresh read = unavailable
  -> allow_stale_with_marker = stale(v1 View)

write v2 View but do not certify yet
  -> projection hash changes
  -> still stale

certify resolver v2 / generation 1 / exact v2 hash
  -> completed_generation = 1
  -> read = fresh(v2 View)
```

Thus writing a new row is not itself enough to make the row fresh; the exact persisted result must be certified against current semantics.

## Strict vs stale-allowed policy

`require_fresh` never returns a stale projection outcome. If the current projection is not fully certified, the result is `unavailable` and `outcome=None`.

`allow_stale_with_marker` may return the old/current physical projection, but the result structurally carries:

- `state="stale"`;
- current resolver version;
- work and completed generations;
- certified resolver version;
- certified and actual projection hashes;
- derivation ID; and
- machine-readable reason(s), e.g. `pending_generation`, `resolver_version_not_certified`, `projection_hash_not_certified`.

A caller therefore cannot obtain an unmarked stale value through the freshness-aware API.

## Scheduler-complete is not freshness

The strongest negative test deliberately misuses the weaker Experiment-16 API.

Starting from a fully certified v2 View:

1. resolver v3 is published, dirtying the target to generation 2;
2. no v3 projection is written;
3. no v3 projection is certified;
4. `deployment.complete_work(work_v3, definition_v3)` is called anyway.

After that misuse:

```text
work generation       = 2
completed generation  = 2
pending_work()         = empty
current projection     = old v2 View
certificate version    = v2
shared resolver        = v3
```

The freshness-aware reader still returns:

```text
require_fresh            -> unavailable
allow_stale_with_marker  -> stale(old v2 View)
```

The old row cannot become fresh merely because the scheduler says there is no pending work.

This is an important separation:

```text
scheduler completion != semantic freshness
```

## Projection hash mismatch

A separate test persists View A and tries to certify a different expected View B for the same target/generation.

Certification fails because the persisted `projection_hash` does not match the expected derived outcome hash.

The caller cannot certify an outcome that was not actually persisted.

## Unregistered projections

The raw generic store can contain a valid `MutationProjection` that is not registered as dependent on the semantic resolver.

The freshness-aware reader returns:

```text
state  = unavailable
reason = unregistered_dependency
outcome = None
```

Raw row existence therefore does not create semantic freshness.

## Crash behavior

Projection write and freshness certification are intentionally separate operations inside the broader resolver deployment gate in this spike.

If the worker crashes after writing but before certifying:

- the newly written projection remains physically present;
- the prior certificate no longer matches its hash and/or resolver generation;
- strict reads refuse it;
- stale-allowed reads mark it stale; and
- repair can rerun and certify the deterministic result.

This is a safe false-negative: availability is reduced, but an uncertified projection is never silently promoted to fresh.

## Real-Neo4j coverage

Workflow run: `31918436647`  
Job: `95094158204`

Measured result: **76 passed, 1 warning in 24.20s** against the throwaway `neo4j:5-community` service.

The warning remains Graphiti 0.29.2's Pydantic-v2 class-config deprecation at `graphiti_core/driver/search_interface/search_interface.py:22`.

The Experiment 17 diff from the Experiment 16 documentation head contains exactly:

- `spikes/mutation_kernel/freshness.py`
- `spikes/mutation_kernel/test_freshness_neo4j.py`

No `src/menhir` files changed.

## Boundary learned

The projection lifecycle now separates:

```text
physical projection row      what result is stored
semantic definition version  which rules are current
work generation              what rebuild work is scheduled/completed
projection certificate       which exact stored result was derived under those rules
read policy                  whether stale-but-marked output is acceptable
```

A robust read path should reason over all five rather than treating `current=true`, row existence, or an empty work queue as synonymous with semantic freshness.

## Limitations / next pressure point

1. **The freshness helper is trust-resolver-specific only because Experiment 16's deployment registry is resolver-specific.** The certificate shape itself appears domain-neutral and likely belongs beside a future generic semantic-definition registry.
2. **Certification uses spike-private envelope helpers.** A promoted API needs a public canonical projection fingerprint contract.
3. **The derivation ID is asserted by the worker rather than cryptographically linked to a `TrustFoldTrace`.** The exact projection hash is checked, but stronger lineage binding remains possible.
4. **Raw generic `load_views()` can still bypass freshness.** The experiment proves a safe read API, not that every future consumer is forced through it.
5. **Freshness has not yet propagated into retrieval/context composition.** A downstream renderer could still drop the freshness envelope if given the raw outcome separately.

## Next experiment

Propagate certified freshness into retrieval/context candidates.

The key invariant should be:

```text
freshness is structural metadata owned by the generic retrieval envelope,
not optional text supplied by an extension renderer.
```

Under `require_fresh`, a known-stale projection should produce no context candidate. Under `allow_stale_with_marker`, it may be surfaced only with an unforgeable/structural stale state that survives ranking/rendering and cannot be relabeled by extension code.