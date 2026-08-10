# menhir - Memory System Design

<div align="center" style="margin: 10px 0 22px">

<span style="display:inline-block;background:#0d6efd;color:white;padding:4px 10px;border-radius:12px;font-size:0.85em;font-weight:600">v1 Design</span>
<span style="display:inline-block;background:#198754;color:white;padding:4px 10px;border-radius:12px;font-size:0.85em;font-weight:600;margin-left:8px">Local LLM Aware</span>
<span style="display:inline-block;background:#6c757d;color:white;padding:4px 10px;border-radius:12px;font-size:0.85em;font-weight:600;margin-left:8px">Graph + Retrieval</span>

</div>

## Overview

Concept id: `memory.overview`

> [!TIP]
> This doc is formatted for **Markdown Preview Enhanced**: callouts, mermaid blocks, and foldable sections should be easier to skim.

Do not preload this entire file by default. Read [concept-tree-design.md](concept-tree-design.md) or the document map
first, then open only the section(s) you need.

Use this file for memory policy and behavior. Use [architecture.md](architecture.md) for runtime and ops details, and
[data_models.md](data_models.md) for the field-level contract.

Concept ids for this doc are registered in [concept-ids.md](concept-ids.md).

## Quick Index

- Need the compact split docs first:
  - [memory-foundations.md](memory-foundations.md)
  - [memory-policy.md](memory-policy.md)
  - [memory-ingest-queries.md](memory-ingest-queries.md)
  - [memory-futures.md](memory-futures.md)
  - [memory-backlog.md](memory-backlog.md)
- Need retrieval logic: read `memory.policy.scoring`
- Need memory-type behavior: read `memory.policy.types` and `memory.policy.emotion`
- Need lifecycle / retention: read `memory.policy.scope` and `memory.policy.lifecycle`
- Need graph semantics: read `memory.policy.graph` and `memory.policy.edges`
- Need ingestion policy shape: read `memory.design.ingest`
- Need term definitions first: read `glossary.md`

```mermaid
flowchart LR
    Ingest[Episode Ingest]
    Extract[Entity + Edge Extraction]
    Dedup[Dedup / Merge]
    Store[SESSION + PERSISTENT Nodes]
    Retrieve[Two-Phase Recall]
    Explain[Explainability Payload]
    Ingest --> Extract --> Dedup --> Store --> Retrieve --> Explain
```

## Document Map

- [Core Data Structure](#core-data-structure)
- [Scoring](#scoring)
- [Memory Types](#memory-types)
- [Emotional Quotient](#emotional-quotient)
- [Edge Design](#edge-design)
- [Memory Scope](#memory-scope)
- [Memory Lifecycle (Freshness States)](#memory-lifecycle-freshness-states)
- [Responsibilities](#responsibilities)
- [Pattern Promotion: Skills and Hooks](#pattern-promotion-skills-and-hooks)
- [Temporal Awareness (post-v1)](#temporal-awareness-post-v1)

## Quick Design Principles

Concept id: `memory.principles`

- **Constrain LLM scope**: keep prompts narrow and schema-bound.
- **Separate concerns**: keep retrieval scoring and lifecycle sharpness independent.
- **Cache expensive signals**: avoid repeated full-graph scans at read time.
- **Second-pass safety**: unresolved merges stay in a replay queue until confident.
- **Single-flight ingestion**: expose async ingestion, but serialize Graphiti `add_episode()` calls to protect local model RAM and simplify attribution.
- **Deterministic load shedding**: use cheap prechecks and metadata stamping outside the LLM path whenever possible.

Use [memory-foundations.md](memory-foundations.md) for the longer explanation of the system's
intent, the context-replacement direction, type-driven content contracts, and the baseline stack
assumptions behind this design.

## Core Data Structure

Concept id: `memory.policy.graph`

Core graph rule:
- memories are nodes
- relationships are typed edges
- one concept may appear under many conceptual branches

Read [memory-policy.md](memory-policy.md) for the full graph semantics, episode-anchor role,
and retrieval access-pattern notes. Use [data_models.md](data_models.md) for the exact field
contract on nodes and episodes.

---

## Scoring

Concept id: `memory.policy.scoring`

Scoring split:
- relevance decides what surfaces at read time
- sharpness helps decide what survives lifecycle transitions

Read [memory-policy.md](memory-policy.md) for the composite relevance model, candidate generation,
preset tuning, cached prominence behavior, and sharpness-related notes.

---

## Memory Types

Concept id: `memory.policy.types`

Type rule:
- not all memories behave the same
- type affects both interpretation and future content expectations

Read [memory-policy.md](memory-policy.md) for the candidate types, emotional-vs-non-emotional
behavior, and type-specific retention implications.

---

## Emotional Quotient

Concept id: `memory.policy.emotion`

Emotion rule:
- emotions are structured metadata, not a single flat sentiment field
- sharpness uses emotional arousal where available, and uniqueness where it is not

Read [memory-policy.md](memory-policy.md) for the emotional field shape, sharpness derivation, and
why mixed-emotion structure matters for richer recall.

---

## Edge Design

Concept id: `memory.policy.edges`

Edge rule:
- edges are first-class records with their own metadata
- weight and cleanup should evolve deterministically, not heuristically

Read [memory-policy.md](memory-policy.md) for edge properties, weight dynamics, lifecycle
expectations, and deterministic merge/bridging semantics.

---

## Memory Scope

Concept id: `memory.policy.scope`

Scope rule:
- `CANDIDATE` is the low-trust human-review tier (see below)
- `SESSION` is the working layer
- `PERSISTENT` is durable memory under lifecycle control
- `PROMOTED` is protected durable memory

### CANDIDATE review tier

`CANDIDATE` nodes are pre-structured, low-trust signals written directly by external
emitters (e.g. recurring-friction clusters from `cth.painscan`) via the intake door:

- Written with `create_candidate` (`CandidateRepository`) - a direct Cypher write that
  bypasses the Graphiti enrichment queue, exactly like `TEMPORAL` nodes. Idempotent on
  `(source, candidate_cluster_id)`; re-emitting a cluster refreshes its provenance
  (`candidate_evidence_strength`, `candidate_distinct_sessions`, `candidate_last_seen`,
  `candidate_notes`) instead of duplicating.
- Always `user_flagged = false` - a flagged node would hit the lifecycle auto-promote
  shortcut, which skips the contradiction check we require for candidates.
- **Excluded from recall** (`RecallService` drops `scope == CANDIDATE` unconditionally,
  even with `include_session=True`). This is the load-bearing guarantee of staged review.
- Surfaced for human review in the explorer (`/explorer/partials/candidates`).

Transitions:
- **Approve** -> `PERSISTENT`. The canonical path is `CandidateService.approve` (exposed
  as backend `approve_candidate` for MCP/agent-driven approval): promote, then run the
  same contradiction check consolidation uses on freshly-promoted nodes (best-effort).
  The explorer approve button performs the scope flip directly against its repo and
  relies on the scheduled conflict scan over `PERSISTENT` nodes for the check.
- **Reject** -> `DETACH DELETE`. (Re-emit after reject recreates the candidate; emitter-
  side run-state dedup is responsible for not re-staging rejected clusters.)

Entry points: MCP tool `add_candidate`; HTTP `backend_invoke` ops `create_candidate`,
`list_candidates`, `fetch_candidate`, `promote_candidate`, `reject_candidate`,
`approve_candidate`.

Read [memory-policy.md](memory-policy.md) for session-vs-persistent behavior, consolidation
thresholds, lease-safe session ingest, sidecar split direction, and why one semantic store is
still preferred.

---

## Memory Lifecycle (Freshness States)

Concept id: `memory.policy.lifecycle`

Lifecycle rule:
- persistent memory should decay through explicit freshness transitions instead of disappearing abruptly
- compression, rehydration, and contradiction handling are policy decisions, not incidental side effects

Read [memory-policy.md](memory-policy.md) for the v1/post-v1 lifecycle shape, compression and
rehydration rules, contradiction handling, invariants, thresholds, and prominence-as-brake logic.

---

## Responsibilities

Concept id: `memory.design.responsibilities`

Responsibility split:
- graph handles structure, scoring signals, and timestamps
- LLM handles extraction, compression summaries, and interpretation
- user signals stay narrow and high-value instead of becoming routine tagging

Read [memory-ingest-queries.md](memory-ingest-queries.md) for the detail on explainable retrieval,
LLM budget controls, heartbeat behavior, and the intended user-override model.

---

## Stack

This section is summarized in [memory-foundations.md](memory-foundations.md). Use
[architecture.md](architecture.md) for runtime/provider wiring and [data_models.md](data_models.md)
for field-level contracts.

---

## Pattern Promotion: Skills and Hooks

Concept id: `memory.design.promotion`

Promotion path:
- memory -> repeated pattern -> passive skill or active hook
- promotion should require cross-session repetition and explicit guardrails
- promoted nodes stay protected, so quotas and review loops matter

Read [memory-futures.md](memory-futures.md) for the detail on skill-vs-hook behavior, promotion
criteria, self-generated automation, and promoted-scope guardrails.

---

## Query Construction (stub)

Concept id: `memory.design.query`

v1 keeps query construction narrow:
- classify intent
- resolve the target entity
- choose a bounded preset/template path

Read [memory-ingest-queries.md](memory-ingest-queries.md) for the fuller notes on intent
classification, traversal bounds, and why freeform query generation stays deferred.

---

## Entity Extraction & Resolution

Concept id: `memory.design.ingest`

Current ingest contract:
1. precheck
2. Graphiti extract/resolve
3. episode anchor write
4. policy stamping

Read [memory-ingest-queries.md](memory-ingest-queries.md) for the full explanation of
episode-centered ingest, duplicate-vs-false-merge tradeoffs, and provenance stamping. Use
[architecture.md](architecture.md) for runtime execution and [data_models.md](data_models.md) for
the exact field contract.

---

## Temporal Awareness (post-v1)

Concept id: `memory.design.temporal`

Temporal awareness stays post-v1 because it needs real usage history.

The compact design intent is:
- store some time structure in the graph
- compute suppression/resurfacing logic in a separate reasoning layer
- distinguish "fresh but redundant" from "old and worth resurfacing"

Read [memory-futures.md](memory-futures.md) for the fuller temporal model, including rhythm,
epochs, drift detection, and context-dependent recency behavior.

---

## Agent-Authored Graph Queries (post-v1)

Concept id: `memory.design.freeform_queries`

Freeform graph queries remain a future expansion.

The compact rule is:
- keep v1 on rigid, inspectable pipelines
- only open up to agent-authored Cypher once validation, resource limits, and dry-run behavior exist

Read [memory-futures.md](memory-futures.md) for the sandbox responsibilities and future MCP-shape
changes this would require.

---

## Open Questions

Concept id: `memory.backlog.open_questions`

The active open questions now live in [memory-backlog.md](memory-backlog.md), including scoring
defaults, promotion thresholds, temporal query handling, and drift-detection strategy.

---

## Weak Spots and Feature Ideas Backlog

Concept id: `memory.backlog.weak_spots`

The priority backlog now lives in [memory-backlog.md](memory-backlog.md). The current top buckets are:
- reliability and correctness
- retrieval quality and signal control
- product surface and integrations

### Community-Derived Priority Backlog (Reddit Scan)

Concept id: `memory.backlog.community`

The community-derived comparison notes are summarized in [memory-backlog.md](memory-backlog.md),
covering operator UX, eval visibility, hybrid retrieval, and low-friction startup priorities.

---

## External Project Evaluation (Adopt / Experiment / Skip)

Concept id: `memory.backlog.external_eval`

The adopt/experiment/skip comparison now lives in [memory-backlog.md](memory-backlog.md), including
the current notes on MCP Memoria, mem0 MCP, Hippocampus, and GraphRAG-style strategy routing.

---

## Code Graph Companion MVP (Phase 0/1/2)

Concept id: `memory.backlog.code_graph`

Its purpose: add a deterministic structural lane for symbols, calls, imports, and route-like
navigation without replacing the semantic memory layer. Phase-by-phase status (Phase 0/1 done,
Phase 2/2b/3 remaining) is tracked in [post-v1-todo.md](post-v1-todo.md) under
"Code graph companion MVP" / "Code graph Phase 1" — that is the current source of truth, not this
file or `memory-backlog.md`.

