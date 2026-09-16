# Tasks: Ingest

Use this file when the work is about how memories enter the system, move through enrichment, or are stamped into graph state.

## Common Tasks

### Measure whether an ingest/extraction change actually worked
- read first: [workflows/scalar_state_measurement.md](workflows/scalar_state_measurement.md)
- the four-stage coverage matrix (`assertion_emitted` / `subject_bound` / `view_materialized` /
  `fold_correct`) and the live authority A/Bs already exist, in `archolith-bench`, not menhir
- do NOT hand-roll a probe script or scrape enrichment logs to answer this; the gap BETWEEN stages
  is the diagnosis, and an ad-hoc probe cannot produce it

### Understand the ingest path
- read first:
  - `memory.design.ingest`
  - `runtime.shape`
  - `model.episode`
- then open:
  - [memory-ingest-queries.md](memory-ingest-queries.md)
  - [architecture.md](architecture.md)
  - [data_models.md](data_models.md)

### Ingest a project the server cannot see (remote code)

`ingest_project` stats and scans its path **in the server process**, so a hosted Menhir cannot
reach code on a workstation. The replacement is a client-built, sanitized snapshot uploaded over
MCP; the design and phase gates are in
[`plans/menhir-mcp-snapshot-ingest-2026-09-16.md`](plans/menhir-mcp-snapshot-ingest-2026-09-16.md).

What exists today is the local half only:

- `menhir sync --check` reports what a sync would upload. No network call, no archive written.
- `menhir.snapshot.protocol` is the wire contract; treat it as API, not as a helper module.
- Uploading is phase P2 and is gated on measuring the real transport (plan open decision 1).

Two rules to keep in mind before touching this code: the client never states completeness
(`partial_index` is the server's finding, derived from the bytes it extracted), and an omission is
not a deletion -- anything deliberately left out is declared in the manifest, because structure
writes prune.

### Understand memory scope or stamping
- read first:
  - `memory.policy.scope`
  - `memory.policy.graph`
  - `model.entity`
  - `model.episode`
- then open:
  - [memory-policy.md](memory-policy.md)
  - [data_models.md](data_models.md)

### Understand queueing and enrichment tools
- read first:
  - `mcp.group.ingest`
  - `mcp.group.processing`
  - `mcp.tool.add_memory`
  - `mcp.tool.add_memory_and_track`
- then open:
  - [endpoints.md](endpoints.md)

### Understand oversize / invalid-output handling
- read first:
  - `runtime.ops`
  - `memory.design.ingest`
  - `model.episode`
- then open:
  - [retry-policy.yaml](retry-policy.yaml)
  - [architecture.md](architecture.md)
  - [endpoints.md](endpoints.md)

## Machine-Readable Helpers

- [processing-states.yaml](processing-states.yaml)
- [retry-policy.yaml](retry-policy.yaml)
