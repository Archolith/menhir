# Weekend Roadmap — Oracle Runtime Work While Embedder Is Blocked

## Status

Short-term roadmap for the three-day window before a real embedder is available.

The current R2 facet benchmark has been human-hardened and is a good candidate checkpoint. The next promotion decision is blocked on a real embedder and live graph. This roadmap focuses on work that will remain valuable regardless of which embedder wins.

---

## Guiding Principle

Do not tune retrieval around a missing component.

Use the embedder wait time to build the architecture that retrieval will eventually feed:

> the Oracle Runtime, Layer 4 knowledge model, and cold-start briefing system.

---

## What Is Already In A Good Resting State

The current R2 benchmark work has established:

- fixture validator
- draft 50/20 fixture
- real-grounded stale, rename, wrong-repo, vague, and belief-drift cases
- strong lexical baseline
- gold-facet improvements on wrong-scope and stale metrics
- honest extracted-facet collapse exposing the next bottleneck

This is enough to pause benchmark expansion until the embedder arrives.

---

## Weekend Priority 1 — Oracle Runtime Interfaces

Goal: define the runtime shape before implementing individual oracles.

> **Drafted:** the interface spec for this priority now lives in
> `docs/research/retrieval/oracle-runtime-interfaces.md` — OracleInput/OracleFinding schema, primitive/composite
> taxonomy, combiner responsibilities, deterministic-vs-LLM boundary, and the reconciliation with the
> retrieval-level RetrievalOracle/combiner in `oracle-amplified-retrieval.md`. Spec only, no code; the
> composite/Cold-Start-Brief layer it defines is the L3/L4 GAP, pending ctharvey's sequencing.

The Oracle Runtime should answer:

- How are oracles scheduled?
- What does an oracle receive?
- What does an oracle return?
- How are facts separated from hypotheses?
- How are oracle outputs merged?
- How are findings cached?
- How does an oracle hand evidence to the Context Engine?

Suggested interface shape:

```text
OracleInput
  task
  structural_context
  semantic_context
  institutional_knowledge
  budget
  run_mode

OracleFinding
  oracle_name
  finding_type
  summary
  facts
  hypotheses
  evidence
  confidence
  risk
  open_questions
  suggested_context
```

Important rule:

> Oracles observe and explain. They do not mutate.

---

## Weekend Priority 2 — Oracle Taxonomy

Goal: separate primitive oracles from composite oracles.

### Primitive Oracles

Primitive oracles read one class of evidence.

Examples:

- StructureOracle
- GitOracle
- TestOracle
- MemoryOracle
- SemanticNodeOracle
- TemporalOracle
- EvidenceOracle

### Composite Oracles

Composite oracles synthesize primitive findings for a task.

Examples:

- ColdStartOracle
- RiskOracle
- RefactorOracle
- DebugOracle
- PlanningOracle
- StaleKnowledgeOracle

This prevents every future feature from becoming a one-off subsystem.

---

## Weekend Priority 3 — Cold Start Brief Spec

> **Drafted (Day 2):** the concrete schema + assembly + context-pack provenance rules now live in
> `docs/research/schemas/cold-start-brief.md`. Spec only; the producing ColdStartOracle is the unsequenced GAP.


Goal: define the artifact Menhir should produce before an agent begins work.

A Cold Start Brief should include:

- task summary
- relevant files and symbols
- active capabilities and policies
- current assumptions
- prior architectural decisions
- failed approaches to avoid
- related incidents or regressions
- tests protecting behavior
- stale or contradicted knowledge
- risky dependencies
- recommended first actions
- recommended context pack

The brief should distinguish:

```text
Known Facts
Trusted Knowledge
Likely Interpretations
Open Questions
Risks
Evidence Links
Recommended Context
```

This becomes the end-to-end target for the Oracle + Context Engine pipeline.

---

## Weekend Priority 4 — Layer 4 Knowledge Artifact Schema

> **Drafted (Day 2):** the concrete generic-artifact schema + promotion lifecycle + evidence model now
> live in `docs/research/schemas/layer4-knowledge-artifacts.md` (resolves the table-vs-generic question to
> "generic artifacts, oracle-interpreted"). Spec only; Program B/D, the unsequenced GAP.


Goal: make Layer 4 operational rather than a loose memory bucket.

A generic knowledge artifact should include:

```text
id
type
summary
body
status
confidence
created_at
valid_from
valid_to
origin
evidence
anchors
supersedes
superseded_by
invalidated_by
review_state
```

Initial artifact types:

- DecisionMemory
- FailureMemory
- IncidentMemory
- AssumptionMemory
- ReviewMemory
- AgentDiscovery

Key design question:

> Should specialized memory types be stored as separate tables/classes, or as typed knowledge artifacts interpreted by specialized oracles?

Current preference:

> Store generic knowledge artifacts. Let oracles provide specialized interpretation.

---

## Weekend Priority 5 — Evidence-First Context Assembly

Goal: define how the Context Engine consumes oracle output.

The Context Engine should not decide truth. It should package oracle conclusions.

Pipeline:

```text
Task
  -> deterministic retrieval
  -> oracle evaluation
  -> oracle combiner
  -> cold start brief
  -> context engine packaging
  -> agent session
```

The context pack should carry provenance:

- why each item was included
- which oracle requested it
- whether it is fact, trusted knowledge, or hypothesis
- what evidence supports it
- what risk it mitigates

---

## Weekend Priority 6 — Facet Extraction Improvement Plan

> **Drafted (Day 3):** `docs/research/retrieval/facet-extraction-plan.md` answers the five design questions below
> (deterministic-from-structure / git-inferred / LLM-interpreted / vague-query guard / per-source
> confidence) and gives the hybrid extractor + bench plan. Plan only.


Do not tune retrieval yet, but write the extractor improvement plan.

The current benchmark suggests:

```text
Gold facets help.
Extracted facets fail.
```

Therefore the near-term engineering hypothesis is:

> better facet extraction may improve retrieval without changing the retrieval engine.

Design questions:

- Which facets are deterministic from structure?
- Which facets require LLM interpretation?
- Which facets can be inferred from Git history?
- Which facets should be forbidden for vague queries?
- How should extractor confidence be scored?

---

## Explicit Non-Goals For This Weekend

Do not spend the weekend on:

- embedding model selection
- retrieval tuning
- adding more benchmark cases
- rewriting the fixture
- optimizing lexical baselines
- live graph promotion decisions

Those should wait until the embedder is available.

---

## Suggested Three-Day Plan

### Day 1 — Oracle Runtime Spec

Deliverables:

- oracle input/output schema
- primitive/composite oracle taxonomy
- combiner responsibilities
- deterministic vs. LLM boundary

### Day 2 — Layer 4 + Cold Start Brief

Deliverables:

- knowledge artifact schema
- memory lifecycle states
- Cold Start Brief schema
- context-pack provenance rules

### Day 3 — Integration Plan

> **Drafted:** `docs/roadmap/oracle-integration-plan.md` — buildable-now vs gated map, Context Engine
> integration sketch, first task-level ColdStartBrief benchmark sketch, and a written (not filed) issue
> list tagged by gate.


Deliverables:

- Context Engine integration sketch
- extractor improvement plan
- first oracle benchmark sketch
- issue list for implementation

---

## Success Criteria

By the end of the weekend, Menhir should have:

1. A clear Oracle Runtime interface.
2. A defined Layer 4 knowledge artifact schema.
3. A Cold Start Brief artifact spec.
4. A plan for evidence-first context assembly.
5. A clear post-embedder path for retrieval validation.

---

## Strategic Framing

The R2 facet benchmark is a foundation for retrieval.

The Oracle Runtime is the foundation for agent readiness.

Most systems benchmark whether they retrieved the right chunks.

Menhir should eventually benchmark whether an agent was properly prepared to make the right change.

That is the higher-value target.
