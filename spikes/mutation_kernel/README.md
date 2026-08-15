# Menhir Mutation Kernel Spike

**Status:** isolated research spike.  
**Branch:** `research/mutation-kernel-spike`  
**Base:** `13143d8a7ef5bfb9198db48895d55c7147f43c42`  
**Production impact:** none. Nothing under this directory is imported by `src/menhir`.

## Question

Can Menhir's durable memory/governance machinery become a domain-neutral substrate where coding is
an extension rather than a core assumption?

This spike uses **personality and learned behavior** as the first hostile/non-coding reference
domain. It is not an attempt to turn Menhir into a chatbot. The personality extension exists to
force useful abstraction boundaries.

## Hypothesis

The reusable kernel is smaller than Menhir itself:

```text
Evidence
  -> immutable Assertion
  -> explicit supersession/current set
  -> domain-owned Fold
  -> rebuildable View
```

The kernel should own identity, provenance, time, authority and supersession. An extension should
own value semantics, admission, fold rules, relationship semantics and rendering.

## Experiment 1 — personality

`kernel.py` provides domain-neutral:

- `EvidenceRef`
- source-grounded identity (`source_key`)
- immutable `Assertion`
- explicit supersession
- weakest-contributor authority
- `View` / `Abstention`
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
policy defining which group scopes are appropriate to learn and what evidence is required.

## Experiment 2 — production scalar compatibility

`scalar_adapter.py` asks the harder question: can Menhir's existing typed-scalar machinery fit the
same kernel without rewriting or weakening its semantics?

The adapter deliberately delegates all scalar meaning to production Menhir:

```text
frozen model response
  -> production extract_typed_scalars_once
  -> production gate_typed_scalars
  -> production bind_and_persist_typed_scalars
  -> production TypedAssertion
  -> production fold_assertions
  -> kernel View | Abstention | Retirement
```

The external LLM and durable repository are injected seams in the fixture test. The parser, gate,
binding logic, assertion construction, temporal/reference-time handling and scalar fold are the real
Menhir implementations. This is therefore a deterministic ingest-contract test, **not** a claim that
a live model or Neo4j-backed ingest was executed.

This second domain exposed two useful kernel corrections:

1. **Extension-owned slot dimensions.** Scalar identity includes `value_kind` and `unit` in addition
   to subject/attribute/scope. The kernel now preserves arbitrary named dimensions without knowing
   what they mean. Personality currently needs none.
2. **Retirement is not abstention.** Scalar `expire` means the prior value is known to have ended and
   there is intentionally no current View. That is materially different from being unable to decide
   current state, so the kernel now has a `Retirement` outcome beside `View` and `Abstention`.

Blank scope is also valid: production scalars use `scope=""`, so the kernel no longer requires every
domain to invent a synthetic scope name.

## Fixture coverage

`fixtures/scalar_ingest_fixture.json` carries raw episode prose plus frozen three-sample model
captures. Tests cover:

- absolute + later delta (`10 + 3 -> 13`) through the real deterministic ingest boundary;
- unanimous gate commit and canonical-self binding;
- conflicting k-sample interpretations failing closed;
- weakest-contributor authority preserved by the adapter;
- equal-time conflicting absolutes remaining an abstention;
- explicit expiry becoming retirement rather than an abstention;
- scalar `value_kind`/`unit` remaining part of slot identity;
- input-order invariance of the production fold and adapted output.

## What this deliberately does not do

- no Neo4j schema or repository changes
- no Graphiti changes
- no production extraction changes
- no MCP tools
- no recall integration
- no plugin framework
- no changes to current scalar activation
- no live external LLM call in the fixture test
- no claim that the proposed personality fold math is final personality science

Those would contaminate the audit/remediation surface before this experiment has earned them.

## Evaluation rule

The useful output is **not whether this toy personality model is good**. The useful output is a
ledger of what an extension cannot express without changing the kernel.

Current seams to test after the audit:

1. Can assertion authority remain generic while admission is extension-owned?
2. Can Views expose exact contributors/counterevidence without knowing their domain?
3. Can relationship/person/group scopes be represented without changing recall core?
4. Can coding-specific belief evidence (`TEST_FAILED`, `SOURCE_IS_GIT`, etc.) become registered
   extension signals rather than a closed core enum?
5. Can production scalar persistence/rebuild plug into the generic contract without an adapter
   needing repository-specific knowledge?
6. Should abstentions expose their exact offending assertion IDs? Production scalar abstention
   currently carries the slot/reason but not those IDs, so the adapter intentionally cannot invent
   them.

## Run the spike tests

From the repository root:

```bash
pytest -q spikes/mutation_kernel
```

The research branch also has a branch-scoped GitHub Actions workflow that runs only this isolated
spike. It exists so the production Menhir imports can be exercised even when the local ChatGPT shell
cannot resolve GitHub or install the repository.
