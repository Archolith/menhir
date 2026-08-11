# Probabilistic belief layer for Chronostratum

## Status

superseded

## Superseded by

```text
docs/research/belief-temporal/belief-layer.md
```

## Why this file remains

This file was the first research spike for adding a probabilistic belief layer to menhir / Chronostratum. Its useful contents have been consolidated into `belief-layer.md`, including:

```text
BeliefCircuit as a sidecar over Graphiti/Neo4j/Git/structure evidence
four belief heads: relevant, current, supported, superseded
LLM-facing recall buckets
CE willow belief-drift example
frontier probabilistic-circuit research pointers
transparent baseline before ProbLog/PyJuice/true PCs
```

Keep this file only as a historical pointer so old links do not break.

## Current source of truth

Use `docs/research/belief-temporal/belief-layer.md` for new work on:

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

The most important correction since this original note is:

```text
Superseded memories should usually become historical/anergic, not dead.
```

Meaning:

```text
current-truth retrieval:
  suppress or label them

historical / belief-drift traversal:
  preserve and allow them

bad extraction / unsafe context:
  block or prune separately
```
