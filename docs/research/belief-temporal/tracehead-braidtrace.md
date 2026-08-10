# Tracehead and BraidTrace vocabulary

## Status

parked

## Promotion condition

Promote to active when menhir has a result object, formatter, or archolith-bench trace artifact that uses this vocabulary. Do not promote the full BraidFrame graph schema until a fixture proves that projecting over existing Episode/Entity/Fact/Evidence nodes is insufficient.

## Purpose

This document captures the braided-memory traversal vocabulary emerging around Chronostratum, BeliefCircuit, and temporal blast radius. It is vocabulary, not implementation — the concepts are parked here so they can be referenced consistently before any code adopts them.

The core insight:

```text
Memory should not be modeled as fact + timestamp.
Memory should be a braid of who, what, when, where, why, how, evidence, and belief state.
```

## Vocabulary

### Tracehead

```text
Tracehead = the selected entry point into a braided memory/evidence structure.
```

A Tracehead is not the whole braid. It is where an agent starts traversal.

Examples:

```text
CE willow patch episode
first observed texture-cache crash
last-known-good test run
first failing test run
user correction
Git commit touching a dependency cone
```

### BraidTrace

```text
BraidTrace = a query-time traversal through interwoven memory strands.
```

A BraidTrace follows multiple strands while preserving where they align, diverge, or contradict:

```text
who
what
when
where
why
how
evidence
belief state
Git state
code structure
tests
actions
```

### BraidFrame

```text
BraidFrame = a stored braided memory/event/belief unit.
```

A BraidFrame preserves the strands independently rather than flattening them into a single text snippet.

Candidate graph shape (do not build until needed):

```text
(:BraidFrame)-[:WHO]->(:Actor)
(:BraidFrame)-[:WHAT]->(:Event|:Fact|:Belief)
(:BraidFrame)-[:WHEN]->(:TemporalAnchor)
(:BraidFrame)-[:WHERE]->(:Repo|:File|:Symbol|:Test|:Conversation)
(:BraidFrame)-[:WHY]->(:Goal|:Hypothesis|:Rationale)
(:BraidFrame)-[:HOW]->(:Mechanism|:Patch|:Command|:TestRun)
(:BraidFrame)-[:SUPPORTED_BY]->(:Evidence)
(:BraidFrame)-[:HAS_BELIEF_STATE]->(:BeliefState)
```

### TraceCrossing

```text
TraceCrossing = a point where strands intersect during traversal.
```

Examples:

```text
belief + Git change + later failure
file/symbol + test failure + dependency cone
user correction + expired belief + current query
research claim + bench artifact + source card
```

### Cairn / Landmark

```text
Cairn = a durable landmark left after a useful BraidTrace.
```

Possible cairns:

```text
resolved cause
superseded belief
important correction
confirmed fix
research insight
benchmark result
```

## Mental model

```text
A query resolves to one or more Traceheads.
Menhir follows a BraidTrace from each Tracehead.
The BraidTrace crosses who/what/when/where/why/how/evidence/belief strands.
Probabilistic breakers decide which crossings, assertions, writes, merges, or actions are safe.
Useful resolved paths become durable Cairns or landmarks.
```

## CE willow example

```text
Question:
  What changed after the CE willow patch?

Tracehead:
  CE willow patch episode

BraidTrace:
  who:    user + agent debugging thread
  what:   CE willow texture-cache crash, patch, load-order/compatibility issue, load-order fix
  when:   original crash -> patch -> compatibility issue -> load-order fix
  where:  RimWorld mod stack, Combat Extended plant bounds / texture handling
  why:    stop hunting/LOS crash and log spam
  how:    patch addressed original texture-cache symptom; load order later changed compatibility
  evidence: error log, patch attempt, later user report, load-order resolution
  belief: "patch fixed it" -> "patch addressed original symptom, but full-fix belief superseded by load-order evidence"
```

## Relationship to other concepts

**Chronostratum** provides the temporal anchors (`valid_at`, `invalid_at`, `created_at`, `expired_at`, `observed_at`, `committed_at`) that BraidTrace uses to distinguish:

```text
true now / true then / believed then / learned later / superseded later
```

**BeliefCircuit** breakers operate on BraidTrace crossings, not just isolated facts:

```text
Assertion breaker:       Is this crossing safe to assert as current truth?
Write-promotion breaker: Are enough strands aligned to promote to durable memory?
Retrieval-injection:     Should this historical strand be normal context, conflict context, or suppressed?
Causal-claim breaker:    Do time, structure, evidence, and mechanism strands support causal language?
Agent-action breaker:    Is the trace strong enough to justify editing code?
```

**Temporal blast radius** is a BraidTrace query:

```text
structure strand ∩ time strand ∩ Git strand ∩ belief strand
```

## Lightweight domain sketch

Do not build this until a formatter or bench fixture needs it. The first useful implementation is an internal result shape over existing Graphiti/Neo4j/Git evidence:

```python
@dataclass(frozen=True)
class Tracehead:
    id: str
    kind: str
    target_id: str
    reason: str

@dataclass(frozen=True)
class BraidTrace:
    tracehead: Tracehead
    strands: tuple[BraidStrand, ...]
    crossings: tuple[TraceCrossing, ...]
    breaker_decisions: tuple[BreakerDecision, ...]

@dataclass(frozen=True)
class TraceCrossing:
    strand_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    interpretation: str
    risk: str
```

## Open questions

- Should `BraidFrame` be a persisted graph node or only a projection over existing Episode/Entity/Fact/Evidence nodes?
- Is `Tracehead` a query planner artifact only, or should important traceheads persist as anchors?
- Should useful `BraidTrace` outputs become durable Cairns?
- What is the smallest CE willow fixture that proves BraidTrace adds value beyond normal graph recall?

## First eval idea

A fixture where standard recall retrieves all relevant CE willow memories but fails to correctly separate original crash, patch attempt, apparent-fix belief, load-order issue, and load-order resolution. BraidTrace should walk the sequence and mark "patch fully fixed it" as superseded.

Potential metrics:

```text
current_vs_historical_accuracy
stale_assertion_rate
belief_drift_correctness
evidence_attribution
trace_completeness
```

## Source

Issue #12.
