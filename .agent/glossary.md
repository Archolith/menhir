# Glossary

Compact definitions for recurring `menhir` terms.

Read this file when a term is unclear instead of scanning several large docs.

## Terms

### `episode`
A provenance anchor for one ingest event. Episodes move through processing states and point to the
memory graph changes created or touched by that ingest.

### `episode anchor`
The persisted episode node created before or during enrichment. Used for queueing, provenance, and
recovery.

### `PENDING`
Episode exists and is queued for enrichment, but no worker currently owns it.

### `ENRICHING`
Episode is actively owned by a worker and is moving through Graphiti extraction or follow-up steps.

### `READY`
Episode enrichment finished successfully and the anchor points at the resolved canonical episode.

### `FAILED`
Episode enrichment ended unsuccessfully. Retry behavior depends on failure classification.

### `processing_stage`
Coarse live phase for an episode, such as `queued`, `graphiti_extracting`, `stamping`, or `finalizing`.

### `processing_substage`
Finer-grained live marker within a stage, used for troubleshooting stalls and boundary transitions.

### `sharpness`
Lifecycle-oriented retention signal. Used to help decide what should persist, compress, or decay.
Not the same as recall relevance.

### `relevance`
Retrieval-time ranking score. Combines similarity and graph-aware bonuses to decide what should
surface for a query.

### `scope`
Durability / visibility tier for a memory node. Core values are `SESSION`, `PERSISTENT`, and
`PROMOTED`.

### `freshness`
Lifecycle state for durable memories, used to govern compression and pruning behavior.

### `manual_review`
Failure classification meaning the system should stop blind retries and leave the item for operator
inspection or a safer recovery path.

### `terminal`
Failure classification meaning automatic retry should stop because the current input or condition is
not expected to succeed on retry.

### `retryable`
Failure classification meaning the system may safely try again within bounded retry policy.

### `stale recovery`
Repair path that releases or fails work that was abandoned by a dead worker or expired lease.

### `orphan recovery`
Lifecycle cleanup that reconciles leftover session-owned graph objects after abnormal shutdown or
session loss.

### `single-flight ingestion`
Per-runtime serialization of Graphiti `add_episode()` calls to protect local model capacity and keep
attribution simpler.

### `scheduler-managed llama`
Local OpenAI-compatible llama.cpp endpoint reached through `yawn.scheduler` rather than a fixed
always-on base URL.

### `task event`
Per-episode LLM usage event captured in telemetry for tracing active models, endpoints, and
scheduler task relationships.
