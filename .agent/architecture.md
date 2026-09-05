# Architecture Reference

Do not preload this entire file by default. Start with `README.md` and `concept-ids.md`, then open only the
runtime section you need.

This file is the architectural source of truth. Keep operator runbooks, launcher procedures, and troubleshooting steps in `workflows/` docs rather than duplicating them here.

## Quick Index

- Need startup / boot order: read `runtime.overview`, `runtime.shape`, and `runtime.dependencies`
- Need scheduler behavior: read `runtime.ops`, `runtime.dependencies`, and `runtime.roadmap`
- Need provider wiring: read `runtime.providers`
- Need queue / telemetry behavior: read `runtime.ops` and `runtime.storage`
- Need write-time memory projections: read `runtime.projections` and `data_models.md`
- Need package ownership: read `runtime.packages`
- Need who "the user" is / self-entity identity: read `runtime.canonical_self` and `workflows/canonical-self-migration-runbook.md`
- Need operator commands / readiness checks / logs: use `workflows/operations_runbook.md` and `workflows/logging-and-troubleshooting.md`

## Overview

Concept id: `runtime.overview`

`menhir` is a Python service for long-term memory graph research and policy experiments for agent context.

It targets a remote Neo4j (via systemd) + Graphiti stack and is designed to evolve into an MCP-backed
recall/ingest service with the following core loop:

1. Ingest episode text into the graph via Graphiti.
2. Stamp policy metadata (scope, session, source) onto created/touched graph records.
3. Query for similar nodes and build graph-context ranked results.
4. Run lifecycle decay jobs to compress/prune stale memories.

### Event → Fold → Projection

Concept id: `runtime.projections`

The durable write-side boundary is:

```text
immutable, provenance-bearing evidence/events
  -> deterministic fold or reconciliation
  -> disposable, query-sufficient projection/View
  -> recall or a separate authority lane
```

An LLM may perceive typed assertions/events from language at the first boundary; it does not perform
the arithmetic, ordering, latest/predecessor selection, or supersession inside the fold. Raw evidence
and durable assertion/event logs remain the source of truth. Views and scalar/event projections are
additive, rebuildable products, never replacements for their contributors.

The local, not-yet-deployed lifecycle implementation gives current FACT Views a live-provenance
contract: every UUID in `episode_uuids` must resolve to an
`:Episodic` or `:TurnEvidence` node, and the evidence-to-View `MENTIONS` relationship is the automatic
retention authority. Ordinary memory decay does not manage derived Views. Authorized evidence
erasure instead retires dependent current Views, scrubs the erased UUID from retained history, and
resets fold cursors in the same graph transaction; generic recall fails closed on any orphaned View.
Activation remains blocked on schema/backfill coordination plus runtime publication-recovery,
tombstone-key, and generic-repair-dispatch services.

The original July frame described “one View node shape plus N folds.” Current code keeps the useful
invariant—new capabilities should reuse the event-log/projection boundary and shared write/query
infrastructure—but does not require every projection to share one physical Neo4j label or one value
slot. Scalar state/history, event timelines, metrics, and compatibility counters have kind-specific
contracts documented in `data_models.md`.

Batch and incremental execution are two evaluation modes of the same fold laws, not separate pure
and stateful operation families. Event-time ordering, replay/dedup, and anchor+delta reconciliation
are specified in `reference/fold-algebra.md`; precision and abstention policy are specified in
`memory-aggregation-under-uncertainty.md`.

## Technology Stack

Concept id: `runtime.stack`

- Python 3.12+
- Neo4j 5 (remote systemd service via bolt)
- `graphiti-core` >=0.28.1 (graph memory framework)
- llama.cpp (`llama-server`) via OpenAI-compatible API
- provider scaffold for pluggable chat backends (`openai_compat`, `openai`, `gemini`, `anthropic`)
- yawn.scheduler for llama-server lifecycle management and endpoint acquisition
- Langfuse (optional local tracing for OpenAI-compatible llama.cpp calls)
- `pytest` / `pytest-asyncio` for tests
- `fastapi` for the developer explorer UI
- `python-dotenv` for config loading

## Active Ops Direction

Concept id: `runtime.ops`

Deferred enrichment now runs behind a persistent backend runtime, with MCP stdio acting as a client surface.

Direction:

- plain reads stay side-effect free
- `core/runtime.py` is now the canonical runtime owner for init, shutdown, and runtime state; `mcp/lifecycle.py` is a thin stdio-client lifespan wrapper plus a small compatibility surface for flagged-memory bootstrap helpers
- runtime preflight now produces an explicit capability snapshot (`neo4j_ready`, `embedder_ready`, `llm_ready`, `scheduler_ready`) that the HTTP surface exposes directly for readiness and debugging
- `menhir serve` constructs one immutable `MemorySettings` snapshot and shares it with HTTP auth, the embedded OAuth AS, client-token storage, and the backend runtime; request handlers do not reread OAuth/HTTP environment variables
  - `config/oauth.py` owns `OAuthConfig` and its snapshot/legacy-environment builder
  - `config/auth_mode.py` owns the OAuth > client-token > static > none precedence decision
  - `api/oauth.py` retains token verification and compatibility exports, while config never imports API
- embedded-AS process dependencies (registered-client/code/refresh-token stores, signing key, and rate limiters) are configured from that snapshot before routes serve traffic
  - `api/oauth_refresh_store.py` owns the persistent, hashed, rotating refresh-token store; the grant remains default-off and is exposed through app state without request-time environment drift
  - authorization responses implement RFC 9207 `iss`; URL-form client IDs use the shared SSRF-safe CIMD resolver and a bounded, durable client snapshot, while DCR remains the fallback
  - protected-resource scopes remain Menhir permissions only; `offline_access` is authorization-server-only and never maps to a Menhir tier
- degraded startup is now split cleanly:
  - `degraded_reads_only`: Neo4j + embedder up, Graphiti client built without an LLM client so recall/search still works while enrichment is disabled
  - `degraded_queue_only`: Neo4j only, episodes still persist but Graphiti-backed recall/enrichment are unavailable
  - scheduler ownership only starts when enrichment capability is present
- `mcp/service_access.py` owns MCP backend selection and compatibility helpers, while
  `core/request_context.py` owns the transport-neutral caller session, auth-mode, and tier ContextVars;
  `core/reader_identity.py` owns reader-id normalization, and backend/runtime telemetry comes from
  `infrastructure.telemetry`; `core` therefore imports neither `menhir.mcp` nor the MCP framework
- remote OAuth requests additionally bind their immutable OAuth configuration and verified scopes in
  `mcp/service_access.py`; only an invocation-tier denial becomes an MCP `mcp/www_authenticate`
  tool result, while tenancy, allowlist, argument, and domain refusals remain ordinary errors
- production OAuth authority is client-specific: the exact digest-bound `client_id` selects the
  complete tool decision, and consent cookies approve only that client rather than an application
  suite; overlapping tool sets never imply shared authority or shared approval
- the production access contract fixes one client data-plane endpoint (`/mcp-http`) and product
  roles across host variants: ChatGPT, Codex, and Claude are operator-tier; OpenCode is agent-tier;
  namespace-wide deletion and client credential administration remain separate trust boundaries
  and are explicitly denied (see `deploy/ACCESS_CONTRACT.md`)
- `deploy/scaffold/menhir_app_only.py` is the sole root-owned app-image replacement
  authority: it mechanically classifies immutable bundles, holds the production lock,
  recreates only the Compose `menhir` service, preserves the Neo4j container identity,
  performs authenticated acceptance with a 60-second read-only deploy-probe JWT, and
  atomically commits or restores the prior image and release authority
- all 54 MCP tools declare a title, reviewed safety annotations, and a minimum OAuth scope; startup
  rejects an incomplete or tier-incoherent declaration before the catalog is served
- `core/backend_protocol.py` + `core/backend_impl.py` now define the backend-first MCP contract
  - `RuntimeProvider` wraps in-process `BuildArtifacts` + telemetry into serializable backend operations
  - `BackendClient` maps the same protocol onto an internal-only HTTP transport at `/api/internal/backend/{operation}`
  - `core/backend_config.py` resolves backend transport credentials without depending on MCP
  - all MCP tools now call `BaseTool.get_backend()` instead of reaching into `BuildArtifacts` directly
  - all MCP resources now call `BaseJsonResource.get_backend()` as well; the old `get_services()` resource/tool bridge has been removed from active MCP wiring
  - ingest provenance now uses an explicit caller-session layer instead of inheriting the runtime process session
  - local backend/provider paths now distinguish:
    - process session: runtime maintenance ownership only
    - caller session: stdio caller, remote MCP caller, or REST caller provenance
  - authenticated HTTP/MCP requests bind a request-scoped caller session via middleware headers/context, and REST/backend routes now reuse that caller session before falling back to generated local request sessions
  - local stdio backend-client mode carries a cached MCP caller session keyed by config rather than reusing runtime ownership state
  - stdio MCP now requires `MENHIR_BACKEND_URL`; it health-checks the backend during lifespan startup and does not own local runtime bootstrap/shutdown at all
  - the public HTTP surface is now intentionally split:
    - `/api/health`, `/api/ready`, `/api/stats`: canonical external runtime/status endpoints
    - `/api/internal/backend/*`: hidden transport layer used by `BackendClient`, not part of the public OpenAPI surface
  - public REST handlers now use the same backend/provider seam as MCP instead of reaching into `built.*` directly; the only deliberate exception is `POST /api/memory?wait=true`, which still touches the local runtime to wait on in-process queue completion after queueing through the backend seam
  - remote MCP scope is now explicit:
    - stdio MCP: tools + resources
    - HTTP-mounted remote MCP: tools only
  - `api/mcp_remote.py` now builds the remote transport through a dedicated tool-only constructor so the public remote contract is narrow by design, not just by omission
- runtime state is tracked through an explicit typed lifecycle state container instead of an ad hoc dict-only bag
  - owned runtime state fields: `built`, `session`, `scheduler`, `init_task`, `startup_runtime_task`, `shutdown_task`, `flagged_bootstrap_reads`
- MCP contract cleanup now has a shared class-based endpoint layer in `mcp/contracts.py`
  - JSON resources register through `BaseJsonResource`, which centralizes telemetry wrapping, JSON rendering, and structured JSON error envelopes
  - all MCP tools now register through explicit `BaseTextTool` / `BaseJsonTool` contracts instead of ad hoc per-file telemetry and formatting
  - `mcp/server.py` and `mcp/tools/__init__.py` are now wiring modules, not compatibility barrels for individual tool handlers
- each bot/client can spawn its own `menhir.mcp.server` process in multi-bot environments, but those stdio processes are now backend clients rather than independent runtime owners
- scheduler-owned jobs now handle stale lease recovery and bounded failed-retry sweeps
- `services/scheduler_tasks.py` retains the stable personal-memory scheduler entry point, while
  `services/scalar_consolidation.py` owns typed-scalar dirty selection, paged perception/cursors,
  duplicate-counter retirement, and scalar repair/reconciliation; both counter and scalar phases
  receive the same per-run LLM call counter and budget
- a singleton SQLite lease now ensures only one MCP process runs scheduler jobs at a time; later processes stay in standby
- operators can now force lease takeover from MCP for troubleshooting when a stale/incorrect owner blocks scheduler progress
- queue state remains durable in Neo4j
- failure history is also persisted append-only in SQLite so retry and terminal decisions can be audited after the graph row changes
- malformed Graphiti JSON/schema output is now classified as `manual_review` instead of being blindly retried until the normal attempt cap is exhausted
- future capacity control will reserve llama.cpp throughput for delegate versus memory workloads
- live episode telemetry now carries a coarse `processing_stage` plus finer `processing_substage` and current LLM task metadata so stalled Graphiti calls can be correlated with scheduler activity
- `menhir` now also registers generic parent-job / child-task updates with `yawn.scheduler` so episode UUIDs can be shown as parent jobs while Graphiti add-episode requests are shown as child tasks on the scheduler dashboard
- `ingest_project` now writes a deterministic structural graph directly into Neo4j and then queues a best-effort semantic narrative episode for the same project
  - `services/project_ingest.py` owns path validation, scan/write orchestration, bounded narrative
    construction, best-effort episode queueing, and transport-neutral outcomes
  - the MCP tool only supplies backend/caller arguments and formats that outcome
- structural-semantic integration is now active:
  - semantic entities can be linked to structural entities via `ANCHORED_TO`
  - recall can inject file-linked semantic candidates through `file_context`
  - blast-radius queries can surface `related_memories`
- direct graph-managed TODOs now exist as a parallel operator surface:
  - `:Todo` nodes are created directly in Neo4j, not through Graphiti enrichment
  - TODOs can link to structural files (`REFERENCES_FILE`), source episodes (`CREATED_FROM`), and persistent semantic entities (`CONCERNS`)
  - open TODOs are surfaced in hook bootstrap, query context, and blast-radius output
- `domain/retrieval_trace_models.py` is the single owner of shared recall/trace value contracts;
  recall and tracing no longer import each other, and in-repo consumers use the owner directly

## Package Map

Concept id: `runtime.packages`

```text
src/menhir/
|- __init__.py        Package metadata
|- __main__.py        CLI entry point
|- main.py            Startup dependency checks (Neo4j + scheduler/llama.cpp connectivity)
|- core/              build_memory_services(), prepare_memory_runtime(), BuildArtifacts
|- config/            MemorySettings, AuthMode, OAuthConfig (env-backed), MilestoneZeroScope
|- domain/            MemoryNode, Edge, MemorySession, IngestResult, recall types (QueryPreset, ScoredMemory, etc.)
|   |- retrieval_trace_models.py  Neutral recall scoring/trace value contracts
|   |- event_history.py  Event History Phase 1: immutable TypedEventAssertion/EventLane contract +
|                       pure latest/predecessor selector (select_event_assertion)
|   |- truth/         Truth package (SSOT for provenance/trust): ReviewState, TruthAttestation, TruthClaim,
|                     WardenLabel, ANCHOR_KINDS, KIND_TO_SIGNAL, DIVERSITY_FAMILY, SOURCE_CONFIDENCE_* constants
|- infrastructure/    Neo4jRepository, GraphitiClient, MemoryGraphAdapter, schema bootstrap
|   |- graphiti_*_patches.py  Extraction, model/dedup, and LLM-response compatibility families
|   |- telemetry/store.py  SQLite connection and schema owner
|   |- telemetry/*_store.py  Event, lifecycle, and recall/client persistence families
|   |- view_repository.py  Composition facade over View models, writes, scalar authority, and queries
|   |- typed_assertion_repository.py  Composition facade over assertion writes, reconciliation, and repair
|   |- typed_event_repository.py  Durable TypedEventAssertion append/audit log (Event History Phase 2); head/source-key
|                                idempotency, strict-rank supersession, binding safety, lane read-back
|   |- view_query_repository.py  event-lane timeline View methods: record/fetch/list/retire + exact EVENT_HISTORY_ENTRY draw
|- services/          IngestService, RecallService, ScoringService, LifecycleService, MaintenanceScheduler
|   |- scalar_consolidation.py  Typed-scalar backfill, cursor, retirement, and repair runner
|   |- typed_scalar_rules.py  Deterministic scalar extraction, gating, temporal, and binding rules
|   |- typed_scalar_persistence.py  Assertion persistence and pending-binding repair
|   |- typed_scalar_service.py  Stateful scalar activation and perception coordinator
|   |- event_history_service.py  Deterministic exact-lane event timeline rebuild from the durable assertion log (Phase 2)
|   |- event_history_perception.py  Event History Phase 3: generic LLM extraction/admission seam -> grounded proposals
|   |- event_consolidation.py  Event History Phase 3: backfill :TurnEvidence -> durable assertions via independent
|                             :EventConsolidationWatermark cursor; fail-closed page spine, bounded generic metrics
|   |- event_history_recall.py  Event History Phase 4: pure latest/predecessor classifier + selector (EventQueryRoute)
|   |- event_history_authority.py  Event History Phase 4: structured EventAuthorityVerdict for conservative first-person queries
|   |- recall_service.py  Public RecallService dataclass and API facade (conditional event-history authority layer)
|   |- recall_policies.py  Pure retrieval, temporal, and authority policy helpers
|   |- recall_support.py  Rehydration, facet/frontier, adjacency, and post-recall operations
|   |- recall_pipeline.py  Candidate acquisition, scoring, enrichment, and result assembly
|   |- ingest_service.py  Public IngestService dataclass and composition facade
|   |- ingest_queue.py / ingest_worker.py / ingest_intake.py  Queue lifecycle, enrichment workers, and intake operations
|   |- lifecycle_service.py  Public LifecycleService dataclass and composition facade
|   |- lifecycle_consolidation.py / lifecycle_decay.py / lifecycle_conflicts.py  Focused lifecycle workflows
|- mcp/               FastMCP server (43 tools, 9 resources, SQLite telemetry, structured failure logs, deferred enrichment, scheduler)
|- explorer/          FastAPI graph explorer with Cytoscape.js visualization

Project tooling:
|- integration_test.py    End-to-end graph + LLM extraction smoke/integration run
|- smoke_test.py          Quick Neo4j connectivity check (no LLM)
|- tests/                 Pytest suite (unit + online markers)
```

## Current Runtime Shape

Concept id: `runtime.shape`

The runtime is now split into one persistent owner plus multiple optional client surfaces:

- `menhir serve`
  - canonical runtime owner
  - owns Neo4j/Graphiti services, queue recovery, maintenance scheduler, auth, REST, and remote MCP
- stdio MCP (`menhir.mcp.server`)
  - client-only process
  - requires `MENHIR_BACKEND_URL`
  - exposes tools/resources locally but forwards operations through the backend protocol
- remote MCP (`api/mcp_remote.py`)
  - HTTP-mounted tool-only MCP surface
  - shares the same backend/runtime as REST
- REST (`api/server.py`, `api/routes.py`)
  - canonical external health/readiness/stats and memory API surface

Startup modes:

- `full`: Neo4j + embedder + LLM + scheduler available
- `degraded_reads_only`: Neo4j + embedder available, LLM unavailable
- `degraded_queue_only`: Neo4j available, embedder/LLM unavailable

## Provider Direction

Concept id: `runtime.providers`

`menhir` now has an explicit provider scaffold for memory-processing LLM calls:

- `openai_compat`
- `openai`
- `gemini`
- `anthropic`

Current scope:

- `LLMAdapter` is now built through a provider-backed chat backend factory
- `openai_compat` and `openai` use the current OpenAI SDK path
- `gemini` now uses a direct Google `generateContent` REST backend for `LLMAdapter`
- `anthropic` is still scaffolded but not implemented yet

Important limitation:

- `GraphitiClient` still requires an OpenAI-compatible contract today
- non-OpenAI Graphiti providers are rejected explicitly at startup
- swapping Graphiti extraction to Gemini or Anthropic will require a dedicated bridge layer, not just an env flip

Practical backend implication:

- the currently supported "hybrid" setup is `OpenAI` or `openai_compat` for Graphiti extraction, plus optional local embeddings / reranking behind the OpenAI-compatible endpoints Graphiti already knows how to call
- `gemini` can be used for direct `LLMAdapter` work outside the Graphiti path, but it is not a drop-in replacement for Graphiti episode extraction today
- when discussing backend cost, treat Graphiti extraction as the billable path unless and until a dedicated Gemini bridge exists

Supported hybrid config shape:

- Graphiti extraction can now target `openai` directly via `OPENAI_API_KEY`/`OPENAI_CHAT_MODEL`/`OPENAI_EMBED_MODEL` plus `GRAPHITI_LLM_PROVIDER=openai`
- Graphiti embeddings can independently target `openai_compat` via `GRAPHITI_EMBED_PROVIDER=openai_compat` and the `LOCAL_LLM_EMBED_MODEL`/`LOCAL_LLM_EMBED_BASE_URL` (or legacy `LLAMA_EMBED_MODEL`/`LLAMA_EMBED_BASE_URL`) endpoint settings — there is no separate `GRAPHITI_EMBED_BASE_URL`/`GRAPHITI_EMBED_API_KEY`/`GRAPHITI_EMBED_MODEL`; those names are not read anywhere in the codebase (SSOT-07)
- if no Graphiti-specific overrides are set, extraction and embeddings inherit from the selected provider defaults for backward compatibility
- there is still no dedicated external reranker integration in the current code; retrieval continues to use Graphiti's built-in search/rerank path plus local graph scoring

Database isolation for provider testing:

- `NEO4J_DATABASE` now selects the target graph database for both the local repository adapter and Graphiti
- use a separate database name (for example `menhir_gemini_test`) when testing Gemini-backed memory processing
- `memory://system/metadata` now reports both the active `neo4j_database` and the selected chat/graphiti providers

The system has a working ingestion pipeline:

0. **Backend startup bootstrap** (`menhir serve`)
   - Start scheduler process bootstrap when Graphiti is using scheduler-managed local llama endpoints
   - Initialize the canonical runtime once on backend startup
   - Recover pending work and then run orphan recovery in the background so `/api/ready` can return promptly

0b. **MCP stdio startup** (`mcp.server` lifespan)
   - Require `MENHIR_BACKEND_URL`
   - Probe `/api/ready` before serving stdio MCP
   - Do not initialize or own a second local runtime

1. **Bootstrap** (`prepare_memory_runtime`)
   - Initialize Neo4j repository (lazy driver)
   - Construct Graphiti client from settings (patches prompt JSON serialization and installs the
     Graphiti 0.29.2 combined-extraction compatibility layer)
   - Run Graphiti `build_indices_and_constraints()`
   - Run menhir phase-1 schema bootstrap (indexes + defaults for all policy fields)

2. **Ingestion** (`IngestService.ingest_episode`)
   - Async, session-aware, single-flight (asyncio.Lock serializes Graphiti calls)
   - Calls Graphiti `add_episode()` for LLM-backed entity extraction + resolution
   - The compatibility layer runs Graphiti's typed combined node-and-edge extractor during the
     node phase, then passes its edges through a task-local `ContextVar` to Graphiti's subsequent
     edge phase. This keeps explicit relational values such as `the suburbs` and `downtown` from
     becoming orphaned by separate node extraction. Cached edges are consumed once; custom edge
     schemas and cache misses fall back to upstream edge extraction.
   - An entity-bearing, edge-empty combined extraction gets one bounded corrective pass. Only that
     pass lazily loads the two preceding user/assistant `:TurnEvidence` records from the linked
     turn's namespace and session, delivering them through Graphiti's native `previous_episodes`
     channel so a shorthand reply such as `100 is a good starting point` can recover its subject.
     Repair edges must retain a meaningful literal token from the current message; context-only
     claims copied from prior turns are suppressed. Successful first passes perform neither the
     evidence-context graph read nor an extra model call.
   - Repeated extraction evidence is retained in `results/suburbs_extraction_gate.json`; isolated
     production replay evidence, including stale-edge invalidation and recall, is retained in
     `results/suburbs_extraction_live_smoke.json`.
   - Bounds Graphiti `add_episode()` with a configurable timeout (`MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS`, default `300s`) so hung extraction requests fail back into retry flow instead of leaving episodes stuck in `ENRICHING`
   - Rejects obviously oversized episode text before Graphiti extraction using a configurable rough token estimate (`MEMORY_GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS` / `GRAPHITI_EPISODE_MAX_ESTIMATED_TOKENS`, default `12000`, `0` disables the guard)
   - When Graphiti is using scheduler-managed llama endpoints, the client watchdog also fails `add_episode()` if scheduler status stays unavailable or goes idle past the configured stall timeout, rather than waiting indefinitely for a stuck request
   - Extracts UUIDs from Graphiti result (episode, entity nodes, edges, episodic edges)
   - Stamps policy metadata via `MemoryGraphAdapter.stamp_ingest_metadata()`:
     - Episodic nodes: strong stamp (scope=SESSION, session_id, user_id, source)
     - Entity nodes: conservative stamp (coalesce, never downgrade PERSISTENT/PROMOTED)
     - Edges: conservative stamp (coalesce scope, weight, source)
   - Returns `IngestResult` with episode_id, status, counts

3. **Retrieval** (`RecallService.recall`)
   - Async two-phase pipeline: Graphiti vector search → graph-based scoring
   - Phase 1: `GraphitiClient.search_scored()` for hybrid node search (BM25 + cosine, RRF reranker)
   - Optional `file_context` resolves a structural file neighborhood and injects anchored semantic candidates into recall even when vector similarity alone would have missed them
   - Fetches candidate metadata and adjacency via `MemoryGraphAdapter` Cypher queries
   - Filters out SESSION-scoped (by default) and GONE-freshness nodes
   - Phase 2: `ScoringService.score_candidates()` applies pure relevance formula:
     `relevance = similarity + α×adjacency + β×recency + γ×prominence`
   - Five presets (recent, knowledge, emotional, connected, conflict) control α/β/γ weights
   - **Scoring signal status:**
     - γ (prominence) is active — `edge_count` is maintained via `sync_edge_counts()` (M4 complete)
     - δ (conflict boost) is active — `has_conflict` and `conflict_status` flow into `CandidateData`, conflict preset uses δ=0.4 (M5 complete)
     - "emotional" preset has δ=0.0 — no emotional arousal signals flow into scoring yet (deferred post-v1). It differs from other presets only in adjacency/recency balance.
   - Every result carries `RelevanceBreakdown` with all four component scores + preset info
   - Edge weights are only incremented for edges touching returned results (not all candidates)
   - Updates `last_accessed` on retrieved nodes
   - Returns `RecallResult` with a ranked observation lane (`ScoredMemory`) and, when view authority
     is enabled and selected, a distinct structured `authority_layer`
   - The authority layer reports current, expired, or as-of scalar verdicts. It contains the winning
     slot/value/foundation plus bounded, relation-labelled contributors; it is never interleaved
     with or reranked as ordinary observations
   - When event-history authority is enabled (default off), recall also attaches a separate
     `event_authority_layer` — a structured first-person latest/predecessor verdict, independent of
     scalar authority and likewise never reranked as an observation (see 4b below)
   - Contributor expansion is available at `GET /api/scalar-authority/{view_uuid}/contributors` with
     explicit pagination, while the inline recall payload remains bounded

4. **Scalar projection lifecycle** (`ScalarStateService`)
   - `TypedAssertion` is the immutable evidence log; `ScalarStateView` is a rebuildable projection
   - Typed-scalar perception requires one response envelope per input episode and audits explicit
     empty coverage separately from malformed, duplicate, or missing envelopes. The k-sample gate
     conservatively groups only same-episode quotes with a real common substring; the committed
     proposal is grounded to that common source span. Span grounding is always internal rather than
     an operator-tunable benchmark flag
   - Stable slot families do not encode ungrounded source-time words (`current_`, `latest_`,
     `previous_`, or `prior_`). Exact interval frequency wording is normalized deterministically
     from its grounded quote, while hedged/approximate values still abstain
   - Consolidation audit events retain a bounded, quote-free proposal identity per sample (source
     key, episode/offsets, slot semantics, normalized value, and world time), allowing omissions,
     parser drops, vote fragmentation, and fold outcomes to be distinguished without copying
     transcript text into telemetry
   - `MENHIR_SCALAR_DETERMINISTIC_SHADOW` optionally runs the pure deterministic scalar extractor
     once after the existing LLM gate and emits bounded, quote-free exact/aligned agreement and
     router-completeness receipts through consolidation audit. It is default-off, fail-open, and
     observe-only: deterministic output never reaches binding, persistence, projections, authority,
     recall, or the service result. Audit recording also requires
     `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED=true`
   - Shadow schema v2 additionally composes deterministic and committed LLM proposals independently
     through the same fail-closed structural sidecar. Raw exact/aligned fields remain unchanged;
     compositional exact/aligned, unresolved, identity-disagreement, unjoinable, and diagnostic LLM
     router-miss counts live under a separate `compositional.diagnostic_vs_llm` section. Bounded
     pair rows contain stable hashes and closed enums only, never open target/subject text. This is
     diagnostic evidence, not a correctness label or promotion gate (`promotion_status` remains
     `not_evaluable`). Stable hashes are pseudonymous join identifiers, not secrets or protection
     against dictionary attacks; audit access controls still apply. Legacy raw summaries retain
     their existing schema-v1-compatible semantic fields inside the schema-v2 envelope.
   - `ScalarHistoryView` (`view_kind="scalar_history"`) is a second projection preserving every
     delta/absolute/correction/expiry in source-time order without computing an absolute value.
     Key prefix `sh_`, `lww_register=False`. Feature flag:
     `MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED` (default off). `rebuild_scalar_projections()`
     rebuilds both state and history atomically; `rebuild_scalar_history()` rebuilds history only
    - Future assertions remain `activation_pending` and are excluded from present-time folds until a
      scheduler pass atomically claims them after `valid_at`
   - Assertion, source-memory, and namespace deletion write `ScalarProjectionRepair` receipts in the
     same graph transaction. A receipt is completed only after the affected subject/namespace view
     has been rebuilt at one concrete evaluation time, so a crash leaves discoverable repair work

4b. **Event history (default-off production-capable)** — Event History Phases 1–5 are landed at
    `370eff1` as a **default-off** path. Every event settings/flag defaults off; flag-off behavior is
    byte-compatible and scalar assertion/state/history/authority and wire contracts are unchanged.
    There is no dedicated event endpoint, no default enablement, and no canonical-run gain claim.
    The path:
    - `domain/event_history.py` — immutable `TypedEventAssertion` / `EventLane` contract with three
      identity levels (binding-stable `source_key`, fully-interpreted `assertion_key`, fold-selection
      `lane`) and the pure `select_event_assertion` latest/predecessor selector.
    - `infrastructure/typed_event_repository.py` — durable append/audit log (`:TypedEventAssertion`
      / `:TypedEventAssertionHead`) with idempotent source/assertion keys, strict-rank supersession,
      binding-safety (`binding_mismatch` fails closed), pending→bound adoption, and lane read-back.
    - `infrastructure/view_query_repository.py` + `TimelineKind` — event-lane timeline Views
      (predicate/domain lane discriminator) with exact `EVENT_HISTORY_ENTRY` contributor edges; the
      legacy subject-only timeline API and key stay backward compatible.
    - `services/event_history_service.py` — deterministic rebuild of exactly one event lane from the
      durable log into the disposable View, completing only after a successful View write, exact-edge
      proof, and exact-lane reconciliation.
    - `services/event_history_perception.py` (Phase 3) — generic, offline LLM extraction/admission
      seam: a single completed-acquisition predicate registry (`acquired`), exact-quote/unique-span
      grounding, completed-vs-intent/hypothetical/negation discrimination, and fail-closed admission.
      LLM output is **perception only**; ordering, folding, and selection stay deterministic.
    - `services/event_consolidation.py` (Phase 3) — backfills grounded occurrences from canonical
      user `:TurnEvidence` into durable assertions and rebuilds affected lanes via an **independent**
      `:EventConsolidationWatermark` cursor keyed by namespace in `group_id` (never disturbing the
      scalar/counter cursors), under a fail-closed page spine emitting bounded, generic metrics.
    - `services/event_history_recall.py` + `services/event_history_authority.py` (Phase 4) — pure
      latest/predecessor classifier + selector and a structured `EventAuthorityVerdict` for
      conservative first-person `did I` queries, reasoning only over in-memory assertions.
    - **Conditional recall authority** (Phase 4, default off): `RecallService` probes a recognized
      first-person event route only when `event_history_authority_enabled` is on AND a namespace is
      present, reads assertions via `event_assertions_for_subject_predicate`, and attaches
      `RecallResult.event_authority_layer` — a separate structured verdict, never interleaved with or
      reranked among observations, and never changing the scalar verdict contract.
    - **Transport + lifecycle closeout** (Phase 5): the event authority layer is carried through REST
      `/api/recall` (`event_authority_layer`), the MCP `recall_memories` tool, the
      `ContextBuilderService` context block, and the backend round-trip. The scheduled personal-memory
      job and manual `POST /api/phase3/run` drive event consolidation when enabled and return bounded
      Phase-3 event metrics. Namespace cleanup is event-aware: `delete_namespace_with_scalar_cascade`
      deletes the namespace-keyed event log and the independent watermark, and preserves a shared
      `:TypedEventAssertionHead` that still `HAS_VERSION` to a surviving assertion in another
      namespace (shared-head safety; its deleted CURRENT is repaired by a later idempotent write).
    - **Recall Lab inspection**: benchmark task pages query the active or provenance-verified reused
      graph for scalar-state, scalar-history, and event-history Views. A role map explains current
      authority versus advisory numeric change history versus ordered event occurrences; grounded
      event assertions and exact quotes remain inspectable without treating occurrences as current
      ownership.
    The flow is: durable `TypedEventAssertion` (source of truth) → deterministic lane fold → event
    timeline View (disposable projection) → pure latest/predecessor selection. `valid_at`
    (world/source time) is the only ordering/selection time; `learned_at` is retained only as
    audit/ingest time and is never authority ordering or fallback. Invalid `valid_at` stays durable
    but cannot enter the View or lead. Exact replay dedups; distinct same-world-time winners fail
    closed as ambiguous; event siblings never supersede by recency. It is production-capable but
    **not enabled by default**.

5. **Lifecycle** (`LifecycleService`)
   - `apply_decay()` — ACTIVE → COMPRESSED → GONE transitions (M4)
   - `edge_count` maintenance via `sync_edge_counts()` (M4)
   - Emotional/conflict data paths wired into scoring (M5)
   - Conflict groups with LLM contradiction confirmation (M5)

6. **Ops hardening** (M6)
   - `CircuitBreaker` with CLOSED/OPEN/HALF_OPEN state machine on Graphiti backends
   - Rolling-window LLM budget caps per session with `retry_after` requeue semantics
   - `ContextBuilderService` — token-budget-aware context packing with tiktoken/heuristic fallback
   - `EmbeddingCache` — SHA256-keyed LRU cache at the OpenAI-compatible client layer
   - SQLite sidecar expansion: `lifecycle_actions` and `memory_revisions` tables
   - Telemetry aggregation: p50/p95 latency, failure summaries, enrichment rate, lifecycle stats

7. **Explorer** (`explorer/app.py`, `explorer/integration.py`)
    - Mounted at `/explorer` on the main FastAPI app for inspecting the memory graph
    - Shares the backend's Neo4j pool and supervised lifecycle (single process, one port)
    - Recent episodes, sessions, flagged nodes, entity search
    - Node detail with neighbors and mentioning episodes
    - Cytoscape.js graph visualization API
    - Auth-gated on non-loopback binds (bearer token required for remote access)
    - Static assets at `/explorer/static` exempt from auth (CSS/JS can load)
    - Can be disabled via `MENHIR_EXPLORER_ENABLED=false`
    - **Known limitation:** session views query by `n.session_id`, which means
      persistent/promoted entities touched by a session (but not stamped with its
      session_id due to conservative stamping) will not appear. "Session graph"
      means "records stamped with this session," not "everything this session touched."
    - **Bench-run explorer** (`explorer/bench_runs.py`): filesystem catalog + read-only task
      projection for LongMemEval benchmark runs. Routes under `/explorer/recall-lab/bench-runs/`.
      Reads manifests/checkpoints from `MENHIR_BENCH_RESULTS_ROOT`. Only the configured active
      run (`MENHIR_BENCH_ACTIVE_RUN_ID`) joins artifact data with live graph projections.
      Contract: `bench-inspection/v1`. Standalone `:8200` dashboard is temporary; Menhir Recall
      Lab is the canonical owner.

7. **Project ingestion + structural graph**
   - `ingest_project` scans a repo and writes deterministic structural entities for project, directories, files, endpoints, dependencies, cross-project references, and code symbols (classes, functions, methods)
   - structural edges include `CONTAINS`, `DEPENDS_ON`, `TESTS`, `IMPORTS`, `EXPOSES`, `CALLS`, and `DEFINES` (file → symbol)
   - the deterministic scan is intentionally heuristic and currently strongest for Python projects; parser depth for other languages is present but shallower
   - symbol extraction uses the AST; each `:Symbol` node carries `name`, `kind`, `line_no`, `signature`, `docstring`, `parent`, and `decorator`; capped at 200 symbols per file
   - after the structural write, a best-effort narrative episode is queued so Graphiti can extract a semantic overview of the same project
   - **incremental diff**: on re-scan, stored `file_mtime` values are compared to current mtimes; only files with changed mtimes have their symbols deleted and rewritten — unchanged files are skipped entirely
   - **heat tracking**: `hot_count` on file/entrypoint/config/test nodes is incremented each time a file appears in the incremental diff; surfaces as `[hot:N]` in `query_structure("files")`
   - **background error surfacing**: fire-and-forget background writes (`_do_write`, `_background_symbol_rescan`) push errors to a server-side deque; `routes.backend_invoke` drains and attaches as `x-yawn-bg-warnings` header; `BackendClient` reads and stores; `BaseTool.execute` appends `[background-error]` lines to the next MCP tool response
   - `ingest_document` ingests a single doc/markdown/text file as a `structure_role="document"` Entity node (keyed on absolute path for uniqueness) and queues a narrative episode for Graphiti semantic extraction
   - available `query_structure` types: `projects`, `overview`, `files`, `imports`, `endpoints`, `tests`, `blast_radius`, `affected_tests`, `dependencies`, `cross_refs`, `symbols`, `context`, `documents`
     - `symbols` — list all classes/functions/methods in a file or directory (uses `:Symbol` nodes)
     - `context` — combined view: file summary + symbols + import graph for one file
     - `documents` — list document entities for a project, with optional path prefix filter

8. **Direct TODO graph**
   - `:Todo` nodes are stored directly in Neo4j as operator-managed durable work items
   - they bypass Graphiti, decay, compression, and semantic recall ranking
   - current integrations:
     - hook bootstrap shows the top open TODOs
     - `ContextBuilderService` appends matching TODOs for a query
     - `query_blast_radius` returns open TODOs linked to impacted files
   - this is currently a parallel graph surface, not part of the canonical memory lifecycle

## Data Flow

Concept id: `runtime.flow`

```text
Episode Text
  -> IngestService.ingest_episode(episode, session, source)
  -> [async lock] Graphiti client.add_episode()
  -> Graphiti performs: entity extraction, resolution, edge creation, episode anchor
  -> IngestService extracts UUIDs from Graphiti result
  -> MemoryGraphAdapter.stamp_ingest_metadata() applies policy fields via Cypher
  -> Returns IngestResult(episode_id, status, nodes_touched, edges_touched)
```

### Canonical self identity

Concept id: `runtime.canonical_self`

Menhir defines exactly one structural human-self target per **logical** namespace. Structural
identity is not semantic authority. In `enforce`, an exact Ed25519 owner signature is required for
each Graphiti assertion attached to that target; ordinary semantic retrieval, model output, typed
scalar/event writers, derived Views, and the dedup LLM cannot grant that authority. The default
`off` mode preserves the legacy resolver and makes no new protection claim.

**Why.** Graphiti resolves an extracted entity by cosine candidate search, escalating to an LLM
when several candidates share a normalized name. That boundary is probabilistic, and for the
entity named `user` it failed in production: the candidate window (15) saturated with exact-name
matches, so the deterministic single-match branch became arithmetically unreachable, every
extraction escalated, and a `duplicate_candidate_id = -1` verdict minted another fork. One
identity ended up split across dozens of nodes, crowding out real memories in recall.

**The contract.** The literal name is never authority. Binding requires trusted evidence that the
ingestion boundary owns:

| Layer | Responsibility |
|---|---|
| `domain/self_identity.py` | the ONE UUID formula and trusted turn/endpoint identity contract |
| `domain/self_authority.py` | immutable exact assertion payload, digest, policy, direction, polarity and temporal scope |
| `services/ingest_intake.py` | admission gate decides whether a `user`/`manual` claim is grounded |
| `services/enrichment_steps.py` | reconstructs identity, establishes the structural endpoint, and passes the read-only verifier |
| `infrastructure/self_authority.py` | verifies an external Ed25519 signature against a pinned raw-key fingerprint; never signs |
| `infrastructure/graphiti_extraction_patches.py` | freezes proposals, removes unsigned marker edges, and preserves/rechecks the signed tuple through final edge resolution |
| `infrastructure/self_binding.py` | atomically rewrites an authorized endpoint across the extraction payload |
| typed assertion/event/View repositories | reject alternate canonical-self attachments in `enforce` |
| `services/recall_pipeline.py` | excludes legacy unconfirmed self nodes, typed records and Views; rechecks signed Graphiti edges |
| `infrastructure/memory_queries.py` | keeps recent/flagged bootstrap reads from bypassing exact fact-edge verification |
| `infrastructure/graphiti_model_patches.py` | withholds a bound self and, in `enforce`, isolates canonical self from ordinary resolution |

Evidence survives the asynchronous queue in the episode's persisted `source`. That value is a
gate receipt rather than a caller's assertion: `evaluate_user_tier_claim` requires Menhir-owned
`TurnEvidence` with `role == "user"`, a matching session/namespace, and text grounded in that
turn, and rewrites anything ungrounded to `agent_inference` **before** persistence. This holds
only while `create_pending_episode` has exactly one production writer; a test pins that.

**Logical vs physical.** Identity is keyed by the LOGICAL namespace
(`uuid5(NAMESPACE_URL, "menhir-self:<logical>")`); the Graphiti partition is derived separately by
`namespace_to_group_id`, where logical `default` maps to physical `""`. The extraction context
carries the logical name explicitly so nothing has to reverse that mapping, which is ambiguous.

**Binding seam.** After combined extraction has built nodes, edges and the episode index map --
and after the relationless-repair pass, which replaces all three -- but before Graphiti acquires
candidates. The rewrite covers the node UUID, both edge endpoint directions and the index map as
one unit, with rollback: a node rewritten while its edges still point at the discarded UUID would
orphan the episode's facts.

**Authorship is not assertion authority.** Three separate questions must remain separate:

| Question | Answered by | Evidence |
|---|---|---|
| Who wrote this episode? | `eligible_self_evidence` | gate-approved persisted `source` |
| Which transport endpoint represents that author? | receipt-owned endpoint construction | Menhir-created marker scoped to the exact episode/turn/namespace |
| Which semantic assertion may attach? | `FileSelfAssertionAuthorizer` | exact owner-signed payload under the pinned Ed25519 key |

**No property of the extracted name answers the second question.** Three revisions tried to make
one answer it, and each had a counterexample inside a perfectly valid human turn:

| Attempted rule | Counterexample |
|---|---|
| the literal name `user` | an RBAC role, a `users` table, the customer a support turn is about |
| more than one self alias is ambiguous, one is proof | a lone RBAC `user`, unaccompanied |
| first-person grammar proves the author | reported speech: `She told me, "I will handle it"` |

The common error is treating a property of the extracted STRING as a fact about its PROVENANCE. The
receipt-owned endpoint is therefore transport, not permission. A missing endpoint, two payload
nodes carrying it, or a different node already carrying canonical identity raises a retryable
refusal. Even a valid endpoint edge is removed before candidate acquisition unless its final
structured assertion has an exact current owner signature. The signed payload binds principal,
logical namespace, external episode UUID, turn-evidence UUID, source-text SHA-256, lane, endpoint
direction, predicate/fact/object, polarity, temporal scope, claim revision, schema and policy.
Graphiti's internally generated episode UUID is never substituted for that external lineage.

The older combined-extraction endpoint-closure helper is deliberately **not** this resolver. It
may normalize a missing `I`/`me`/`user` edge endpoint to an ordinary entity named `user` so
Graphiti does not drop the edge and collapse the whole episode. It assigns no canonical UUID and
can still create a fork through ordinary dedup. Its old “canonical”/“bound” terminology was wrong;
the code and log now call this `self_like_endpoints_retained` so turn evidence cannot be mistaken
for node authority again.

**Production subject transport is projection-only.** The claim query atomically recognizes an
evidence projection only when it has exactly one `ADMITTED_ON` user/declarant turn, byte-identical
content, matching normalized namespace and projection lineage, and no diff. In enforce mode Menhir
derives an episode-scoped opaque endpoint from those durable identifiers and carries it through the
task-local extraction receipt. The marker changes neither the stored episode body nor ordinary
entities named `user`; it tells Graphiti which endpoint represents the current message author.

Every trusted `enforce` projection supplies the receipt's opaque endpoint to extraction regardless
of wording, polarity, tense, or whether the turn is phrased as a question. Grammar is not an identity
or authority gate. After the final relationless-repair payload exists, Menhir requires an emitted
marker node to participate in a current-episode edge and index entry, establishes that endpoint as
canonical structural self, strips model-authored authority fields, and holds every incident semantic
edge proposal-local. If an extractor ignores the endpoint and emits an unmarked `I`/`me`/`user`
fallback for a current-author reference, one bounded corrective extraction is allowed; a second miss
is quarantined with a refusal receipt rather than entering ordinary dedup. This refusal-only check
cannot grant authority, and ordinary application/RBAC users plus quoted/reported speakers remain
ordinary entities.

Graphiti then resolves each non-self counterpart normally. Only a counterpart that resolved to an
already-persistent identity can produce an owner-signable schema-v2 proposal, and that proposal
includes the exact persistent counterpart UUID as well as its name and labels. A newly synthesized
extraction UUID, missing resolution, changed duplicate choice, or same-name/different-UUID candidate
fails closed. Menhir verifies the owner signature only after this resolution. Unauthorized edges are
removed; nodes supported only by rejected edges are pruned. Because Graphiti's node-summary and
attribute prompts receive the full episode text, every self-proposal episode bypasses free-form node
hydration and performs name embeddings only, preserving existing node state. This prevents rejected
self language from escaping through a surviving counterpart summary.

Graphiti's later edge resolver is also inside the authority boundary. For an authorized self edge,
Menhir rechecks the external confirmation, requires the actual endpoint direction, resolved
counterpart UUID, name and labels, predicate, fact and temporal fields to equal the signed payload,
requires the relationship's physical `group_id`, external Menhir episode stamp, internal Graphiti
episode stamp and `episodes` attribution to agree, disables model dedup/invalidation, and reuses an
existing edge only on an exact tuple match. It then restores the signed temporal values and the
server-owned payload before persistence. A cloned edge, missing task-local authorization
capability, revoked confirmation or post-authorization mutation fails the episode; it cannot fall
back to an ordinary canonical-self write. Enforce-mode startup also refuses to proceed unless the
combined extractor, exact edge resolver, node-hydration guard, candidate-isolation wrapper and
canonical dedupe wrapper are all installed; the outer service builder cannot replace such a failure
with its degraded Graphiti sentinel. Missing confirmation removes the semantic edge while retaining
the raw episode and a bounded proposal receipt on `:Episodic`; malformed endpoint scope still
refuses the episode. Off and real observe extraction do not receive a marker and retain their
previous prompts and behavior.

That does **not** make `enforce` equivalent to `off`: `enforce` also activates canonical candidate
isolation. Even without a declaration, it refuses a searchable extracted node already carrying
canonical identity and removes canonical UUID/marker candidates from ordinary dedup. This prevents
an undeclared node from acquiring authority, but may preserve or create an ordinary fork instead.
`observe` deliberately leaves that resolution unchanged. On this single-owner deployment it is a
diagnostic mode, not an activation prerequisite: release acceptance uses the exact candidate image,
the live production provider/model, and a post-deploy disposable canary.
The production construction surface is pinned by an AST census: two context constructions inside
the sole factory, one factory call at Graphiti dispatch, and exactly one declaration call in the
final subject-endpoint validator. `self_like_unresolved` and `first_person_unresolved` in observe
mode count the population needing classification. They do **not** say which nodes provenance would
ultimately bind; first-person quoted speech is explicitly part of that upper bound.

Typed-scalar and event-history extraction have no owner-signature verifier. In `enforce`, their
self-shaped outputs remain advisory/proposal-only, their direct assertion repositories and the
shared View writer reject canonical-self attachment, rebind/restore operations refuse structurally
marked self targets, and default recall excludes legacy self assertions and Views. `off` and
`observe` preserve their legacy behavior. This deliberately reduces automatic personal recall until
those lanes implement the same exact confirmation contract.

Recent-memory and flagged-bootstrap readers cannot reconstruct and verify a complete relationship
tuple. In `enforce` they therefore exclude canonical-self nodes, self-derived Views and ordinary
counterpart summaries adjacent to structural self. Recallable UUID-less Views whose text subject is
a self alias are refused at the shared writer, and historical rows from before that writer guard are
excluded by both ordinary recall and generic context readers. The text check is gated on `is_view`,
so an ordinary Entity named `user` remains eligible; a new caller representing an ordinary
third-party `user` must provide its resolved non-self UUID where the View API supports that
distinction.

**Persisting the canonical node.** Graphiti saves a resolved node with `SET n = $entity_data`,
which REPLACES the property map. The bypass therefore commits the STORED canonical node when one
exists, and only falls back to the extracted node on a genuine `NodeNotFoundError`. A transient
driver error -- or a missing driver, which is the same failure through a different door -- must
fail the (retryable) episode rather than degrade into a sparse overwrite that erases markers,
provenance, flags and summary. On creation the extracted node is stamped with
`is_self`, `entity_role` and the logical namespace, because the generic ingest metadata stamp
supplies none of them.

**Canonical candidate isolation.** In `enforce`, an undeclared node cannot reach canonical self
through the ordinary resolver either. A searchable extracted node already carrying canonical
UUID/markers is refused before search. Candidate lists are then filtered (including
`existing_nodes_override`) to remove the active namespace's deterministic self UUID and every
structurally marked self node. Without that second fence, endpoint closure could retain an
ordinary entity named `user` and Graphiti's unique-exact branch could silently grant it canonical
authority despite the binder declining it. Both the declared extracted node and a stored
canonical node must also match the logical namespace's physical `group_id`; cross-group reuse is
a retryable failure.

**Canonical merge immunity.** Correlation must not undo a correct resolver result. The shared
merge-ineligibility predicate treats either `is_self=true` or case-normalized
`entity_role='self'` as a hard veto, so classifier checks and direct repository callers agree. The
final mutation statement repeats both marker predicates to close the preflight-to-write race, and
the rule applies whether canonical self is proposed as survivor or absorbed node. In `enforce`,
the same repository refuses ordinary-node merges and unmerge restores that would consume or recreate
an incident canonical-self relationship. Correlation and lifecycle `RELATES_TO` writers exclude
structurally marked self endpoints; bridge-and-delete and direct delete paths refuse a target whose
detach would erase a self relationship. Synthetic edge-fact repair cannot mutate an edge incident
to structural self or carrying an authorized-self payload. These low-point checks remain permissive
in `off` and `observe`.

**Rollout.** `canonical_self_binding_mode` (`MENHIR_CANONICAL_SELF_BINDING_MODE`) is
`off | observe | enforce`, default `off`. Runtime construction rejects an explicitly configured
unrecognized value; it must not silently turn an intended enforcement activation into `off`. The
low-level parser retains an `off` fallback only for compatibility callers that do not request
strict startup validation.
Observe never applies a production declaration and carries only a declaration-presence bit rather
than the opaque producer-supplied node identifier. `off` and `observe` do not apply canonical
candidate isolation, and real observe extraction does not receive a subject marker because that
would change its prompt. This preserves exact pre-change behavior and non-mutating diagnostics,
but means observe cannot forecast marker compliance. For this single-owner service, activation is
gated by the production-model corpus against the exact release image plus the post-deploy synthetic
canary; a discarded shadow extractor is built only if either exposes a concrete need.
Telemetry emits only the closed source kinds `user`/`manual`; every other caller-controlled source
string is reported as `other`.

**Confirmation and revocation.** Menhir reads a PEM Ed25519 public key, requires the configured
SHA-256 fingerprint of its raw 32-byte public key, and reads per-episode confirmation files from a
read-only directory. It exposes no signing endpoint and stores no private key or signature in the
graph. Authorized Graphiti edges carry the canonical signed payload JSON plus server-owned external
and internal episode-lineage stamps. Fact-edge recall
reconstructs that payload, compares it with the relationship's actual endpoints, stable counterpart
UUID, resolved counterpart name and labels, physical group, episode attribution, predicate, fact
and temporal fields, and re-verifies the current
external confirmation; deleting or changing a
confirmation therefore revokes it from default recall without deleting the episode or proposal.
Because Graphiti summaries may incorporate relationship language, free-form hydration is skipped
for the complete self-proposal episode and existing counterpart state is preserved. Generic
adjacency cannot prove an edge payload, so canonical self is removed from adjacency context in
`enforce`; confirmed self facts enter through a separately verified authority lane even when the
experimental general fact-edge feature is disabled or the query is not classified as historical.
Legacy self nodes/typed records/Views are also excluded. Direct inspection remains an operator
evidence surface, not authoritative recall.

The confirmation-file read and Graphiti relationship write cannot be one atomic transaction: the
file is deliberately external and read-only. Final resolution re-verifies immediately before save,
and every authoritative recall re-verifies again, so a confirmation changed in that narrow window
can at most leave a stored edge that is immediately excluded from authoritative recall. Historical
summaries created after an already-deleted or transformed self edge cannot be attributed by a
current-edge join; activation therefore still requires the separately authorized historical census
and remediation before claiming the pre-existing graph is clean. Neither residual weakens the
default-off prevention boundary implemented here.

**Consolidation is not a runtime operation.** `ensure_self_entity` creates or updates only the
canonical target; pre-existing forks are reported as `SELF_FORKS_REQUIRE_MIGRATION` and never
absorbed. The former `_absorb_self_entity_forks` bulk-rewired and `DETACH DELETE`d forks as a side
effect of an ordinary write, and dropped fork-to-canonical edges as "split artifacts"; it has been
removed, and its Cypher must not be revived as a migration template.

### Turn-evidence capture (ADR 0001)

Concept id: `runtime.turn_evidence_capture`

A separate, selective evidence path that feeds the Phase 3 personal-memory consolidation with real
user-authored input (distinct from the curated-memory ingest above). See
`.agent/adr/0001-conversation-turn-capture-surface.md` and
`.agent/plans/turn-capture-claude-hook.md`.

```text
Claude Code UserPromptSubmit (scripts/hooks/menhir_turn_evidence.py)
  -> deterministic, LLM-free triage: store only candidate prompts (number/money/possession/
     preference/decision/correction); drop boring ones. The MODEL never decides what is captured.
  -> POST /api/turn-evidence (agent tier), idempotent on turn_key
  -> :TurnEvidence {role:'user', declarant:'user', triage_reason[], ...}   (NOT :Entity/:Episodic)
  -> Phase 3 (consolidate_personal_memory) prefers user :TurnEvidence over the legacy user:-prefix
     Episodic path; perception -> fold_algebra -> View
  -> raw :TurnEvidence never enters normal recall; only derived Views do
```

Key rules: capture is selective (not transcript logging); the declarant is captured by the
lifecycle hook, never inferred from prose; the hook is stdlib-only, non-blocking (Menhir down =>
log + exit 0), and silent. Debug visibility: `perception_report.build_phase3_report` /
`format_phase3_report` render a "TurnEvidence Capture" section (totals, triage counts,
`phase3_records_selected`). Live hooks use server receive time as world time. Replay/import
producers may additionally supply `occurred_at`; scalar validity and evidence-projection reference
time use it, while dirty discovery and cursors continue to use server `recorded_at`.

## External Dependencies

Concept id: `runtime.dependencies`

- Neo4j endpoint:
  - `NEO4J_URI`, `NEO4J_DATABASE`, `NEO4J_USER`, `NEO4J_PASSWORD`
- local OpenAI-compatible endpoint settings:
  - `LLAMA_BASE_URL`, `LLAMA_API_KEY`
  - `LLAMA_CHAT_MODEL`, `LLAMA_EMBED_MODEL`
- direct OpenAI settings:
  - `OPENAI_API_KEY`, `OPENAI_CHAT_MODEL`, `OPENAI_EMBED_MODEL` (there is no `OPENAI_BASE_URL` -- not read anywhere; SSOT-07)
- Graphiti-specific backend selection:
  - `GRAPHITI_LLM_PROVIDER` (aliases: `MEMORY_GRAPHITI_PROVIDER`, `GRAPHITI_PROVIDER`) -- selects the provider only; there is no separate `GRAPHITI_LLM_BASE_URL`/`GRAPHITI_LLM_API_KEY`/`GRAPHITI_LLM_CHAT_MODEL` (endpoint/key/model come from the selected provider's own settings above, e.g. `OPENAI_*` or `LOCAL_LLM_*`/`LLAMA_*`)
  - `GRAPHITI_EMBED_PROVIDER`, `GRAPHITI_RERANKER_PROVIDER` (each inherits `GRAPHITI_LLM_PROVIDER` when unset) -- likewise no separate `GRAPHITI_EMBED_BASE_URL`/`GRAPHITI_EMBED_API_KEY`/`GRAPHITI_EMBED_MODEL`
- yawn.scheduler (primary process manager for llama-server):
  - `SCHEDULER_URL` (default `http://localhost:8082`)
  - local scheduler URLs are normalized to loopback IP form (`127.0.0.1`) before memory-side probes and acquire calls, which avoids transient localhost-resolution differences between processes
- runtime preflight checks are side-effect free by default; only backend runtime initialization opts into scheduler-backed endpoint acquisition during startup validation
- Graphiti add-episode watchdog probes now use the scheduler's lightweight `/watchdog-status` endpoint instead of the heavier `/status` payload so long-running requests do not get false `scheduler_status_unavailable` stalls while the scheduler is busy
  - on Windows, scheduler autostart now launches `manager.py` as a hidden direct child process (`CREATE_NO_WINDOW`) so MCP startup does not spray visible PowerShell/terminal windows
  - when `menhir` runs under WSL/bash, scheduler autostart now normalizes Windows drive-letter model/bin paths from `yawn.scheduler/.env` to `/mnt/<drive>/...` before launch, so remote SSH into WSL can reuse the same scheduler config
  - `LLMAdapter` and `GraphitiClient` attempt `POST /acquire` and fall back to `LLAMA_BASE_URL` on failure
  - Graphiti recall/search/ingest paths re-issue acquire calls per operation as a wake ping so idle timeout recovery is automatic
  - background enrichment heartbeat now sends periodic `/ping` keepalives only when the Graphiti client is actually using a scheduler-managed local llama endpoint
  - If scheduler is not running at MCP startup/runtime acquire, `llama_endpoint` will auto-start `yawn.scheduler` (`manager.py`) before continuing
  - during runtime init, `menhir` registers itself as a generic scheduler task source
  - enrichment now emits parent-job updates keyed by episode UUID plus child-task updates for Graphiti `add_episode` work, using episode-scoped scheduler task ids like `memory-<episode>--graphiti-add-episode`
- Langfuse (optional observability):
  - `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
  - when configured, both direct `LLMAdapter` calls and Graphiti's OpenAI-compatible clients are traced

## Storage

Concept id: `runtime.storage`

Primary storage is Neo4j.

- Graph nodes/edges are managed through Graphiti abstractions + `MemoryGraphAdapter` policy layer.
- Schema extensions and property contracts are tracked in `data_models.md`.
- **Before modeling a new feature**, read `model.primitives` in `data_models.md`. Four
  general rules govern how objects, subordinates, declarations and locators relate;
  they are not specific to todos or artifacts, and rediscovering them per feature is
  how duplicate vocabulary gets introduced.
- Graphiti prompt JSON serialization is patched at runtime to handle Neo4j temporal values.

### Work-artifact corpus reconciliation

Concept id: `runtime.storage`

File state and semantic state have different authorities, and the seam between them is three
modules with no overlap:

- `domain/artifact_reconciliation.py` — pure. Route table, raw-byte hashing, authored-metadata
  reader, and the match planner that turns "what is on disk" plus "what the graph holds" into
  actions. No Neo4j, no filesystem, no Git, which is what makes the whole match matrix testable
  offline.
- `infrastructure/artifact_corpus_scanner.py` — reads files and asks Git. Produces entries; decides
  nothing.
- `services/artifact_reconciliation_service.py` — the single corpus collector, used by
  `menhir artifacts` (audit / validate / reconcile), the `audit_artifact_corpus` MCP tool, and the
  startup recovery pass. `scripts/migrate_work_artifacts.py` is now a thin wrapper over it rather
  than a second collector, which is the drift that caused the corpus split it repairs.

Audit is read-only. Apply re-derives the plan and refuses unless the caller supplies the digest of
the ledger they approved, so an approved plan cannot be applied to a state it was not approved
against. Detectors may relocate and refresh sources; lifecycle, supersession, retyping and
relationships stay behind explicit MCP operations.

Each graph repository has one `ArtifactReconciliationCursor` containing the last commit reached by
a clean apply. Audit uses that cursor as its default Git rename-evidence base and exposes both the
stored cursor and selected base in the digest-bound report. `--from-commit` overrides evidence only.
Apply compares the stored cursor again before writing and advances it with compare-and-set only when
there are no conflicts or skipped writes and Git supplied an observed commit.
If Git cannot evaluate the selected cursor-to-HEAD interval, audit marks the evidence base invalid
and apply refuses before artifact mutation; zero rename records are not treated as proof that no
rename occurred.

An authored UUID may identify a `WorkArtifact` that exists without an embodiment. Audit reads those
semantic identities separately from source snapshots and proposes `ATTACH_SOURCE`, not
`REGISTER_ARTIFACT`. The conditional write creates only `ArtifactSource` plus `EMBODIED_IN`; graph
type disagreement, an existing source, or a claimed locator refuses without changing semantic
artifact properties.

Sources with no repository identity are queried separately and only when relevant to the scanned
paths or declared artifact UUIDs. They participate in the digest and collision checks but never in
path- or hash-based automatic matching. A document-declared owner UUID produces the explicit
`ADOPT_SOURCE_REPOSITORY` action; otherwise audit emits `UNSCOPED_SOURCE_REPOSITORY`. Apply assigns
the repository through the same conditional source relocation used for normal moves, preserving the
source node and refusing a stale locator, stale integrity, or newly occupied destination.

Operational sidecar storage:

- MCP/server telemetry is persisted in SQLite at `<workspace_root>/.agent/mcp_telemetry.db` by default (resolved by `infrastructure/paths.py` from the workspace root, not a hardcoded project path)
- this SQLite file is the first place to inspect when estimating real usage, queue pressure, failure rates, and likely LLM cost
- `mcp_events` records operation timings plus serialized input/result sizes
- `failure_events` records structured enrichment/scheduler failures
- `episode_task_events` records per-episode LLM task events (phase, kind, model, endpoint, scheduler task) and is now populated directly from ingest-side Graphiti LLM usage instrumentation
- `llm_usage_events` records one terminal row per instrumented provider-client call, correlated by
  `call_id`. It preserves provider-reported input, output, total, cached-input, and reasoning-output
  tokens plus the raw usage payload, duration, operation, endpoint, model, run, and episode. Counts
  are never estimated; completed calls without provider usage remain explicitly measurable as
  `missing_usage_calls` in aggregates.
- async OpenAI-compatible chat/response/embedding calls, Gemini REST chat calls, synchronous scalar
  chat, and synchronous View embedding all use the same instrumentation boundary. An
  episode-scoped callback attaches ingest provenance; a process-wide fallback captures scheduler
  and maintenance calls without double-writing episode calls.
- token telemetry is the durable usage source. Monetary cost remains a derived report concern so
  historical token evidence is not rewritten when provider price schedules change.
- debug/lifecycle traces now persist to a dedicated `lifecycle_events` stream in the same SQLite sidecar so MCP boot, runtime init, queue transitions, and pre/post Graphiti call boundaries can be inspected without relying on transient stderr logs
- Recall Lab experiments persist to `recall_lab_runs` in the same sidecar, including exact arm settings, privacy-filtered displayed results, and blinded-judge verdicts
- startup reconciliation now recovers orphaned/stale `ENRICHING` rows before normal queue resume, but rows that have already exhausted retry budget are failed instead of being recycled into dead `PENDING`
- graceful MCP runtime shutdown now releases any `ENRICHING` rows owned by the current worker, again failing exhausted rows instead of requeueing work that no worker can claim
- `failure_events` now distinguish malformed Graphiti output (`graphiti_invalid_output`, manual review) from oversized preflight rejection (`graphiti_preflight_rejected`, terminal), so retry sweeps stop wasting budget on those cases

## Roadmap State

Concept id: `runtime.roadmap`

The project is in **post-v1 hardening** mode (M0-M7 complete):

- Keep API and policy surface intentionally narrow.
- Scoring, lifecycle, consolidation, conflict governance, and ops hardening are all complete.
- Defer post-v1 automation and full traversal-query language until base behavior is stable.
- MCP server is live with deferred enrichment, SQLite telemetry, maintenance scheduler, circuit breakers, LLM budget caps, context builder, and embedding cache.
- Current focus is follow-on hardening and operator ergonomics: replay tooling, richer runbooks, metrics/SLO visibility, capacity controls, and larger-scale validation.
