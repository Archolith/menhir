# Connected-data substrates beyond graphs for Chronostratum

## Status

speculative

## Promotion condition

Promote a lane to active only when a menhir spike or archolith-bench fixture shows it improves temporal blast radius, belief drift reasoning, or structure-aware recall compared to a Neo4j/Graphiti baseline. Do not replace Neo4j/Graphiti until a lane earns that comparison.

## Purpose

Chronostratum is currently framed around Graphiti/Neo4j plus temporal metadata. Menhir's differentiator is structure-aware temporal memory for coding agents. The key capability is not "memory with dates" — it is temporal blast radius:

```text
Given a failure and a known-good point, compute the structural dependency cone
and intersect it with Git/code changes between the good and bad states.
```

Killer query:

```text
Test C failed. What does it depend on, and which of those changed between when it last passed and now?
```

This document tracks frontier connected-data representations that go beyond ordinary binary graph edges, and assesses which might support that query better.

## Working thesis

Do not replace Neo4j/Graphiti immediately. Use them as the inspectable substrate. Research a sidecar reasoning layer for richer connectedness:

```text
typed hyperevents for memory/event capture
Datalog or differential-dataflow views for temporal blast-radius reasoning
semiring provenance for explainable suspect ranking
sheaf-style consistency checks for contradictions across local code/memory contexts
tensor/factorized representations where temporal/code/belief dimensions are first-class axes
```

## Research lanes

### 1. Hypergraphs / n-ary knowledge representation

**Why it matters:** Debugging memories are n-ary, not binary. A patch event bundles user, agent, commit, file, symbol, failing test, belief, timestamp, and evidence into one hyperedge.

Menhir application:

```text
(:HyperEvent:PatchObservation) = {
  episode, commit_or_snapshot, file, symbol, test,
  belief, valid_time, learned_time, evidence
}
```

Papers:

- HyperGraphRAG: RAG with Hypergraph-Structured Knowledge Representation — arxiv 2503.21322
- Two-dimensional Taxonomy for N-ary Knowledge Representation Learning — arxiv 2506.05626
- A Survey of Link Prediction in N-ary Knowledge Graphs — arxiv 2506.08970

### 2. Topological data structures

**Why it matters:** Simplicial complexes, cell complexes, and combinatorial complexes represent higher-order structure beyond pairwise edges — useful for modeling a whole failure situation as a structured object.

Menhir application:

```text
FailureComplex(TestC, BadCommit) = {
  test, call tree, touched symbols, config,
  fixtures, dependency versions, observed failure, belief state
}
```

Sources: Topological deep learning overview and domain taxonomy (Wikipedia).

Likely research-only. May provide vocabulary for dependency cones and grouped failure contexts.

### 3. Cellular sheaves / heterogeneous sheaf models

**Why it matters:** Sheaves allow different local regions to carry different data spaces, with restriction maps defining how local stories should agree. Useful for detecting contradiction across layers.

Menhir application:

```text
Git local state says:       Symbol X changed.
Structure local state says: Test C depends on X.
Episode local state says:   Agent believed unrelated File Y caused failure.
Sheaf check says:           These local explanations do not glue cleanly.
```

Papers:

- Heterogeneous Sheaf Neural Networks — arxiv 2409.08036
- Sheaf Theory through the Lens of Deep Learning — arxiv 2502.15476

### 4. Datalog / incremental relational logic / differential dataflow

**Why it matters:** Temporal blast radius is rule-shaped. We need derived facts like `suspect(symbol)` from `depends(test, symbol)` and `changed_between(symbol, good, bad)`.

Rule sketch:

```prolog
depends_on_test(Test, Symbol) :- calls(Test, Function), reaches(Function, Symbol).
changed_in_window(Symbol)     :- changed_between(Symbol, GoodCommit, BadCommit).
suspect(Symbol)               :- depends_on_test(Test, Symbol), changed_in_window(Symbol).
```

Menhir application: Neo4j as the inspectable store; Datalog or differential-dataflow sidecar for recursive/temporal derivations.

Papers:

- FlowLog: Efficient and Extensible Datalog via Incrementality — arxiv 2511.00865
- A Differential Datalog Interpreter — arxiv 2308.04214
- Incremental Maintenance of DatalogMTL Materialisations — arxiv 2511.12169

### 5. Semiring provenance

**Why it matters:** Menhir needs to explain *why* a symbol was suspected or recalled. Semiring provenance represents derivation paths algebraically instead of relying on LLM explanation alone.

Example:

```text
suspect(TreeWillowPatch)
=
changed(TreeWillowPatch, commit_A)
× depends(Test_C, TreeWillowPatch)
× failed_after(Test_C, commit_A)
× mentioned_in(Episode_17)
```

Papers:

- Provenance semirings — dl.acm.org/doi/10.1145/3034786.3056125
- Dagstuhl Seminar 25081: Semirings in Databases, Automata, and Logic

### 6. Tensor / factorized representations

**Why it matters:** Chronostratum has dimensions that look more like tensor axes than graph annotations:

```text
Fact[entity, relation, target, valid_time, belief_time, source, confidence]
Changed[symbol, commit, file, author, time]
Depends[test, symbol, depth, confidence]
```

Menhir application: sidecar representation for slicing across time/code/belief/source dimensions when temporal blast-radius graph traversals get expensive.

Papers:

- Representing and Querying Data Tensors in RDF and SPARQL — arxiv 2504.19224

### 7. Probabilistic circuits / belief models

**Why it matters:** Extraction reliability is the gating risk. Menhir should not always promote extracted beliefs to hard facts.

Menhir application: track uncertain beliefs and contradiction/supersession with confidence. This lane is already partially implemented — see `docs/research/belief-layer.md` and `src/menhir/domain/belief.py`.

Papers:

- Probabilistic Circuits for KG Completion with Reduced Rule Sets — arxiv 2508.06706
- Tractable Representation Learning with Probabilistic Circuits — arxiv 2507.04385
- Probabilistic Graph Circuits — arxiv 2503.12162
- Sparse Probabilistic Graph Circuits — arxiv 2508.07763
- Scaling Tractable Probabilistic Circuits (PyJuice) — arxiv 2406.00766

### 8. Category-theoretic / functorial data integration

**Why it matters:** The hard problem in temporal blast radius is identity reconciliation across schemas: Graphiti Entity, Git Commit, Code Structure File/Symbol/Test/Dependency, Episode/FactState, TestRun.

Menhir application: probably not a storage engine. More likely a formal vocabulary for schema mapping and identity preservation across layers.

## Research scout

A `ResearchScout` / `FrontierScout` skill should periodically pull and classify papers across these lanes.

### Sources

```text
arXiv: cs.DB, cs.AI, cs.LG, cs.PL, cs.SE, cs.LO, stat.ML, math.CT
Semantic Scholar, DBLP, OpenReview, Papers With Code, Crossref
Venues: SIGMOD, VLDB, PODS, NeurIPS, ICML, ICLR, KDD, WWW, WSDM, KR, IJCAI, AAAI, ISWC, ESWC, PLDI, OOPSLA
```

### Per-paper metadata to extract

```text
title, authors, date, venue/source, arXiv id / DOI, code link
formal object: graph / hypergraph / tensor / sheaf / Datalog / semiring / category / circuit
connectedness type: pairwise / n-ary / topological / probabilistic / algebraic / temporal
temporal support: none / timestamps / intervals / partial order / streaming updates
provenance support: none / source citation / derivation trace / algebraic provenance
query mechanism: traversal / logic rules / tensor contraction / neural inference / factorized join
Menhir relevance: low / medium / high
Why it matters: 2-4 sentence summary
```

### Output per run

```text
1. New papers worth reading
2. Frontier shifts / trends
3. Direct menhir applications
4. "Steal this idea" notes
5. Papers to ignore and why
```

## Implementation order

Do not derail Rung 1. Keep Chronostratum implementation order:

```text
Rung 1: surface Graphiti bitemporal/transactional edge state
Rung 2: temporal-aware recall
Rung 3: ingestion-order independence
Rung 4.5: Git/Structure Time Join
Rung 5: temporal blast-radius over shared File/Symbol/Test/Dependency identity
```

Start the research scout in parallel so Rung 5 can build on the frontier instead of reinventing old graph-RAG patterns.

## Success criterion

This research is useful only if it improves:

```text
What changed inside this failing test/file/symbol's dependency cone between the last
known-good state and the current broken state, and what did we believe at each point?
```

If a representation does not help answer that better, faster, or more explainably, it stays research-only.

## Source

Issue #9.
