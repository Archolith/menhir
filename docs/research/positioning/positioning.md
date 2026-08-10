# Menhir / Archolith positioning

## Status

active

This is the single canonical positioning doc. It consolidates four overlapping
artifacts without losing their content:

```text
.agent/memory-futures.md (CIP vision tag)   -> summarized here, stub kept as a futures pointer
agent-experience-substrate.md               -> superseded by this doc (Lens 2)
cognitive-artifacts-and-software-cognition.md -> superseded by this doc (Lens 3)
cognitive-infrastructure-platform.md        -> superseded by this doc (Lens 1)
```

Each of those was a different lens on the same system. They are folded in below
as Lenses 1–3 under one category statement.

## Promotion condition

Positioning vocabulary stays `active` as the working source of truth. The
*benchable* part — the CIP metrics in Lens 1 — promotes independently: it becomes
`supported-by-eval` when archolith-bench implements a CIP metric beyond recall
(e.g. Decision Accuracy per Retrieved Token, Context Compression Ratio). The
category names promote to product-facing only when they appear in the top-level
`README.md` / product copy / external positioning.

Per the index rule, a concept is "part of menhir" once it has a code surface, a
bench fixture, a metric, or a named failure mode. This doc owns no mechanism — it
maps the ones the per-layer docs own (see "Layer ownership map").

## The category (canonical statement)

Menhir / Archolith is not simply a memory system. The canonical category is
**decided**:

```text
Cognitive Infrastructure Platform (CIP)
```

`Agentic Context Control Plane` is retained as the internal architecture name.
These were considered and set aside as the external category name:

```text
Agent Experience Substrate
Software Cognition Platform
Temporal Cognitive Substrate
Engineering Cognition Platform
Cognitive Substrate for Software Engineering
```

Existing categories each describe only a piece and are rejected as the whole:

```text
AI Memory
Agent Memory
Long-Term Memory
Vector Database
Knowledge Graph
Context Engine
RAG
```

## Three lenses on one system

```text
Lens 1 — Cognitive Infrastructure Platform:
  where the substrate sits in the stack, what primitives it exposes,
  and how success is measured (decision quality per token).

Lens 2 — Agent Experience Substrate:
  the runtime — what an agent experiences as working context.

Lens 3 — Cognitive artifacts / software cognition:
  the accumulation — typed cognitive artifacts (memory is one) built over time.
```

The unifying question all three answer:

```text
How does an AI continuously build, maintain, revise, and explain its
understanding of a software system over time?
```

Everything else is implementation.

---

## Lens 1 — Cognitive Infrastructure Platform (category & architecture)

### Definition

```text
A Cognitive Infrastructure Platform provides the persistent knowledge, temporal
reasoning, structural understanding, provenance, and retrieval primitives
required for autonomous agents to think across sessions, repositories, and long
time horizons.
```

Role analogy:

```text
A CIP is to AI cognition what an operating system is to software.
```

Purpose — not to answer search queries, but to:

```text
minimize the amount of information an agent needs to make the correct decision.
```

(From the original `.agent/memory-futures.md` CIP tag, preserved: "Menhir is a
Cognitive Infrastructure Platform that turns raw episodes into durable memory,
knowledge, constraints, experience records, and reusable behavior for autonomous
agents.")

### Architectural position

The CIP sits above storage technologies and below reasoning models.

```text
Applications
  IDE agents, research agents, robotics, customer support, scientific systems

Reasoning Models
  Claude, GPT, Gemini, Qwen, DeepSeek, ...

Cognitive Infrastructure Platform
  Temporal Memory
  Identity Resolution
  Structure Memory
  Git History
  Belief Tracking
  Contradiction Handling
  Blast Radius Analysis
  Attention Planning
  Reflection
  Memory Consolidation
  Knowledge Versioning
  Provenance
  Context Assembly

Storage Layer
  Postgres, object storage, graph indexes, vector indexes, blob storage
```

### Design philosophy

Each prior category optimizes one axis; the CIP optimizes a different one.

```text
databases          -> retrieval
vector databases   -> similarity
knowledge graphs   -> relationships
CIP                -> cognitive efficiency
```

Primary objective:

```text
Deliver the smallest amount of context required for an agent to make the
correct decision with the highest confidence.
```

This shifts optimization away from latency / nearest-neighbor accuracy toward
reasoning quality.

### Core cognitive primitives

The platform exposes cognitive operations, not storage operations. Higher-level
agents compose these:

```text
Remember()
Recall()
Consolidate()
Reflect()
ResolveIdentity()
DetectContradiction()
MergeBeliefs()
ExplainReasoning()
TraceHistory()
ComputeBlastRadius()
AssembleContext()
PredictRelevantContext()
Forget()
Supersede()
```

### Long-term direction: multiple interacting memory systems

Eventually the platform should support several memory systems rather than one
universal store. Each may evolve independently while sharing common identity,
provenance, and temporal infrastructure.

```text
Episodic Memory
Semantic Memory
Structural Memory
Temporal Memory
Procedural Memory
Reflective Memory
Failure/Friction Memory
Belief Memory
Consolidated Knowledge
```

### Research hypothesis and CIP metrics

```text
Current AI infrastructure optimizes retrieval.
Future cognitive systems will optimize decision quality per token.
```

This implies new, benchable evaluation metrics — the most promotable part of the
positioning, to be specified as archolith-bench metric candidates alongside the
existing recall/precision/temporal metrics:

```text
Context Compression Ratio
Decision Accuracy per Retrieved Token
Explanation Completeness
Temporal Reasoning Accuracy
Provenance Fidelity
Belief Consistency
Contradiction Detection Rate
Cognitive Cost
```

### Positioning line (Lens 1)

```text
Archolith is not simply a memory system. It is a Cognitive Infrastructure
Platform that provides the persistent substrate enabling long-term autonomous
cognition through temporal memory, structural understanding, provenance,
identity resolution, and adaptive context assembly.
```

---

## Lens 2 — Agent Experience Substrate (runtime / control plane)

### Category split

```text
External category:
  Agent Experience Substrate

Internal architecture:
  Agentic Context Control Plane

Implementation:
  Menhir memory, retrieval, belief, temporal, structure, oracle,
  scheduling, and mutation layers
```

Definition:

```text
Menhir is an agent experience substrate:
it shapes the memories, evidence, temporal state, code structure,
belief constraints, and retrieval rails that an AI agent experiences
as working context.
```

An agent never directly experiences the whole repo, chat history, Git history,
issue history, logs, prior failures, user corrections, or evolving beliefs. It
experiences a constructed context window, and Menhir controls that constructed
experience. It decides:

```text
what the agent sees
what the agent does not see
what is current
what is historical
what is contradicted
what is stale but still useful
what evidence is safe to assert
what code structure matters
what prior failures should shape behavior
what retrieval loops should be cooled down
what memories should be treated as dangerous or anergic
what state changes are allowed after retrieval
```

Positioning line (Lens 2):

```text
Menhir is an agent experience substrate, not just a memory system.
It governs what memories, evidence, temporal state, code structure,
and belief constraints enter an agent's working context.
```

### System hierarchy

```text
Archolith:
  broader agentic context infrastructure

Menhir:
  agent experience substrate / agentic context control plane

Chronostratum:
  temporal memory layer

Retrieval tuning stack:
  candidate generation and semantic tuning knobs

Facet retrieval:
  deterministic structured candidate generation

Oracle layer:
  fine-grained evidence evaluation

Oracle scheduler:
  performance control for oracle fan-out

Oracle combiner:
  deterministic reducer over oracle evidence

BeliefLayer:
  assertion policy and belief/currentness classification

SelfReinforcementGuard:
  anti-spiral and productive-recency rails

Mutator layer:
  serialized state-changing operations (the write boundary)
```

### Operating principle

```text
Retrieval is not just search.
Retrieval is a control system.
```

Anti-pattern:

```text
semantic similarity
-> top-k
-> context
```

The Menhir direction:

```text
candidate generation from many paths
-> oracle evaluation
-> role-specific scoring
-> belief/currentness gates
-> context packet
```

Semantic ranking is demoted, not replaced — it becomes one evidence signal
(`SemanticOracle`), not the retrieval authority:

```text
Semantic ranking finds what sounds related.
Lexical/facet/graph retrieval finds what is explicitly connected.
Oracles decide what is temporally, structurally, evidentially,
and belief-wise usable.
BeliefLayer decides what can safely be asserted.
```

---

## Lens 3 — Cognitive artifacts & software cognition (accumulation)

### The reframe

```text
Most AI memory systems optimize for remembering.
Menhir should optimize for understanding.
```

Memory is one artifact produced by a larger cognitive process: a system that
continuously observes engineering activity, extracts understanding, and
accumulates structured knowledge over time. Competitive note:

```text
Competitors are rapidly building "memory."
Very few are building continuous software understanding.
```

### Memory is an artifact

A session should generate many kinds of cognitive artifacts, each answering a
different question:

```text
Memories
Decision Frames
Beliefs
Evidence
Architecture understanding
Friction
Risk estimates
Structural reputation
Replay traces
Identity links
```

Memory is one projection of the underlying cognition, not the whole thing.

### Cognitive artifacts as the core abstraction

```text
Every FATES lens emits artifacts.
Artifacts are first-class graph objects.
```

Shared artifact properties:

```text
provenance
confidence
temporal validity
attached structure
evidence
contradictions
supersession history
```

This abstraction scales without redesign: a new lens simply produces a new
artifact type — no schema rewrite, no new subsystem.

```text
Design principle:
  Do not ask "What else should Menhir remember?"
  Ask "What other kinds of understanding can be distilled from
       engineering experience?"
```

### FATES as lenses (scientific-instrument framing)

```text
Scientists observe reality through different lenses.
Each lens measures different phenomena.
The observations combine into a unified model.

FATES plays the role of those instruments.
Each lens observes software engineering from a different perspective.
Together they construct an evolving cognitive model of the software system.
```

The lenses that together answer the unifying question:

```text
Chronostratum
Structure Graph
Git Graph
Decision Frames
Dream
Friction
Beliefs
Identity
Evidence
Replay
```

### Long-term vision

A mature Menhir answers "What do I understand?" rather than "What do I
remember?" — and can explain:

```text
why that understanding exists
what evidence supports it
how confident it is
when it became true
whether it is still true
what changed
which engineering decisions were affected
where in the codebase the understanding applies
```

That is strictly more than memory retrieval.

---

## Naming reconciliation (decided)

```text
FATES = lens (decided):
  "FATES" / "Fates" name the observational lenses that EMIT cognitive artifacts
  (Lens 3). The WRITE/mutation boundary is the Mutator — never "Fate".
  Pipeline: Oracles observe -> Combiners decide -> Mutators write.
  FATES sits at the observe end; the Mutator sits at the write end.
```

```text
Category name = CIP (decided):
  "Cognitive Infrastructure Platform" is the canonical external category;
  "Agentic Context Control Plane" is the internal architecture name. The
  set-aside alternatives are recorded under "The category" above.
```

## Layer ownership map

This doc owns the category, the lenses, the system hierarchy, and the
operating/design principles. Each layer's mechanism is owned elsewhere:

```text
Retrieval tuning stack:
  retrieval-tuning-stack.md

Facet retrieval:
  facet-retrieval.md

Oracle layer / combiner / amplification:
  oracle-amplified-retrieval.md

Oracle scheduler / SelfReinforcementGuard:
  retrieval-control-rails.md

Oracle execution / write boundary / performance / candidate priors:
  oracle-execution-and-performance.md

BeliefLayer:
  belief-layer.md

Chronostratum temporal layer / connected-data lanes:
  connected-data-substrates.md, tracehead-braidtrace.md

Eval harness and CIP metrics home:
  archolith-bench-operational-model.md, research-process.md
```

## Non-goals

Do not:

```text
treat any category name as a built subsystem
re-document oracle/tuning/rails/belief mechanisms here; link to their owner docs
treat the cognitive primitives as a built API before one is named in code
ship the CIP metrics as "results" before archolith-bench implements them
split memory into nine independent stores before a shared identity/provenance/
  temporal spine exists
conflate FATES (the observe-side lenses) with the Mutator (the write boundary)
let the positioning multiply back into several overlapping docs — update this
  one doc instead
```
