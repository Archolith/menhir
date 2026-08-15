# Experiment 7 — shared registry fencing and removed-target retirement

**Tested implementation:** `078dcee41f31854e4db8a241b15767ca006695ac`  
**Production impact:** none; this experiment remains under `spikes/mutation_kernel/`.

## Question

Can independently upgraded Menhir processes share extension projection definitions without allowing a stale process to route new assertions with an obsolete mapper? And when a newer definition version stops producing a View that an older version owned, can that old View be explicitly ended instead of remaining current forever?

## Result

Yes, for the tested spike model.

`deployment.py` adds a shared, namespace-scoped projection-registry epoch. Publishing a new definition or advancing a definition version increments that epoch. An in-process `ProjectionRegistry` captures a `RegistrySnapshot`, and assertion routing includes the expected epoch in the same Neo4j statement that persists the assertion and dirties its registered projection targets.

That closes the check-then-write race:

```text
process A has registry epoch 1
process B publishes v2 -> shared epoch 2
process A attempts assertion commit expecting epoch 1
  -> Neo4j epoch predicate fails
  -> assertion is not persisted
  -> projection work is not dirtied
```

After process A refreshes to the shared definition set and captures epoch 2, normal routing can resume.

The same layer tracks which output targets a definition has actually owned. During a definition-version sync, historical evidence is mapped with the new definition. Targets still produced are backfilled normally. Previously owned targets that the new mapper no longer produces are written as explicit `Retirement` outcomes with reason `definition_no_longer_maps_target`.

A late worker that started under the older definition version cannot overwrite that retirement because the existing definition-version fence rejects the older projection write.

## Real-Neo4j coverage

The branch workflow ran the whole isolated mutation-kernel suite against the throwaway `neo4j:5-community` service at the tested implementation commit:

- stale registry snapshot rejected before assertion persistence;
- assertion count unchanged after the rejected stale write;
- refreshed registry snapshot accepts the same new assertion normally;
- v1 materialized View becomes an explicit Retirement when v2 stops mapping its target;
- stale v1 worker cannot resurrect the retired View;
- all earlier scalar, personality, lifecycle, dirty-generation, registry, multi-input, and backfill tests remain green.

Measured result: **31 passed, 1 warning in 12.54s**. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

## Boundary learned

The shared registry can verify declared definition IDs, versions, input types, and output View types. It does **not** attempt to hash Python mapper/fold callables. The extension therefore has a semantic obligation to bump its declared definition version whenever mapper or fold behavior changes.

The current epoch is global to the namespace, so any definition update conservatively fences all registry-routed writes until a process refreshes. A future implementation could shard epochs by extension/definition for availability, but that is an optimization rather than a correctness requirement demonstrated here.

## Next pressure point

The next useful substrate question is authority/admission: whether core can preserve opaque provenance and authority while an extension decides which sources are allowed to create high-authority assertions. Personality gives a concrete test: explicit user configuration must be distinguishable from model-inferred personality evidence, and inferred evidence must not be able to self-promote into user/manual authority.
