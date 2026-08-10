# Oracle Architecture

## Status

active

> **2026-07-11:** architectural direction, not current state. The oracle pipeline/combiner described
> here is built but benched neutral-to-negative on LongMemEval and ships default-off; the ColdStartBrief
> half is unbuilt (L3/L4 GAP). The current active build direction is write-time consolidation. See the
> build-status note in [`semantic-operating-system.md`](semantic-operating-system.md).

## Purpose

This document defines how Menhir's Four-Layer Knowledge Model interacts with the Oracle system, Context Engine, and AI agents.

The key architectural principle is:

> Layers store knowledge. Oracles reason over knowledge. The Context Engine packages knowledge. Agents act on knowledge.

## Architectural Stack

```text
Execution
──────────────
Mutators
Tests
Compiler
Git

Context Delivery
──────────────
Context Engine

Reasoning
──────────────
Oracle Combiner
Specialized Oracles

Knowledge
──────────────
Layer 4 - Institutional Knowledge
Layer 3 - Semantic Model
Layer 2 - Structural Model
Layer 1 - Source Code
```

## Layer Responsibilities

### Layer 1
Raw source code.

### Layer 2
Deterministic structural truth.
Functions, symbols, dependencies, types, tests, Git anchors.

### Layer 3
Semantic understanding.
Capabilities, policies, constraints, decisions, invariants.

### Layer 4
Institutional knowledge.
Design rationale, incidents, failed approaches, production lessons, architectural discussions, agent discoveries.

Layers never decide what is relevant.
They only preserve knowledge.

## Oracle Responsibilities

Oracles never mutate knowledge.

They answer questions by interpreting evidence across the layers.

Example oracle families:

- StructureOracle
- SemanticOracle
- DecisionOracle
- FailureOracle
- IncidentOracle
- AssumptionOracle
- TemporalOracle
- EvidenceOracle
- BeliefOracle
- TestOracle

Each oracle returns:

- known facts
- evidence
- confidence
- unresolved questions

Never final truth.

## Oracle Combiner

The combiner merges oracle findings into a coherent evidence-first view.

It separates:

- Deterministic facts
- Trusted semantic knowledge
- AI hypotheses
- Open questions

The combiner is the only component allowed to synthesize across oracle outputs.

## Context Engine

The Context Engine is downstream of the Oracle layer.

It does not decide relevance.

It packages:

- files
- symbols
- semantic nodes
- evidence
- memories
- tests
- decisions
- incidents

into the smallest useful context for the target model.

## Cold Start Pipeline

```text
Task
 ↓
Deterministic Retrieval
 ↓
Oracle Evaluation
 ↓
Oracle Combiner
 ↓
Cold Start Brief
 ↓
Context Engine
 ↓
Agent
```

## Design Principles

1. Retrieval is not truth.
2. LLMs interpret; deterministic systems establish facts.
3. Every semantic claim should be backed by evidence.
4. Institutional knowledge should outlive individual developers and AI sessions.
5. The Context Engine packages knowledge; it does not create it.

## Vision

The Four-Layer Model is Menhir's memory.

The Oracle system is Menhir's reasoning.

The Context Engine is Menhir's communication layer.

Together they enable agents to begin work with an evidence-backed understanding of the codebase instead of a blind cold start.