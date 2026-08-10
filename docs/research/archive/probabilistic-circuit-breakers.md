# Probabilistic circuit breakers for menhir

## Status

superseded

## Superseded by

```text
docs/research/belief-layer.md
```

## Why this file remains

This file expanded the BeliefCircuit lane into probabilistic circuit breakers for memory operations. Its useful contents have been consolidated into `belief-layer.md`, including:

```text
BeliefCircuit scores beliefs.
Breakers gate memory operations.
BreakerOperation / BreakerDecision vocabulary.
Assertion, retrieval-injection, write-promotion, temporal-currentness, entity-merge, causal-claim, agent-action, lifecycle, research-claim, and provider-extraction breakers.
Local circuits, not one global probabilistic circuit.
Transparent heuristic baseline before ProbLog/PyJuice/true PC dependencies.
archolith-bench A/B conditions and memory_cascade_rate.
```

Keep this file only as a historical pointer so old links do not break.

## Current source of truth

Use `docs/research/belief-layer.md` for new work on:

```text
BeliefCircuit
probabilistic breaker decisions
AnergicBeliefGate
ApoptoticIndexPrune
HISTORICAL_ONLY / ANERGIC_CURRENT / BLOCKED buckets
productive-recency vs unproductive-recency
RetrievalExhaustionPenalty
bounded structural expansion
SelfToleranceGate
BraidTrace relationship
archolith-bench fixture plan
```

## Migration note

The breaker idea remains active, but its scope is now narrower and cleaner:

```text
Do not create a giant breaker taxonomy as standalone architecture.
Use breakers only where they change code behavior, eval fixtures, or metrics.
```

Immediate breaker work should focus on:

```text
1. assertion/currentness gating
2. retrieval-injection labels
3. write-promotion safety
4. Git/structure stale evidence
5. agent-loop exhaustion control
```
