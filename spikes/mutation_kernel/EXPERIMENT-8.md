# Experiment 8 — investigative entities and rebuildable graph edges

**Tested implementation:** `a9d6ae76fb6429bdf69254fb644702b6169f9a13`  
**Production impact:** none; all implementation remains under `spikes/mutation_kernel/`.

## Question

Does the mutation-kernel architecture still hold when a domain needs to create and maintain graph entities and relationships, not just Assertions and Views?

The hostile/reference domain is an investigation with people, parcels, source records, and changing ownership.

## Result

Yes, for the tested slice.

The graph side can use the same separation already established for projections:

```text
source observations
  -> extension-owned entity identity resolution
  -> durable generic entity + source receipts

source evidence
  -> immutable ownership Assertions
  -> extension-owned ownership fold
  -> rebuildable current graph relationship
```

The generic graph layer does not know what a Person, Parcel, or `OWNS` means. The investigation extension registers:

- `investigation.person` with its identity resolver;
- `investigation.parcel` with its identity resolver;
- `investigation.owns` with allowed Person -> Parcel endpoints and temporal/provenance requirements.

Core-like generic machinery owns stable hashed entity IDs, namespace isolation, exact source receipts, endpoint-kind validation, temporal relationship history, and persistence/reconciliation.

## Entity result

Entity identity remains extension-owned rather than being hardcoded into Menhir. The fixture identifies a person by an extension-provided external ID and a parcel by county + account ID.

Two source observations using `Alice Smith` and `A. Smith` with the same external ID resolve to one durable entity while retaining two separate source receipts. The source observations are not collapsed merely because the canonical entity identity is shared.

## Edge result

Ownership is not written directly from extraction into canonical topology. A source produces a durable ownership Assertion. The investigation fold decides current ownership, and the generic graph materializer writes the result as an actual Neo4j relationship.

The fixture proves:

```text
Alice owns Parcel 123
  -> active Alice -[investigation.owns]-> Parcel 123

later Bob ownership assertion
  -> Alice edge retained as retired history
  -> Bob edge becomes the only active current relationship
```

Both relationship generations retain the exact assertion IDs that produced them.

If two equal-latest source assertions disagree about the owner, the investigation fold returns an edge abstention. Reconciliation then retires the previously active ownership relationship instead of leaving a stale graph fact visible as current. A later clean assertion can reactivate the slot with a new current edge.

The physical test relationship type is the spike-local `MUTATION_EDGE`; its semantic kind is stored as `investigation.owns`. That deliberately tests type-neutral graph machinery rather than adding an investigation-specific relationship type to generic persistence.

## Real-Neo4j coverage

The first implementation run exposed a Cypher grammar bug (`WITH` required between `FOREACH` and `CALL`) while preserving **33 passing tests**. The query was corrected without weakening the graph expectations.

The corrected implementation ran the complete isolated mutation-kernel suite against the throwaway `neo4j:5-community` service.

Measured result: **35 passed, 1 warning in 13.37s**. The warning remains Graphiti 0.29.2's Pydantic-v2 deprecation.

The experiment verifies:

- extension-owned identity can unify aliases while preserving source receipts;
- edge schemas can generically restrict allowed endpoint entity kinds;
- graph state can be a rebuildable materialization of immutable assertions;
- temporal changes preserve previous relationships rather than overwriting history;
- exact assertion provenance survives onto materialized relationships;
- ambiguous current evidence removes stale current topology;
- the materialized result is a real Neo4j relationship, not merely a relation-shaped property node;
- all previous scalar, personality, lifecycle, scheduling, registry, backfill, and deployment-fencing tests remain green.

## Boundary learned

The extensibility model now has two generic output axes:

```text
Evidence -> Assertion -> extension fold -> View / Abstention / Retirement
                   \
                    -> extension graph fold -> current Entities / Edges
```

Entities differ slightly from Views: stable entity identity is durable and source observations accumulate as receipts. Relationships behave much more like Views: their current topology is disposable/rebuildable, while the Assertions that justify it remain durable.

## Still open

1. **Identity merge/split and resolver migrations.** Versioning an identity resolver is not enough by itself if v2 decides two old entities should merge, or one old entity should split.
2. **Entity property state.** The spike preserves observed properties in source receipts, but does not yet fold competing names, addresses, roles, etc. into canonical current entity properties.
3. **Scheduler integration.** Relationship materialization currently uses direct fold + reconcile; it is not yet plugged into the generation-backed projection/materializer registry.
4. **Deployment fencing for graph definitions.** Entity/edge kind and graph-materializer versions are not yet covered by the shared registry epoch proven in Experiment 7.
5. **Provenance topology.** Exact contributor assertion IDs are stored on the relationship, but Neo4j relationships cannot themselves have relationships. A production design may want a reified relationship/provenance node if provenance must be traversed directly.
6. **Physical relationship typing.** The spike uses one physical `MUTATION_EDGE` type plus a semantic `edge_kind`. Whether production extensions need dynamic Neo4j relationship types for traversal/indexing/performance is still open.
7. **Production Graphiti integration.** This experiment intentionally uses isolated spike labels and does not claim compatibility with the production Graphiti entity/edge schema yet.

## Next pressure point

The strongest next graph test is identity evolution: introduce ambiguous identity evidence, then prove a resolver can safely merge or split investigative entities without losing source provenance or leaving materialized edges pointed at stale identities. After that, graph materializers can be wired into the same registry/generation/deployment machinery already proven for Views.
