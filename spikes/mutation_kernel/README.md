# Menhir Mutation Kernel Spike

**Status:** isolated research spike.  
**Branch:** `research/mutation-kernel-spike`  
**Base:** `13143d8a7ef5bfb9198db48895d55c7147f43c42`  
**Production impact:** none. Nothing under this directory is imported by `src/menhir`.

## Question

Can Menhir's durable memory/governance machinery become a domain-neutral substrate where coding is
an extension rather than a core assumption?

This spike uses **personality and learned behavior** as the hostile/non-coding reference domain. It is
not an attempt to turn Menhir into a chatbot. Personality exists here to force useful abstraction
boundaries.

## Hypothesis

The reusable kernel is smaller than Menhir itself:

```text
Evidence
  -> immutable Assertion
  -> explicit supersession/current set
  -> domain-owned Fold
  -> rebuildable View
```

The kernel should own identity, provenance, time, authority and generic envelope mechanics. An
extension should own value semantics, admission, fold rules, relationship semantics, serialization
of opaque values, and rendering.

## Experiment 1 — personality semantics

`kernel.py` provides domain-neutral:

- `EvidenceRef`
- source-grounded identity (`source_key`)
- immutable `Assertion`
- explicit supersession
- weakest-contributor authority
- `View` / `Abstention` / `Retirement`
- extension-owned slot `dimensions`
- a minimal `Fold` protocol

`personality.py` supplies the domain semantics:

```text
Incident
  -> TraitAssertion          -> TraitView
  -> ValueAssertion          -> ValueView
  -> BehaviorPolicyAssertion -> BehaviorPolicyView
```

An `Incident` preserves what happened, how the subject reacted, what happened next and the later
reflection. The View never replaces the incident or its assertions.

Behavior policies are scope-aware:

```text
person:<id> > group:<id> > global
```

That precedence is only a mechanism in this spike. A real extension needs a separate admission
policy defining which scopes are lawful to learn and what evidence is required.

## Experiment 2 — production scalar compatibility

`scalar_adapter.py` asks whether Menhir's existing typed-scalar machinery can fit the same kernel
without rewriting or weakening scalar semantics.

The adapter delegates all scalar meaning to production Menhir:

```text
frozen model response
  -> production extract_typed_scalars_once
  -> production gate_typed_scalars
  -> production bind_and_persist_typed_scalars
  -> production TypedAssertion
  -> production fold_assertions
  -> kernel View | Abstention | Retirement
```

The deterministic fixture freezes only the external model response. Menhir's parser, gate, binding,
assertion construction, temporal/reference-time handling and scalar fold run unchanged.

`test_scalar_neo4j.py` then takes the same experiment through the real persistence/projection seam on
a throwaway Neo4j service:

```text
frozen model response
  -> production deterministic typed-scalar ingest
  -> production TypedAssertionRepository
  -> Neo4j
  -> production ScalarStateService
  -> production scalar_state View + contributor edges
  -> generic kernel View
```

It checks durable assertion count, `10 + 3 -> 13`, weakest authority, the current View,
`CURRENT_ANCHOR` / `CONTRIBUTED_TO` edges, and replay idempotency. It does not call a live LLM or
Graphiti.

This domain exposed two kernel corrections:

1. **Extension-owned slot dimensions.** Scalar identity includes `value_kind` and `unit` in addition
   to subject/attribute/scope. The kernel preserves arbitrary named dimensions without interpreting
   them.
2. **Retirement is not abstention.** Scalar `expire` means an earlier value is known to have ended.
   That differs from being unable to determine current state, so the kernel has a `Retirement`
   outcome beside `View` and `Abstention`.

Blank scope is valid because production scalar slots use it.

## Experiment 3 — extension-neutral Neo4j persistence

`neo4j_store.py` asks whether a non-scalar extension can persist through Neo4j without either the
store or Menhir production code learning personality concepts.

`Neo4jEnvelopeStore` knows only kernel envelopes. It:

- stores immutable assertions under a namespace-scoped storage identity;
- fingerprint-checks assertion replay and fails closed if the same identity carries a different
  envelope;
- stores one replaceable current projection outcome per generic slot
  `(view_type, subject_id, scope, key, dimensions)`;
- keeps namespace isolation in persistence rather than extension semantics;
- delegates opaque value serialization to an injected extension codec.

`personality_codec.py` owns the JSON mapping for `ContinuousSignal`, `PolicySignal`, and
`PolicyDecision`. The Neo4j store never imports the personality extension.

`test_personality_neo4j.py` exercises:

```text
Personality Incident
  -> personality PolicySignal Assertion
  -> generic Neo4jEnvelopeStore
  -> reload generic Assertions
  -> personality fold_policy
  -> generic View persistence
  -> reload generic View
```

The test also replays the same inputs, adds later evidence that changes the preferred behavior,
checks that assertion history grows while the current View remains one projection row, checks
namespace isolation, and verifies the experiment created no production `:TypedAssertion` or
scalar-state View nodes.

## Experiment 4 — lifecycle, reconciliation, and crash repair

`lifecycle.py` adds a generic `ProjectionSlot` + `rebuild_projection` mechanism:

```text
durable assertion history
  -> exact slot selection (including opaque dimensions)
  -> kernel supersession/current set
  -> extension fold
  -> validate fold stayed in requested slot
  -> atomically replace current projection outcome
```

The persisted projection is not always a View. Its current state may be:

- `View` — a current answer exists;
- `Abstention` — the fold cannot safely answer, so an older View must not remain current;
- `Retirement` — an earlier answer explicitly ended and its absence is itself known state.

This lets assertion persistence and projection persistence remain separate commits. If a process dies
after a durable assertion write but before refreshing its projection, the old projection can be
reconstructed and reconciled from assertion history. The projection write itself is one Neo4j query,
so the current outcome changes atomically within that database transaction.

The lifecycle experiment also closed a genericity hole exposed by the earlier persistence test:
`load_assertions()` can now filter exact extension-owned `dimensions`. Without that, repair of two
slots sharing subject/type/scope/key but differing on axes such as unit could mix their evidence.

`test_lifecycle_neo4j.py` covers:

- an assertion-only interruption leaving a stale View, followed by deterministic repair;
- explicit supersession changing the rebuilt personality trait without deleting history;
- a contested fold replacing a previously active View with durable Abstention;
- View -> Retirement -> later View reactivation in one stable projection slot;
- exact dimension filtering so extension-owned slot axes cannot bleed into one another;
- idempotent reconciliation after the repaired projection already matches the fold.

## Experiment 5 — dirty-slot discovery and optimistic generations

`dirty.py` tests whether repair can scale without repeatedly scanning all assertions. A new immutable
assertion can be committed together with one or more generic projection targets in **one Neo4j
statement**. Each target has a monotonically increasing work generation:

```text
new Assertion + target ProjectionSlot(s)
  -> atomic assertion/work commit
  -> work generation N
  -> discover only work_generation > projected_generation
  -> extension fold for that slot
  -> persist outcome tagged generation N
```

Replaying the same immutable assertion does not advance the work generation. A bounded worker can
therefore read only dirty work metadata, rebuild those slots, and leave unrelated or unprocessed
slots dirty.

The generation is also an optimistic concurrency token. The current spike deliberately has no lease
or exclusive claim:

- if generation `N+1` arrives while a generation `N` worker is folding, the `N` result may still be
  written, but the slot remains dirty because work is ahead of the projected generation;
- once `N+1` is projected, a late generation `N` worker is rejected and cannot overwrite it;
- the same generation producing two different projection hashes fails closed as nondeterministic.

That means leases are not required for correctness in this model. They could still reduce duplicate
work under contention.

`test_dirty_neo4j.py` covers:

- atomic assertion creation + dirty generation registration;
- idempotent assertion replay not manufacturing new work;
- generation advancement only when genuinely new evidence is created;
- bounded dirty batches that do not clear unprocessed slots;
- generation-backed repair removing a slot from the dirty set;
- a late old worker being unable to overwrite a newer projection.

This experiment also exposed a new extension boundary rather than hiding it: **the assertion write
must know which projection definitions it dirties.** Registering a new View or changing projection
mapping later therefore needs an explicit backfill/reindex mechanism for already-durable assertions.
The current spike does not invent that mechanism yet.

The generational layer currently subclasses the spike Neo4j store and reuses several private helper
functions. If the design survives, those envelope/hash/slot helpers are candidates for promotion into
a supported persistence interface; that coupling is spike debt, not a proposed production API.

The spike-local `:MutationAssertion`, `:MutationProjection`, and `:MutationProjectionWork` labels and
constraints exist only in the throwaway test database. They are intentionally **not** a proposal for
Menhir's production schema.

## What this deliberately does not do

- no production Neo4j schema/repository changes
- no Graphiti changes
- no production extraction changes
- no MCP tools
- no recall integration
- no plugin framework
- no changes to current scalar activation
- no live external LLM call
- no claim that the personality fold math is final personality science
- no claim that the spike-local generic Neo4j schema is production-ready
- no distributed transaction between external source admission and Neo4j assertion persistence
- no lease/exclusive projection-worker claim
- no projection-definition registry migration or historical backfill
- no multi-input projection whose fold consumes several assertion types at once

## Current boundary ledger

Evidence from the spike now supports these narrower claims:

1. **Fold semantics can remain extension-owned.** Personality and production scalar folds coexist
   behind the same outcome envelopes without a shared arithmetic model.
2. **Additional slot axes can remain opaque to core.** `dimensions` is enough for scalar
   `value_kind/unit`, and lifecycle selection can preserve those axes without interpreting them.
3. **Opaque values require an extension codec.** Generic persistence cannot faithfully reconstruct
   `Any` by itself. Injecting the codec keeps this responsibility with the extension.
4. **Namespace can stay below extension semantics.** Storage can isolate identical assertion
   identities in separate silos without changing the assertion contract.
5. **Projection mutability differs from assertion durability.** Replay-safe immutable Assertions and
   disposable current projections need different persistence rules.
6. **Absence has multiple meanings.** Abstention and Retirement must be durable projection outcomes
   rather than both collapsing to "no View", or a stale prior answer can survive incorrectly.
7. **Crash repair does not require domain-specific persistence logic.** Once assertion history is
   durable, a slot can be reselected, supersession applied, the extension fold rerun, and the current
   projection atomically reconciled without the store knowing the domain.
8. **Dirty discovery can be metadata-driven.** Per-slot work generations avoid scanning all assertion
   history just to discover what needs rebuilding.
9. **Correctness does not require an exclusive worker lease in the tested model.** Optimistic
   generations keep newer projections from being overwritten by late older work; leases would be an
   efficiency mechanism.
10. **Projection registration is part of the extension contract.** The system needs to know which
    projection slots a new assertion invalidates, while the meaning and fold remain extension-owned.
11. **Production scalar persistence fits the conceptual contract, but not yet the generic store.**
    Scalar deliberately continues using Menhir's production repositories; replacing those with the
    spike schema would prove something different and is not justified by this experiment.

Still open:

1. Can assertion authority remain generic while admission is extension-owned?
2. Can relationship/person/group scopes flow through production recall without changing recall core?
3. Can coding-specific belief evidence (`TEST_FAILED`, `SOURCE_IS_GIT`, etc.) become registered
   extension signals rather than a closed core enum?
4. What should the projection-definition registry look like, and how does adding/changing a
   projection backfill historical assertions without replaying domain semantics incorrectly?
5. Can one projection safely consume several assertion types, or does the current one-assertion-type
   `ProjectionSlot` need a more general input-set identity?
6. Should abstentions expose exact offending assertion IDs? Production scalar abstention currently
   does not provide them.
7. Are worker leases worth adding purely to reduce duplicate folds under high contention?

## Run the spike tests

From the repository root:

```bash
pytest -q spikes/mutation_kernel
```

The research branch also has a branch-scoped GitHub Actions workflow. It starts a throwaway Neo4j
service and runs only this isolated spike. Graphiti is pinned to Menhir's locked `0.29.2` for
environment parity, although these integration tests do not invoke Graphiti.
