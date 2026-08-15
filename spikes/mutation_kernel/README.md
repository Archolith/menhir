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

## First experiment

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

## What this deliberately does not do

- no Neo4j schema or repository changes
- no Graphiti changes
- no production extraction changes
- no MCP tools
- no recall integration
- no plugin framework
- no changes to current scalar activation
- no claim that the proposed fold math is final personality science

Those would contaminate the audit/remediation surface before this experiment has earned them.

## Evaluation rule

The useful output is **not whether this toy personality model is good**. The useful output is a
ledger of what the extension cannot express without changing the kernel.

Initial seams to test against Menhir after the audit:

1. Does assertion identity need to know domain value kinds?
2. Can authority remain generic while admission is extension-owned?
3. Can Views expose exact contributors/counterevidence without knowing their domain?
4. Can relationship/person/group scopes be represented without changing recall core?
5. Can coding-specific belief evidence (`TEST_FAILED`, `SOURCE_IS_GIT`, etc.) become registered
   extension signals rather than a closed core enum?
6. Can the current scalar fold remain one extension of a more general assertion/fold contract
   without weakening its deterministic guarantees?

## Run the spike tests

From the repository root:

```bash
pytest -q spikes/mutation_kernel/test_personality.py
```
