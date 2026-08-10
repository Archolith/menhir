# Memory Ingest And Queries

Compact companion for the ingestion, query, and advanced interaction parts of
[memory-design.md](memory-design.md).

Use this file first when you need how the system is expected to ingest, surface, or evolve memory
behavior without loading the entire design doc.

## Scope

This file covers:

- explainability and user involvement
- LLM budget controls
- heartbeat expectations
- pattern promotion
- query construction
- entity extraction and resolution
- temporal-awareness ideas
- agent-authored graph query direction

## Sections

### `memory.design.responsibilities`
- source sections: `Responsibilities`, `Heartbeat`
- use for:
  - operator involvement expectations
  - explainability requirements
  - budget-conscious system behavior

Key points:
- retrieval should remain explainable
- users should have minimal but meaningful override paths
- background or periodic behavior should stay lightweight

Detail notes:
- the graph does scoring math and timestamps, the LLM handles extraction and interpretation, and the user provides explicit override signals
- v1 user involvement is intentionally narrow: `user_flagged` exists as a high-signal retention override without requiring routine tagging
- heartbeat behavior is session-scoped and lightweight; it should surface obligations and preferences without becoming a second workflow engine
- budget control depends on deterministic guards before model calls, plus resumable/idempotent jobs once work is queued

### `memory.design.promotion`
- source sections: `Pattern Promotion: Skills and Hooks`
- use for:
  - when repeated memory becomes reusable behavior
  - skills vs hooks distinction

Key points:
- memory can eventually promote into reusable procedure or triggerable behavior
- promotion needs repetition, confidence, and guardrails

### `memory.design.query`
- source sections: `Query Construction (stub)`
- use for:
  - intent-to-query translation
  - preset and traversal shaping
  - why freeform traversal is deferred

Key points:
- query construction needs an intermediate intent layer
- v1 stays with vector search plus preset scoring
- complex traversal remains post-v1

Detail notes:
- query shaping starts with intent classification and entity resolution before any Cypher template selection
- bounded traversal and visited-set tracking matter more than expressive query syntax at v1
- freeform graph operations are deferred until validation and resource controls exist

### `memory.design.ingest`
- source sections: `Entity Extraction & Resolution`
- use for:
  - how episodes become graph updates
  - how Graphiti and later policy layers divide responsibility

Key points:
- ingest is episode-centered
- extraction and resolution are bounded and structured
- policy metadata may be stamped after Graphiti writes

Detail notes:
- the ingest path is precheck -> extract/resolve -> anchor/link/write -> stamp
- false-positive merges are worse than duplicates, so node resolution should err toward creating new nodes and cleaning them up later
- episode anchors are the deterministic correlation point for later stamping and provenance queries
- existing persistent/promoted entities should not be downgraded just because a new session references them

### `memory.design.temporal`
- source sections: `Temporal Awareness (post-v1)`
- use for:
  - future time-aware retrieval and suppression ideas

Key points:
- chronology and perceived relevance are separate
- temporal logic is deferred until the graph has enough real usage

Detail notes:
- future temporal design separates graph-native time structure from a reasoning layer that computes suppression and resurfacing behavior
- rhythm, epochs, and drift detection only become credible once the system has enough real interaction history
- time-aware recall should distinguish "too recent and redundant" from "old enough that the user may need a refresher"

### `memory.design.freeform_queries`
- source sections: `Agent-Authored Graph Queries (post-v1)`
- use for:
  - future sandboxed Cypher generation
  - read/write guardrail direction

Key points:
- rigid pipelines come first
- freeform agent-authored graph operations are a later expansion
- safety depends on sandbox validation and resource limits

Detail notes:
- any future freeform Cypher path needs schema injection, query classification, resource limits, and dry-run behavior
- write safety and auditability matter more than flexibility at this stage

## Read Next

- Need lifecycle and scoring policy -> [memory-policy.md](memory-policy.md)
- Need runtime implementation shape -> [architecture.md](architecture.md)
- Need MCP surfaces -> [endpoints.md](endpoints.md)
- Need full design discussion -> [memory-design.md](memory-design.md)
