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

`neo4j_store.py` tests a different boundary: can a non-scalar extension persist through Neo4j
without either the store or Menhir production code learning personality concepts?

`Neo4jEnvelopeStore` knows only kernel `Assertion`, `EvidenceRef`, and `View` envelopes. It:

- stores immutable assertions under a namespace-scoped storage identity;
- fingerprint-checks assertion replay and fails closed if the same identity carries a different
  envelope;
- stores one replaceable current View per generic slot
  `(view_type, subject_id, scope, key, dimensions)`;
- keeps namespace isolation in persistence rather than extension semantics;
- delegates opaque `value` serialization to an injected extension codec.

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
checks that assertion history grows while the current View remains one row, checks namespace
isolation, and verifies the experiment created no production `:TypedAssertion` or scalar-state View
nodes.

The spike-local `:MutationAssertion` / `:MutationView` labels and constraints exist only in the
throwaway test database. This is intentionally **not** a proposal to add those labels to Menhir's
production schema.

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

## Current boundary ledger

Evidence from the spike now supports these narrower claims:

1. **Fold semantics can remain extension-owned.** Personality and production scalar folds coexist
   behind the same outcome envelopes without a shared arithmetic model.
2. **Additional slot axes can remain opaque to core.** `dimensions` was enough for scalar
   `value_kind/unit` without scalar vocabulary in the kernel.
3. **Opaque values require an extension codec.** Generic persistence cannot faithfully reconstruct
   `Any` by itself. Injecting the codec keeps this responsibility with the extension.
4. **Namespace can stay below extension semantics.** Storage can isolate identical assertion
   identities in separate silos without changing the assertion contract.
5. **Projection mutability differs from assertion durability.** Replay-safe immutable Assertions and
   disposable replaceable Views need different persistence rules.
6. **Production scalar persistence fits the conceptual contract, but not yet the generic store.**
   Scalar deliberately continues using Menhir's production repositories; replacing those with the
   spike schema would prove something different and is not justified by this experiment.

Still open:

1. Can assertion authority remain generic while admission is extension-owned?
2. Can relationship/person/group scopes flow through production recall without changing recall core?
3. Can coding-specific belief evidence (`TEST_FAILED`, `SOURCE_IS_GIT`, etc.) become registered
   extension signals rather than a closed core enum?
4. What generic lifecycle contract is needed for View retirement/reconciliation and crash repair?
5. Should abstentions expose exact offending assertion IDs? Production scalar abstention currently
   does not provide them.

## Run the spike tests

From the repository root:

```bash
pytest -q spikes/mutation_kernel
```

The research branch also has a branch-scoped GitHub Actions workflow. It starts a throwaway Neo4j
service and runs only this isolated spike. Graphiti is pinned to Menhir's locked `0.29.2` for
environment parity, although these integration tests do not invoke Graphiti.
