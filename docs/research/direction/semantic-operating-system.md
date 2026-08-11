# Menhir as a Semantic Operating System for Software

## Status

active

Research direction and architectural synthesis.

> **Build-status note (2026-07-11) — vision vs. current code.** This is the target architecture, not
> current state. Where it stands on `main`: **Program A** (deterministic structural foundation) is
> shipped (`ingest_project`, `structural_anchoring`, `structure_queries`). **Program E**'s oracle
> pipeline/combiner is **built but benched neutral-to-negative on LongMemEval** and ships default-off
> (`config/settings.py` frontier_* all False); the ColdStartBrief/context-assembly half depends on the
> L3/L4 GAP and is unbuilt. **Programs B/D** (L3/L4 semantic + institutional overlay) remain design-only
> (schemas exist, unsequenced — the "GAP"). The **current active build direction is write-time
> consolidation** (Track W in `.agent/research/menhir-research-execution-ladder.md`): maintain
> query-sufficient state at write time (D0 entropy, D1 QuantState, Event -> Fold -> View,
> agent-counters — all built and measured; live shape in `.agent/architecture.md`).
> That is the first concrete instantiation of **Program C** (knowledge evolution over time), reached
> after the read-side retrieval levers were exhausted. The vision below stands; only the near-term build
> order changed.

This document narrows the earlier graph-primary / semantic-program-graph research into a buildable direction for Menhir. It does not propose replacing programming languages, compilers, Git, or Unison. Instead, it proposes that Menhir become a semantic operating system layered over deterministic code structure.

The core shift is:

> Do not make Menhir a compiler replacement. Make Menhir the system that manages what humans and AI agents know about software over time.

---

## Executive Thesis

Modern codebases suffer from more than technical debt.

They also accumulate:

- **Cognitive debt** — the team's shared understanding erodes.
- **Intent debt** — the rationale, business rules, and constraints behind code disappear.
- **Memory debt** — incidents, failed approaches, and design lessons are scattered across chats, tickets, docs, and people's heads.

Source code is a lossy compression of intent. It tells us what executes, but not reliably why it exists, what assumptions shaped it, what decisions it superseded, or what failures taught the team to avoid certain paths.

Menhir's opportunity is to preserve that missing layer.

---

## Four-Layer Architecture

```text
Layer 4: Institutional Knowledge
  Design rationale
  Production incidents
  Failed approaches
  Architectural discussions
  Performance history
  Human reviews
  AI discoveries

Layer 3: Semantic Model
  Capabilities
  Policies
  Constraints
  Invariants
  Decisions
  Business rules

Layer 2: Structural Model
  Content-addressed definitions
  Types
  Dependencies
  Symbols
  Modules
  Hashes

Layer 1: Source Code
  Python
  TypeScript
  Java
  Rust
  Unison
  Other ordinary source files
```

The distinction between Layer 3 and Layer 4 is important.

Layer 3 models what the software means.

Layer 4 models what the organization has learned about that software over time.

---

## Structural Truth vs. Semantic Truth

Menhir should keep a hard boundary between deterministic structure and probabilistic semantic interpretation.

### Structural truth

Structural truth is deterministic, compiler-verifiable, or directly derived from source.

Examples:

- function identity
- file ownership
- dependency edges
- type information
- symbol references
- test locations
- commit history

Structural truth should never depend on an LLM.

### Semantic truth

Semantic truth is interpretive, evidence-backed, and reviewable.

Examples:

- this function implements rate limiting
- this branch enforces unpaid-user export restrictions
- this decision replaced an older billing policy
- this optimization was rejected because it caused a production regression
- this capability exists because of a customer requirement

Semantic truth can begin as an AI-generated hypothesis, but it should not silently become fact.

Every semantic assertion should carry metadata:

```text
confidence
origin
supporting evidence
review status
valid time
supersession state
```

This prevents Menhir from becoming an undocumented second codebase.

---

## Evidence as a First-Class Entity

A semantic overlay should not merely connect capabilities directly to functions.

Instead, Menhir should model evidence explicitly.

```text
Capability
  supported_by -> Evidence

Evidence
  derived_from -> Function
  derived_from -> Test
  derived_from -> Commit
  derived_from -> Incident
  derived_from -> ADR
  derived_from -> Conversation
  derived_from -> Benchmark
```

This allows Menhir to answer:

> Why do we believe this function enforces GDPR?

The answer should be a chain of evidence, not "because an LLM said so."

---

## Knowledge Promotion Lifecycle

Menhir should treat semantic claims as knowledge that matures over time.

```text
Observation
  -> Candidate Knowledge
  -> Evidence Collection
  -> Human or Agent Review
  -> Trusted Knowledge
  -> Deprecated / Superseded / Historical
```

This applies to:

- capabilities
- policies
- invariants
- architectural decisions
- failure memories
- performance assumptions
- security assumptions
- agent-generated discoveries

The system should allow low-confidence semantic hypotheses without polluting trusted knowledge.

> Prior-art note (2026-06-28 audit): this lifecycle is **partially realized today**, spread across
> existing fields rather than a single `status` enum — `scope` (`CANDIDATE` review tier → `PERSISTENT`
> → `PROMOTED`), `source_confidence` (LLM-inferred 0.5 vs user 0.9/1.0), `conflict_status`, and
> `freshness`. The L3/L4 build reuses this substrate (see `docs/research/schemas/layer4-knowledge-artifacts.md`
> "Prior art in menhir"); what's new is the institutional/semantic *types* and a first-class `Evidence`
> node, not the promotion machinery.

---

## Temporal Semantics

Most semantic code systems are static. Menhir should be temporal by default.

Questions Menhir should eventually answer:

- When did this capability appear?
- When did this policy stop applying?
- What assumptions were true before commit X?
- Which architectural decision superseded this one?
- What business rule caused this function to evolve?
- What failed approach should future agents avoid repeating?

Temporal semantics turn documentation into institutional memory.

A semantic node should support validity windows:

```text
valid_from
valid_to
learned_at
superseded_by
invalidated_by
confidence_over_time
```

---

## Relationship to Unison

Unison is a strong candidate for a Layer 2 structural substrate because it provides:

- content-addressed definitions
- immutable code identity
- dependency-aware structure
- name layers over hashes
- type-aware update workflows
- MCP tooling for agent access

Menhir should not rebuild those capabilities unless necessary.

Instead, Menhir should treat Unison as one possible structural backend.

```text
Menhir Structural Backend
  File/AST backend
  Tree-sitter backend
  Git backend
  Unison backend
```

For ordinary repositories, Menhir can build a best-effort structural model from files, ASTs, symbols, tests, and Git history.

For Unison projects, Menhir can use Unison's stronger identity and dependency guarantees.

The long-term goal is not to force all projects into Unison. The goal is to let Menhir use the strongest available structural substrate for each project.

---

## Semantic Overlay, Not Compiler Replacement

Earlier research considered storing software directly as semantic program graphs and compiling source code as a projection.

That remains a possible long-term frontier, but it is too risky as the first implementation path.

The slimmer direction is:

```text
Source code
  -> deterministic structural model
  -> semantic overlay
  -> temporal institutional memory
```

Only after semantic knowledge becomes rich and trustworthy should Menhir explore semantic-first authoring.

For now:

> Source remains executable truth. Structure provides deterministic anchors. Semantics and memory accumulate above it.

---

## Oracle-Driven Cold Start Context

Layer 4 should not be treated as a pile of memories that are blindly retrieved into a model context window.

Layer 4 is the accumulated institutional knowledge substrate. The Oracle layer is the reasoning engine that interprets it.

```text
Task arrives
  -> deterministic structural pass
  -> oracle pass over Layers 2/3/4
  -> oracle combiner
  -> cold start brief
  -> context engine packages the final context
  -> agent begins work
```

This separates Menhir's responsibilities cleanly:

- **Layer 4 stores institutional knowledge.**
- **Oracles interpret that knowledge for a task.**
- **The combiner separates facts from hypotheses.**
- **The context engine packages the result for the model.**

The context engine remains essential, but it should be downstream of oracle reasoning.

The context engine answers:

> What should fit into the model context?

The oracle system answers first:

> What does this task mean, what risks matter, what history matters, and what evidence should be considered?

---

## Deterministic Core, LLM Interpretation

The cold-start pipeline should be both deterministic and LLM-assisted, with a hard boundary between the two.

### Deterministic pass

The deterministic pass finds facts.

Examples:

- touched symbols and files
- dependencies and dependents
- tests connected to affected code
- commits and temporal windows
- known semantic nodes anchored to structural entities
- attached memories and evidence
- structural distance and graph proximity

This pass should not depend on an LLM.

### Oracle pass

Specialized oracles inspect relevant knowledge.

Candidate oracles:

- StructureOracle
- DecisionOracle
- FailureOracle
- IncidentOracle
- AssumptionOracle
- TemporalOracle
- EvidenceOracle
- BeliefOracle
- TestOracle

Each oracle returns evidence and findings, not final instructions.

### LLM interpretation pass

An LLM may summarize and interpret findings.

Examples:

- explain why a prior decision matters
- infer a likely missing capability label
- propose risks
- summarize a production incident's relevance
- suggest first investigative actions

LLMs may interpret and propose. They should not silently determine truth.

### Combiner output

The combiner should produce an evidence-first cold start brief with explicit sections:

```text
Known facts
Likely interpretations
Open questions
Relevant risks
Evidence links
Recommended context pack
```

This lets Menhir say:

> Here is what we know, how we know it, what we suspect, and what needs review.

---

## Cold Start Brief

The practical artifact produced by the Oracle + Context Engine pipeline is a Cold Start Brief.

A Cold Start Brief should include:

- what this area does
- active capabilities and policies
- relevant decisions
- known failed approaches
- production incidents or regressions
- assumptions currently believed true
- tests protecting the behavior
- risky dependencies
- open contradictions or stale beliefs
- recommended first files or symbols
- recommended commands or tests to run

This is the agent-facing payoff of Layer 4.

Before an agent changes code, Menhir should provide not only snippets, but also the hard-won institutional knowledge that prevents repeat mistakes.

---

## Core Research Programs

### Program A: Deterministic Structural Foundation

Goal: establish stable anchors for code knowledge.

Work items:

- ingest source files
- extract symbols, types, functions, tests, and dependencies
- integrate Git history
- experiment with Unison as a high-trust backend
- assign stable structural identities where possible

### Program B: Semantic Understanding

Goal: identify what code means at the architecture and domain level.

Work items:

- infer candidate capabilities
- extract constraints and policies
- identify decision points
- connect tests to business rules
- generate semantic hypotheses from code, docs, commits, and conversations

### Program C: Knowledge Evolution

Goal: manage semantic claims over time.

Work items:

- evidence tracking
- confidence scoring
- review states
- contradiction detection
- supersession handling
- valid-time and learned-time modeling

### Program D: Institutional Memory

Goal: preserve what teams and agents learn while working on the system.

Work items:

- attach design rationale to semantic nodes
- link incidents and regressions to affected capabilities
- store failed approaches
- remember why prior fixes were rejected
- preserve agent discoveries across sessions
- surface relevant memory during coding tasks

### Program E: Oracle-Driven Context Assembly

Goal: turn structure, semantics, time, and memory into evidence-first task briefs.

Work items:

- define oracle interfaces
- build deterministic retrieval passes
- separate known facts from LLM hypotheses
- implement oracle combiner
- generate Cold Start Briefs
- connect briefs to the existing context engine

---

## Differentiators

Menhir is not merely a code graph, vector memory, or documentation system.

Its differentiator is the combination of:

1. deterministic structural anchors
2. semantic understanding
3. temporal evolution
4. durable institutional memory
5. oracle-driven reasoning
6. AI-agent-accessible context packaging

Most existing systems stop at one or two of these layers.

Menhir should attempt to unify them.

---

## Example Query Targets

Future Menhir should support queries like:

```text
What code enforces the rule that unpaid users cannot export reports?
```

```text
When did this capability begin requiring authentication?
```

```text
What tests prove this policy is enforced?
```

```text
What production incidents affected this capability?
```

```text
What failed approaches should an agent avoid before changing this module?
```

```text
Which architectural assumptions were true before the billing rewrite?
```

```text
What should an agent know before changing this capability?
```

These queries require structure, semantics, time, memory, and oracle reasoning together.

---

## Build Direction

### Phase 1: Structural Anchors

Build or refine the structural ingestion layer.

Minimum output:

- symbols
- files
- functions/classes
- dependencies
- tests
- commit references

### Phase 2: Semantic Candidates

Use LLM-assisted analysis to propose semantic nodes.

Candidate node types:

- Capability
- Policy
- Constraint
- Invariant
- Decision
- FailureMemory
- IncidentMemory

All generated nodes begin as untrusted or low-confidence.

### Phase 3: Evidence Graph

Add evidence edges from semantic nodes to structural facts, tests, commits, incidents, and docs.

No semantic claim should be trusted without evidence.

### Phase 4: Temporal Knowledge

Add validity windows, supersession, contradiction handling, and confidence-over-time.

### Phase 5: Agent Recall

Use the semantic operating system during coding sessions.

The agent should receive not only relevant files, but also:

- related capabilities
- active policies
- prior decisions
- failed approaches
- known pitfalls
- incident history
- tests protecting the behavior

### Phase 6: Oracle Cold Start Briefs

Build the oracle pipeline that produces a Cold Start Brief before agent work begins.

Minimum output:

- known facts
- likely interpretations
- open questions
- relevant risks
- evidence links
- recommended context pack

---

## Non-Goals

Menhir should not initially attempt to:

- replace programming languages
- compile code from pure semantic graphs
- require Unison adoption
- trust LLM-generated semantics automatically
- treat line numbers as durable anchors
- store memory without provenance
- confuse semantic hypotheses with verified truth
- let retrieval alone imply truth
- let LLM interpretation silently overwrite deterministic facts

---

## One-Sentence Summary

Menhir should become a semantic operating system for software: a temporal, evidence-backed, oracle-driven, AI-accessible knowledge layer that preserves what code means, why it exists, how it evolved, and what teams have learned about it, while relying on deterministic structural substrates for truth anchoring.
