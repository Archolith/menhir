# Memory Foundations

Compact companion for the framing and baseline assumptions behind
[memory-design.md](memory-design.md).

Use this file first when you need the system's high-level intent, design principles, or baseline
runtime assumptions without loading the full design doc.

## Scope

This file covers:

- `memory.overview`
- `memory.principles`
- context-replacement direction
- type-driven content contracts
- baseline stack assumptions

## Sections

### `memory.overview`
- source sections: `Overview`
- use for:
  - what `menhir` is for
  - why the system is graph-first
  - how ingest, recall, and lifecycle fit together

Key points:
- the system is a long-term graph memory service for agent context
- ingest, recall, and lifecycle are separate phases in one loop
- runtime details live in `architecture.md`; policy meaning lives in `memory-design.md`

### `memory.principles`
- source sections: `Quick Design Principles`
- use for:
  - the core constraints shaping v1
  - why the system prefers deterministic guards around narrow LLM usage
  - why retrieval and lifecycle remain separate

Key points:
- keep LLM prompts narrow and schema-bound
- avoid full-graph work on the hot path by caching expensive signals
- prefer cheap deterministic checks before model calls
- keep unresolved merges and retries auditable instead of magical

### `memory.principles.context_replacement`
- source sections: `Quick Design Principles`
- use for:
  - the long-term idea of replacing broad file loading with durable memory recall
  - the quality bar for memories that are worth storing

Key points:
- the graph should eventually replace some file reads, not just supplement them
- useful memories need concrete implementation facts, not generic summaries
- this only works once promotion and recall quality are trustworthy

### `memory.principles.content_contracts`
- source sections: `Quick Design Principles`
- use for:
  - type-specific expectations for stored memory content
  - why type labels should shape extraction and storage format

Key points:
- memory type is a content contract, not just a filter tag
- procedural memories should bias toward signatures, paths, parameters, and gotchas
- semantic and preference memories should preserve why the fact matters, not just the raw text

### `memory.stack`
- source sections: `Stack`
- use for:
  - the high-level technical baseline assumed by the design doc

Key points:
- Neo4j is the graph store
- Graphiti is the extraction and graph-write framework
- OpenAI-compatible endpoints, often local llama.cpp behind `yawn.scheduler`, are the common extraction path
- `architecture.md` is the owner for runtime/provider wiring details

## Read Next

- Need graph/lifecycle policy -> [memory-policy.md](memory-policy.md)
- Need ingest/query behavior -> [memory-ingest-queries.md](memory-ingest-queries.md)
- Need future expansion ideas -> [memory-futures.md](memory-futures.md)
- Need runtime details -> [architecture.md](architecture.md)
