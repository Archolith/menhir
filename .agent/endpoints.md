# MCP Endpoints / Tools: menhir

Do not preload this entire file by default. Start with `README.md` and `concept-ids.md`, then open only the
tool or resource section you need.

## Quick Index

- Need ingest / queueing tools: read `mcp.tool.add_memory`, `mcp.tool.add_memory_and_track`, `mcp.tool.get_enrichment_status`, and `mcp.tool.list_enrichment_queue`
- Need troubleshooting tools: read `mcp.tool.watch_enrichment`, `mcp.tool.get_episode_trace`, and `mcp.tool.repair_stale_enrichment`
- Need recall / memory tools: read `mcp.tool.read_flagged_memories`, `mcp.tool.recall_context_memories`, and `mcp.tool.recall_memories`
- Need operator / repair tools: read `mcp.tool.list_conflicts`, `mcp.tool.resolve_conflict`, and `mcp.tool.force_scheduler_takeover`
- Need TODO / task tools: read `mcp.tool.add_todo`, `mcp.tool.list_todos`, and `mcp.tool.close_todo`
- Need artifact corpus parity or a source move: read `mcp.tool.audit_artifact_corpus` and `mcp.tool.relocate_artifact_source`
- Need session identity / elapsed time: read `mcp.tool.get_client_context`
- Need project file/import/test graph: read `mcp.tool.query_structure`
- Need lightweight inspection: jump to `mcp.resource.system.*` or `mcp.resource.memory.*`

This server exposes Model Context Protocol (MCP) tools and resources for memory management and recall.

## Surface Boundaries

Use this file for MCP tool/resource contracts and the Explorer HTTP surface that shares the same server.

Do not treat it as the source of truth for every HTTP surface. The runtime now has three distinct access layers:

- Explorer HTTP (benchmark task inspection):
  - `/explorer/recall-lab/bench-runs` — HTML list of discovered LME benchmark runs
  - `/explorer/recall-lab/bench-runs/{run_id}` — HTML run detail with task list and arm scores
  - `/explorer/recall-lab/bench-runs/{run_id}/tasks/{namespace}` — HTML task detail with evidence,
    assertions, views, scores, and derivation classification
  - `/explorer/api/recall-lab/bench-runs` — JSON list of runs
  - `/explorer/api/recall-lab/bench-runs/{run_id}` — JSON run detail
  - `/explorer/api/recall-lab/bench-runs/{run_id}/tasks/{namespace}` — JSON task detail
  - Contract: `bench-inspection/v1` returned in every API response.
  - Non-active runs show artifact-only warnings; only the configured active run (`MENHIR_BENCH_ACTIVE_RUN_ID`)
    attempts live graph queries.
  - All text-carrying fields pass through the same Explorer reveal/redaction policy.
  - Catalog reads from `MENHIR_BENCH_RESULTS_ROOT` (required; fail closed when absent). When unset the
    catalog is `is_configured=False` and the list page shows a configuration warning.

Public REST:
  - `/api/health`
  - `/api/ready`
  - `/api/stats`
  - `POST /api/recall` — ranked memories plus structured scalar authority. Each result includes
    `temporal_facts`; `valid_at`/`invalid_at` are source/world time, while
    `created_at`/`expired_at` are Menhir belief time. Set `include_invalidated=true` when a consumer
    needs superseded source-time evidence for previous/later/changed comparisons; this enriches
    already-selected memories and does not add invalidated candidates. When event-history authority
    is enabled (default off) and a namespace is present, may also attach `event_authority_layer` (a
    structured first-person latest/predecessor verdict; see the Event-history surface section below).
  - `/api/turn-evidence` (agent tier) — selective `:TurnEvidence` capture (ADR 0001); see `data_models.md` and `architecture.md`
  - `POST /api/phase3/run` (agent tier) — run one personal-memory View consolidation pass over an explicit namespace (real LLM, all bias guards on); black-box surface for the `archolith-bench menhir-phase3` benchmark. When event history is enabled (default off), also runs event consolidation over an independent `:EventConsolidationWatermark` cursor and reports bounded Phase-3 event metrics (`event_namespaces_processed`, `event_namespaces_failed`, `event_assertions_recorded`, `event_assertions_created`, `event_views_rebuilt`, `event_llm_calls`).
  - `GET /api/phase3/status` (readonly) — namespace dirty flag + `:TurnEvidence` count
  - `GET /api/views` (readonly) — current counter Views (each with value `history`/`superseded`) and `subject='perception'` abstention receipts, split
  - `POST /api/phase3/reset` (agent tier) — tear down a throwaway namespace: graphiti partition (Views + watermark) PLUS namespace-keyed `:TurnEvidence`; refuses the default/shared namespace
  - public memory endpoints
- Internal backend transport:
  - `/api/internal/backend/*`
  - used by `BackendClient`
  - not part of the public OpenAPI contract
- Remote MCP over HTTP:
  - tool-only
  - narrower than stdio MCP by design
  - every tool descriptor includes a title, input schema, reviewed MCP safety annotations, and a
    minimum OAuth `securitySchemes` scope (`menhir:read`, `menhir:write`, or `menhir:admin`)
  - an OAuth invocation-tier denial returns `isError=true` with
    `_meta["mcp/www_authenticate"]`; tenancy, allowlist, and domain refusals do not

Practical rule:

- use `endpoints.md` for MCP tools/resources
- use `architecture.md` plus `workflows/backend-first-mcp.md` for public REST, internal backend transport, and remote-vs-stdio boundary decisions

## Event-history surface: no dedicated endpoint

There is **NO dedicated event-history endpoint, tool, resource, or response contract** for Event
History Phases 1–5 at `370eff1`. The landed code (`domain/event_history.py`,
`infrastructure/typed_event_repository.py`, `infrastructure/view_query_repository.py` event-lane
methods, `services/event_history_service.py`, `services/event_history_perception.py`,
`services/event_consolidation.py`, `services/event_history_recall.py`,
`services/event_history_authority.py`) is reachable only through `MemoryGraphAdapter` delegates and
the existing, default-off recall/phase3 surfaces. There is no consumer-facing way to query event
histories or verdicts directly.

Event history is **default off** — no dedicated endpoint and no default enablement. When enabled, the
event surface rides existing transports rather than adding new ones:

- **Recall authority (default off):** when `personal_memory_event_history_authority_enabled` is true
  AND a namespace is present, `POST /api/recall` may attach `event_authority_layer` (a structured
  first-person latest/predecessor `EventAuthorityVerdict`, separate from `authority_layer`); the MCP
  `recall_memories` tool and the `ContextBuilderService` context block carry the same field. When off
  (the default) the field is absent, preserving flag-off wire output.
- **Consolidation / phase3 run (default off):** when `personal_memory_event_history_enabled` is true,
  the scheduled personal-memory job and the manual `POST /api/phase3/run` run event consolidation over
  an independent `:EventConsolidationWatermark` cursor and report bounded Phase-3 event metrics
  (`event_history_enabled`, `event_namespaces_processed`, `event_namespaces_failed`,
  `event_assertions_recorded`, `event_assertions_created`, `event_views_rebuilt`, `event_llm_calls`).

Do not mistake the adapter-only seam or these default-off transport attachments for product
enablement: there is no dedicated event-history consumer surface today.

## Authentication (HTTP API)

`BearerAuthMiddleware` (`src/menhir/api/auth.py`) guards every `/api/*`, `/mcp/*`, and
`/mcp-http` request except the exempt paths `/api/health` and `/api/ready`.

Token tiers (sent as `Authorization: Bearer <key>`):

| Env var | Tier | Notes |
|---------|------|-------|
| `MENHIR_OPERATOR_KEY` | `operator` | full access (Claude, Codex, human operator) |
| `MENHIR_AGENT_KEY` | `agent` | scoped agent access (Qwen, Gemini, Reasonix) |
| `MENHIR_READONLY_KEY` | `readonly` | dashboards / read integrations |
| `MENHIR_API_KEY` | — | **back-compat alias for the operator key**: if `MENHIR_OPERATOR_KEY` is unset, `MENHIR_API_KEY` becomes the operator token. If both are set, `MENHIR_OPERATOR_KEY` wins. |

Behaviors that surprise callers:

- **No keys set => auth disabled.** If none of operator/agent/readonly (or the api_key
  alias) is configured, the middleware passes every request through (local dev mode).
  This is how a throwaway/benchmark instance runs unauthenticated.
- **Keys are read from the process environment**, not only `.env`. On this workstation the
  real keys live in the **Windows user environment**, so an empty `.env` (or `ENV_FILE`
  pointing at an empty file) does **not** clear them — you must export the key vars empty
  to disable auth for a local instance.
- **`/mcp/` and `/mcp-http`** also accept the key via `?api_key=` query param (for
  connectors that cannot set headers); it is stripped from the scope after validation.
- Invalid/missing token on a protected path returns `401 {"code": "unauthorized"}`.
- **`tools/list` is scoped to the caller's tier** (`TierFilteredFastMCP` in
  `src/menhir/api/mcp_remote.py`): `readonly` sees 15 tools, `agent` 26, `operator` all 44.
  Invocation was already gated in `contracts.py`; filtering the advertised catalog too is
  defense in depth, and it stops small models from spending prompt budget on — and
  mis-selecting from — tools they cannot invoke. An **empty** tier (local stdio trust,
  CT-002; or no-auth loopback) is deliberately **not** filtered, matching the invocation gate.
  Note the gateway's `always_visible` pinning in `mcp/server.py` does **not** apply here: the
  remote surface uses the MCP SDK's `FastMCP` (`mcp.server.fastmcp`), which has no
  tool-transform hook, whereas the gateway uses the separate `fastmcp` v2 package.

Throwaway/benchmark recipe (unauthenticated, local only):

```bash
export ENV_FILE=/path/to/empty.env   # don't load the real .env
export MENHIR_OPERATOR_KEY="" MENHIR_AGENT_KEY="" MENHIR_READONLY_KEY="" MENHIR_API_KEY="" # gitleaks:allow — deliberately empty
# -> BearerAuthMiddleware disables; bind to 127.0.0.1 only.
```

## Tools

Group ids: `mcp.group.ingest`, `mcp.group.processing`, `mcp.group.conflicts`, `mcp.group.recall`, `mcp.group.operator`, `mcp.group.context`, `mcp.group.stats`, `mcp.group.todos`, `mcp.group.structure`

### `add_memory`
Concept id: `mcp.tool.add_memory`

Queue a memory for enrichment. Use this to remember facts, preferences, decisions, or anything worth keeping.
- **`text`** (str): The memory content to store. Be specific and self-contained. **Canonical key** — use this in new code.
- **`content`** (str, optional): Alias for `text`. Accepted for backward compatibility. Precedence: `text` > `content`.
- **`summary`** (str, optional): Alias for `text`. Accepted for backward compatibility. Precedence: `text` > `content` > `summary`.
- **`source`** (str, optional): Where this memory came from (default: `claude-code`).
- **`diff`** (str, optional): Git diff to attach as code-change context. Pass `git diff HEAD` or `git show <hash>` output after a commit so Graphiti can reason about what changed alongside the memory text. Ideal for refactors, bug fixes, and feature additions where the code change is inseparable from the decision. Very large diffs are truncated before Graphiti extraction.
- **`flagged`** (bool, optional): Permanent retention. **USER-ONLY — never set this unless the user
  explicitly asked in this session.** See the warning under `flag_memory`; the flag propagates to
  every entity extracted from the episode, including pre-existing shared ones. Default `false`.
- Response now appends a one-line queue snapshot: `queue_depth`, current `active_enriching` count, and scheduler state.

**Gateway alias behavior**: `memory_gateway(action="add_memory", payload_json='{"text":"..."}')` accepts `text` (preferred), `content`, or `summary` for the memory content field.

### `add_memory_and_track`
Concept id: `mcp.tool.add_memory_and_track`

Queue one memory and return enrichment status updates until `READY`, `FAILED`, or timeout.
- **`text`** (str): Memory content to ingest.
- **`source`** (str, optional): Source provenance label.
- **`diff`** (str, optional): Optional git diff attached to the episode as change context.
- **`timeout_s`** (float, optional): Max seconds to wait for completion (default: 60).
- **`poll_interval_s`** (float, optional): Status polling interval in seconds (default: 1).
- Output includes stage/progress/step telemetry plus live LLM task counters (`attempt` and `total`).
- Output begins with `queued_summary` so callers can see queue depth, active enrichment count, and scheduler state immediately after enqueue.
- Very large memories can now fail early with `episode_preflight_too_large` instead of spending retry budget inside Graphiti extraction.
- **`diff`** (str, optional): Git diff to attach as code-change context — same semantics as `add_memory`. Use when you want live enrichment tracking alongside the code change that motivated the memory.
- Diffs are appended to the episode body with a separator before Graphiti extraction; very large diffs are truncated before send.

### `ingest_project`
Concept id: `mcp.tool.ingest_project`

Scan a project directory and ingest its deterministic structure into the graph.
- **`path`** (str): Absolute path to the project root.
- **`name`** (str, optional): Project-name override.
- **`force`** (bool, optional): Re-scan even if the stored fingerprint matches.
- Writes structural entities and edges directly, then queues a best-effort semantic narrative episode for Graphiti extraction.
- Current parser quality is strongest for Python projects; dependency/import/endpoint detection for other stacks is heuristic and intentionally incomplete.

### `ingest_document`
Concept id: `mcp.tool.ingest_document`

Ingest a single document or text file into the memory graph.
- **`path`** (str): Absolute path to the file (.md, .txt, .rst, .adoc, or any text file).
- **`project`** (str, optional): Project/namespace label. Defaults to the parent directory name. Use the same value as the associated `ingest_project` call to co-locate the document with its project's structural graph.
- Creates a `structure_role: "document"` Entity node in Neo4j (queryable via `query_structure("documents", project=...)`).
- Queues the file content as a narrative episode for Graphiti semantic extraction (first 4000 chars).
- Content excerpt (first 2000 chars) is stored on the entity node for quick lookup.
- File path uniqueness: `structure_path` is the resolved absolute path, so two docs with the same name in different directories always produce separate nodes.

### `add_candidate`
Concept id: `mcp.tool.add_candidate`

Stage a low-trust memory/friction candidate for human review — not recalled until
approved via `resolve_conflict`/the Explorer's candidate approval flow.
- **`content`** (str): The candidate content to stage for review.
- **`source`** (str): Source label for where this candidate originated.
- **`cluster_id`** (str): Cluster identifier for grouping related candidates.
- **`label`** (str): Human-readable label for this candidate.
- **`kind`** (str, optional): Type of candidate (default: `memory`).
- **`candidate_type`** (str, optional): Candidate classification (default: `other`).
- **`type`** (str, optional): Memory type (default: `SEMANTIC`).
- **`evidence_strength`** (str, optional): Strength of supporting evidence (default: `REPEATED`).
- **`distinct_sessions`** (int, optional): Number of distinct sessions providing evidence (default: 0).
- **`first_seen`** / **`last_seen`** (str, optional): ISO dates when first/last observed.
- **`notes`** (list[str], optional): Supporting notes or context.
- **`source_confidence`** (float, optional): Confidence level from 0.0 to 1.0 (default: 0.5).
- Returns confirmation with the candidate uuid, cluster_id, and review status.

### `get_enrichment_status`
Concept id: `mcp.tool.get_enrichment_status`

Inspect one episode's enrichment status, with optional wait for completion.
- **`episode_uuid`** (str): Episode UUID to inspect.
- **`wait`** (bool, optional): Poll until terminal state or timeout.
- **`timeout_s`** (float, optional): Max wait seconds when `wait=true`.
- **`poll_interval_s`** (float, optional): Poll interval in seconds.
- Output includes stage/progress/step telemetry, `processing_substage`, heartbeat, current LLM task metadata, and per-episode LLM task counts.

### `watch_enrichment`
Concept id: `mcp.tool.watch_enrichment`

Follow one enrichment live and return only observed deltas until terminal state or timeout.
- **`episode_uuid`** (str): Episode UUID to watch.
- **`timeout_s`** (float, optional): Max wait seconds before returning latest observed state.
- **`poll_interval_s`** (float, optional): Poll interval in seconds.
- Output is delta-oriented and optimized for live troubleshooting, including `processing_substage` and current LLM task metadata when observed.

### `get_episode_trace`
Concept id: `mcp.tool.get_episode_trace`

Return a compact debug bundle for one episode by combining the live queue row with telemetry-sidecar history.
- **`episode_uuid`** (str): Episode UUID to inspect.
- **`limit`** (int, optional): Max task/failure/lifecycle events to include per stream (default: 20, max: 50).
- Returns compact JSON with:
  - `current` live processing fields
  - `task_events[]` from `episode_task_events`, populated from ingest-side per-episode LLM usage events
  - `failure_events[]` filtered to the episode
  - `lifecycle_events[]` filtered to the episode
- Use this when `watch_enrichment` is too coarse and you need to know whether a job was claimed, queued, failed, or released without scraping logs.
- Recent failure rows may now include `graphiti_invalid_output` for malformed JSON/schema output and `graphiti_preflight_rejected` for oversized memories blocked before Graphiti was called.

### `get_provenance`
Concept id: `mcp.tool.get_provenance`
Required tier: `readonly`

Show a memory/View node's receipts: the source episodes it was built from, plus its
first-class evidence anchors, so you can verify a summary/claim against its sources.
Pass a `node_uuid` from a recall result (e.g. a View/counter node) to expand it into the
episodes that `MENTIONS` it. Read-only.
- **`node_uuid`** (str): UUID of the node to expand.
- **`content_chars`** (int, optional): Max characters of each episode's content to return
  (0–5000, default: 500).
- Returns JSON with the node's name/view_kind, its source episodes
  (uuid/source/content/created_at), `SUPPORTED_BY` evidence, and `ANCHORED_TO` structural
  paths.

### `list_enrichment_queue`
Concept id: `mcp.tool.list_enrichment_queue`

List episodic processing rows with stale-state hints for queue troubleshooting.
- **`state`** (str, optional): `active`, `all`, `pending`, `enriching`, `ready`, or `failed` (default: `active`).
- **`limit`** (int, optional): Max rows to return (default: 25, max: 200).
- Row diagnostics include stage/progress/steps and current-attempt LLM task count.

### `repair_stale_enrichment`
Concept id: `mcp.tool.repair_stale_enrichment`

Inspect and optionally repair stale `ENRICHING` episodes (expired or missing lease).
- **`dry_run`** (bool, optional): Preview only when true (default: true).
- **`limit`** (int, optional): Max stale rows to inspect (default: 100, max: 500).
- Recovery only requeues rows that are still retryable; exhausted rows are failed instead of being reset to dead `PENDING`.

### `force_reenrich`
Concept id: `mcp.tool.force_reenrich`

Force a failed episode back into enrichment and track it live.
- **`episode_uuid`** (str): UUID of the failed episode to re-enrich.
- **`wait`** (bool, optional): Poll until enrichment completes or times out (default: true).
- **`timeout_s`** (float, optional): Max seconds to wait when `wait=true` (default: 300).
- **`poll_interval_s`** (float, optional): Poll interval in seconds (default: 2).
- Resets `processing_attempts` and `processing_error`, pushes the episode to the front of the enrichment queue.

### `force_release_enrichment_lease`
Concept id: `mcp.tool.force_release_enrichment_lease`

Force-release one ENRICHING episode lease.
- **`episode_uuid`** (str): UUID of the episode to release.
- **`requeue`** (bool, optional): Whether to requeue the released episode (default: true).
- Exhausted rows are failed instead of being requeued.

### `list_conflicts`
Concept id: `mcp.tool.list_conflicts`

List grouped memory conflicts for operator review.
- **`status`** (str, optional): `unresolved`, `resolved`, `auto-resolved`, or `all` (default: `unresolved`).
- **`limit`** (int, optional): Max groups to return (default: 25, max: 200).
- Returns compact JSON grouped by `conflict_group_id`, with ordered members (`older` / `newer`) and a machine-usable `suggested_resolution` object.

### `resolve_conflict`
Concept id: `mcp.tool.resolve_conflict`

Resolve one conflict group using explicit operator intent.
- **`group_id`** (str): Conflict group id.
- **`action`** (str): One of `keep_both`, `replace`, or `discard_new`.
- **`keep_uuid`** (str, optional): UUID to retain for `replace` / `discard_new`.
- **`remove_uuid`** (str, optional): Optional UUID to remove for `replace` / `discard_new`. When omitted, all non-`keep_uuid` members are removed.
- **`dry_run`** (bool, optional): Preview the exact mutation plan without writing (default: `false`).
- **`allow_promoted_removal`** (bool, optional): Override PROMOTED-node protection for destructive resolution (default: `false`).
- Dry-run and apply responses are compact JSON.
- Dry-run output includes deterministic preview fields (`nodes_resolved`, `nodes_gone`, `group_clear_count`, `bridge_count_estimate`).
- Apply output includes verification fields (`group_cleared`, `remaining_unresolved_in_group`, `journal_op_id`).

### `scan_for_conflicts`
Concept id: `mcp.tool.scan_for_conflicts`

Scan persistent entity nodes for similarity-based conflicts.
- **`limit`** (int, optional): Max nodes to scan (default: 500).
- New pairs are written as `pending_llm_review` — run `run_llm_conflict_review` afterwards to confirm.

### `run_llm_conflict_review`
Concept id: `mcp.tool.run_llm_conflict_review`

Run LLM contradiction confirmation on `pending_llm_review` conflicts immediately.
- **`limit`** (int, optional): Max groups to process (default: 20).
- Confirmed contradictions become `unresolved` (visible in `list_conflicts`), false positives are cleared.

### `requeue_conflicts_for_llm_review`
Concept id: `mcp.tool.requeue_conflicts_for_llm_review`

Re-queue conflict groups for LLM contradiction confirmation.
- **`from_status`** (str, optional): Source status to re-queue (`unresolved`, `false_positive`, `auto-resolved`). Default: `unresolved`.
- **`limit`** (int, optional): Max groups to re-queue (default: 200).

### `force_scheduler_takeover`
Concept id: `mcp.tool.force_scheduler_takeover`

Force this MCP process to take scheduler lease ownership for troubleshooting.
- **`reason`** (str, optional): Short operator reason for audit context.

### `pause_scheduler`
Concept id: `mcp.tool.pause_scheduler`
Required tier: `operator`

Stop the maintenance scheduler loop without restarting the process. Prevents background
enrichment retries, stale-lease recovery, and conflict jobs from running. In-flight
enrichment already leased by a worker continues to completion. Use `resume_scheduler` to
restart the loop.
- Returns scheduler state after pause (`was_running`, `scheduler_running`, `lease_acquired`).

### `resume_scheduler`
Concept id: `mcp.tool.resume_scheduler`
Required tier: `operator`

Restart the maintenance scheduler loop after a `pause_scheduler` call. Attempts to
re-acquire the scheduler lease and restart background jobs (enrichment retries,
stale-lease recovery, conflict jobs, structure watcher).
- Returns scheduler state after the resume attempt (`start_succeeded`, `scheduler_running`,
  `lease_acquired`, `lease_blocked_reason`).

### `read_flagged_memories`
Concept id: `mcp.tool.read_flagged_memories`

Read flagged memories for startup bootstrap context.
- **`reader_id`** (str, optional): Stable bot/client identifier used for bootstrap gating (default: `default`).
- **`limit`** (int, optional): Max flagged memories to return (default: 10, max: 50).
- **`workspace`** (str, optional): Empty selects `general` pins only. A key selects `general` plus the exact normalized `workspace:<key>` pins.
- This call records that `reader_id` has read the current flagged-memory version for that exact bootstrap selection.
- Returns compact JSON with `items[]` containing summary-sized flagged memory entries for bootstrap efficiency.
- Derived Views are returned only when current, not retired, and fully backed by live `:Episodic` or `:TurnEvidence` contributors; internal, superseded, candidate, gone, and orphaned Views are excluded.

### `recall_context_memories`
Concept id: `mcp.tool.recall_context_memories`

Read non-flagged startup context (relevant + recent) after flagged bootstrap.
- **`reader_id`** (str, optional): Stable bot/client identifier used for bootstrap gating (default: `default`).
- **`query`** (str, optional): Optional natural-language query for relevant context retrieval.
- **`preset`** (str, optional): Ranking strategy for relevant retrieval. One of `knowledge` (default), `recent`, `connected`, `emotional`, `conflict`.
- **`limit`** (int, optional): Max relevant results to return (default: 5).
- **`recent_limit`** (int, optional): Max recent context rows to include (default: 5).
- **`namespace`** (str, optional): Exact recent/relevant memory silo. Recent retrieval excludes every structural node.
- **`workspace`** (str, optional): Explicit bootstrap selection; defaults to `namespace` when omitted.
- Requires a matching `read_flagged_memories(reader_id=..., workspace=...)` receipt for the current scoped flagged-memory version.
- Returns compact JSON with `relevant[]` and `recent[]` arrays using summary-sized memory items; `relevant[]` also includes similarity for scored hits.

### `recall_memories`
Concept id: `mcp.tool.recall_memories`

Search memories by semantic similarity. Returns ranked results with relevance scores.
- **`query`** (str): What to search for. Natural language works best.
- **`preset`** (str, optional): Ranking strategy. One of `knowledge` (default), `recent`, `connected`, `emotional`, `conflict`.
- **`limit`** (int, optional): Max results to return (default: 5).
- **`file_context`** (str, optional): File path whose structural neighborhood should inject linked semantic candidates.
- **`file_context_project`** (str, optional): Optional project disambiguation for `file_context`.
- **`trace`** (bool, optional): Observe-only diagnostics. On the default fused path, records BM25 and cosine ranks without changing returned ordering; failures are warnings, not recall failures.
- Gateway alias: `memory_gateway(action="search", payload_json="{...}")` dispatches to the same behavior as `action="recall"`.
- Returns compact JSON with top results, short summaries, a compact score breakdown (`sim`, `adj`, `rec`, `prom`), and explicit `retrieval_score`, `retrieval_score_kind`, and `relevance_basis`. Legacy `relevance` is retained and explicitly labeled unvalidated.

### `flag_memory`
Concept id: `mcp.tool.flag_memory`

Flag a memory node for permanent retention. Flagged nodes survive lifecycle decay; startup injection is separately controlled by `bootstrap_scope`.

> **USER-ONLY. Agents must never flag on their own initiative.** Flagging is a
> permanent-retention decision reserved for the user. Do not call `flag_memory`, and do not
> pass `flagged=true` to `add_memory`, unless the user explicitly asked for it in the current
> session. "This seems important" is not approval. If something looks flag-worthy, store it
> unflagged and *say so* — the user can flag it. Note that the flag also propagates: a flagged
> episode calls `propagate_user_flag` over every entity extracted from it, and entity resolution
> dedupes those onto long-lived hub entities, so one unsanctioned `flagged=true` write can pin
> pre-existing shared nodes that nobody chose to pin.

- **`node_uuid`** (str): The UUID of the memory node to flag.
- **`bootstrap_scope`** (str, optional): `general`, `workspace:<key>`, or `none`. Omit to preserve the existing selector; `none` clears startup injection but keeps retention.
- Use for: long-term preferences, critical operator instructions, architectural decisions that should always be present at session start, and anything the agent should never forget regardless of how old it is — **when the user asks**.
- Do not make every pin `general`. Use workspace pins for project-specific startup guidance and null/`none` for retention-only facts.

### `unflag_memory`
Concept id: `mcp.tool.unflag_memory`

Remove the permanent-retention flag from a memory node. The node becomes subject to
normal lifecycle decay again unless re-flagged.
- **`node_uuid`** (str): The UUID of the memory node to unflag.

### `promote_memory`
Concept id: `mcp.tool.promote_memory`
Required tier: `operator`

Promote a PERSISTENT memory to PROMOTED: operator-curated, verified ground truth (SSOT-08).
Distinct from `flag_memory` (marks a memory as important to the user, but still an
ordinary claim) — PROMOTED means "this is verified and cannot be false." A promoted node
is immune to being merged into/out of another identity during correlation, its confidence
is pinned at 1.0, and conflicting future claims route to manual review instead of an
ordinary symmetric conflict.
- **`node_uuid`** (str): UUID of the memory node to promote. Must currently be PERSISTENT
  scope (SESSION/CANDIDATE have not earned durability yet). Get this from `recall_memories`
  results.
- Idempotent: promoting an already-promoted memory succeeds.

### `delete_memory`
Concept id: `mcp.tool.delete_memory`

Delete a specific memory node and all its relationships.
- **`node_uuid`** (str): The UUID of the memory node to delete.

### `delete_namespace`
Concept id: `mcp.tool.delete_namespace`
Required tier: `operator`

Tear down a throwaway/eval namespace silo. Refuses the default/shared namespace.
- **`namespace`** (str): The namespace silo to delete.
- **`max_nodes`** (int, optional): Safety gate — refuses deletion if the namespace has
  more nodes than this (default: 200).
- **`force`** (bool, optional): Bypass the `max_nodes` gate (default: false).
- **`dry_run`** (bool, optional): Report the node count and would-delete decision
  without deleting anything (default: false).
- This is a blast-radius guard, not a backup — deletion is still irreversible once it
  proceeds. Take a real backup first for anything you can't afford to lose.

### `close_memory`
Concept id: `mcp.tool.close_memory`
Required tier: `operator`

Mark a TEMPORAL memory as completed. Stops surfacing it in hook output; once completed,
the memory is suppressed from hook reminders and will be compressed by lifecycle shortly
after its `target_date` passes.
- **`uuid`** (str): UUID of the TEMPORAL memory node to complete.

### `recover_orphans`
Concept id: `mcp.tool.recover_orphans`

Recover orphaned SESSION nodes from crashed or abandoned sessions.
- **`max_age_hours`** (float, optional): Only process SESSION nodes older than this (default: 4.0).
- **`dry_run`** (bool, optional): Report counts without making changes (default: false).
- Promotes or deletes stale SESSION nodes that were never consolidated.

### `add_todo`
Concept id: `mcp.tool.add_todo`

Create a persistent TODO item that survives across sessions. TODOs are stored as `:Todo` nodes in Neo4j and are never enriched or decayed.
- **`text`** (str): The TODO description. Be specific and actionable.
- **`code_ref`** (str, optional): File path and optional line, e.g. `src/api/routes.py:42`.
- **`priority`** (str, optional): `low`, `normal`, or `high` (default: `normal`).
- **`episode_uuid`** (str, optional): UUID of an episodic memory that triggered this TODO. Creates a `CREATED_FROM` edge.
- **`structure_project`** (str, optional): Project name for scoped `REFERENCES_FILE` linking. Required in multi-repo workspaces to avoid cross-project path ambiguity.
- **`namespace`** (str, optional): Silo to scope the TODO to. Empty means the shared `default` silo — a stored TODO always carries a non-null namespace.
- Response includes `uuid`, priority tag, and any auto-linked graph nodes (`linked_file_path`, `linked_entities`, provenance episode).
- See `model.todo` in `data_models.md` for the full node schema and edge semantics.

### `list_todos`
Concept id: `mcp.tool.list_todos`

List TODO items filtered by status.
- **`status`** (str, optional): `open` (default) or `closed`.
- **`limit`** (int, optional): Max results (default: 25, cap: 200).
- **`namespace`** (str, optional): Silo to scope to. Empty lists every silo (historical behavior). Supplying one narrows to that silo plus the shared `default` bucket.
- Returns a formatted table with uuid, priority, code_ref, content snippet, and creation/close dates.
- Content is truncated at 100 chars; when any row is truncated the output points at `get_todo`.

### `get_todo`
Concept id: `mcp.tool.get_todo`

Read one TODO in full — the content `list_todos` truncates, plus its graph context.
- **`uuid`** (str): UUID of the TODO.
- **`namespace`** (str, optional): Silo to enforce. Empty looks up by uuid alone; supplying one refuses a TODO outside that silo and the shared `default` bucket. The namespace is reported either way.
- Returns priority, status, dates, age/stale flag, `code_ref`, the linked file
  (`REFERENCES_FILE`), the originating episode (`CREATED_FROM`), the entities named in the
  content, the normalized `:TodoLocation` records, inbound semantic links
  (`MENTIONS_TODO`/`ADDRESSES_TODO`/`RESOLVES_TODO`/`REOPENS_TODO`), and the untruncated body.
- Returns "TODO &lt;uuid&gt; not found" when no such node exists.

### `get_artifact`
Concept id: `mcp.tool.get_artifact`

Read one work artifact — a plan, review, investigation, implementation report or handoff.
- **`artifact_uuid`** (str): The artifact's stable uuid.
- **`namespace`** (str, optional): Silo to enforce. Empty looks up by uuid alone.
- Returns type, title, status, namespace, dates, embodiment locators (`EMBODIED_IN`), the
  code locations the document discusses (`HAS_LOCATION`), and the shape verdict.
- When the document's `Status:` header could not be mapped, reports the raw header and the
  reason, so an artifact holding its type's initial status because nobody could read its
  header stays distinguishable from one that genuinely is in that state.

### `list_artifacts`
Concept id: `mcp.tool.list_artifacts`

List work artifacts, most recently updated first.
- **`artifact_type`** (str, optional): `plan` | `review` | `investigation` |
  `implementation_report` | `handoff`. Empty lists every type.
- **`status`** (str, optional): Lifecycle status filter, e.g. `APPROVED`.
- **`namespace`** (str, optional): Silo. Empty lists every silo; supplying one narrows to
  that silo plus the shared `default` bucket.
- **`limit`** (int, optional): Max rows (default 25, max 200).
- Notes how many rows hold an unmapped status, rather than presenting them as declared.

### `list_artifact_questions`
Concept id: `mcp.tool.list_artifact_questions`

List design questions recorded on artifacts, in the order their author wrote them.
- **`artifact_uuid`** (str, optional): Restrict to one artifact. Empty spans all.
- **`status`** (str, optional): `open` (default) | `answered` | `deferred`.
- **`namespace`** (str, optional): Silo, read off the owning artifact rather than a
  namespace copied onto the question.
- **`limit`** (int, optional): Max rows (default 25, max 200).
- Answers "what design questions remain?" and "which plans are blocked?" without reading
  markdown. Each question carries an addressable `question_uuid`.

### `get_artifact_relationships`
Concept id: `mcp.tool.get_artifact_relationships`

Show an artifact's declared relationships in both directions.
- **`artifact_uuid`** (str): The artifact's stable uuid.
- Returns outgoing and incoming `REVIEWS`/`IMPLEMENTS`/`INFORMS`/`SUPERSEDES`, plus
  `ABOUT` subjects and `REFERENCES_TODO` todos.
- Every edge was explicitly declared; menhir never infers artifact relationships from
  prose. An absent edge means nobody declared it, not that no connection exists — which is
  what the empty result says rather than implying the artifact is unconnected.

### `link_artifacts`
Concept id: `mcp.tool.link_artifacts`

Declare a relationship between two artifacts.
- **`source_uuid`** (str): The artifact making the claim.
- **`target_uuid`** (str): The artifact being claimed about.
- **`relation`** (str): `reviews` | `implements` | `informs`.
- Legality is decided against both artifacts' **stored** types, so a caller cannot assert
  its way past the constraint. Refusals return a reason rather than raising.
- `supersedes` is deliberately not accepted here — see `supersede_artifact`.

### `supersede_artifact`
Concept id: `mcp.tool.supersede_artifact`

Record that one artifact replaces another.
- **`new_uuid`** (str): The replacing artifact.
- **`old_uuid`** (str): The artifact being replaced.
- Writes the `SUPERSEDES` edge and moves the old artifact to `SUPERSEDED` in one statement:
  an edge pointing at an artifact still marked `APPROVED`, or a `SUPERSEDED` artifact with
  no record of what replaced it, are both states the graph must never hold.
- Same-type only. An already-terminal artifact is refused rather than re-superseded, so the
  recorded replacement stays the one that applied.

### `transition_artifact`
Concept id: `mcp.tool.transition_artifact`

Move an artifact to a new lifecycle status.
- **`artifact_uuid`** (str): The artifact to move.
- **`to_status`** (str): Target status.
- Checked against the artifact's stored type and current status, so steps cannot be
  skipped: a `PROPOSED` plan cannot jump to `IMPLEMENTED`.
- A refusal names the statuses that *are* reachable from the current one.

### `audit_artifact_corpus`
Concept id: `mcp.tool.audit_artifact_corpus`

Report whether a repository's work-artifact corpus matches the graph. Read-only.
- **`repo_path`** (str): Absolute path to the working tree to audit.
- **`repository`** (str, required): Repository name recorded on sources. It is never
  inferred from the worktree directory name.
- **`from_commit`** (str, optional): Override the persisted reconciliation cursor for this audit's
  Git evidence interval. It does not replace or advance the stored cursor.
- Returns the stored cursor, selected evidence base, parity counts, plan digest, and bounded lists
  of conflicts and lane/lifecycle contradictions. The full action ledger stays in CLI JSON output
  — a chat transport is the wrong place to send several hundred action records.
- `evidence_base_valid: false` means Git could not compare that commit with the checkout. Audit still
  writes nothing, but apply refuses until a valid `--from-commit` is supplied.
- Writes nothing. Applying anything requires the digest and an operator running
  `menhir artifacts reconcile --repository <name> --apply`.

### `relocate_artifact_source`
Concept id: `mcp.tool.relocate_artifact_source`

Move one artifact source's locator after a file moved. Agent tier.
- **`artifact_uuid`** (str): The artifact you believe currently lives at `old_path`.
- **`old_path`** (str): Repository-relative path recorded on the source today.
- **`new_path`** (str): Repository-relative path the document now lives at.
- **`repository`** (str, optional): Repository name recorded on the source.
- **`expected_old_integrity`** (str, optional): SHA-256 the caller believes is current.
  Supplying it makes the write refuse stale input.
- **`observed_integrity`** (str, optional): SHA-256 of the file at its new path.
- Identity survives: the artifact UUID, the source record, and every relationship are
  untouched; only the locator changes.
- Refused when the old path belongs to a different artifact, when it identifies more than
  one source, or when the destination is already claimed. Each refusal names which.
- **This is the escape hatch, not the routine path.** Ordinary moves are picked up by the
  file-event hook or by the next corpus audit. Use this for an audited ambiguity, or a move
  where Git evidence is unavailable.

### `close_todo`
Concept id: `mcp.tool.close_todo`

Mark a TODO as closed.
- **`uuid`** (str): UUID of the TODO to close.
- Returns confirmation or "not found or already closed".

### `supersede_todo`
Concept id: `mcp.tool.supersede_todo`

Close a TODO and record the TODO that replaced it, atomically. Menhir has no update path:
editing a todo means closing it and adding a replacement. Use this instead of `close_todo`
whenever the new todo IS the edited version of the old one, so the refile lineage survives
as a `SUPERSEDED_BY` edge rather than as prose in some memory.
- **`old_uuid`** (str): TODO being replaced. Must be open and have no successor already.
- **`new_uuid`** (str): Replacement TODO. Must be open, and in the old todo's namespace or
  the shared `default` bucket.
- Returns confirmation, or a specific reason: `old_todo_not_found`,
  `old_todo_already_superseded`, `old_todo_not_open`, `new_todo_ineligible`, or
  `cannot_supersede_itself`.
- Read the lineage back through `get_todo`, which prints `superseded by: <uuid>` and
  `supersedes: <uuid>` lines, scoped to your namespace.
- `superseded_by` is a LIST. More than one successor means concurrent supersessions
  raced; `get_todo` prints a warning and the lineage is ambiguous until one edge is
  removed. Note the namespace rule is the INVERSE of `supersede_artifact`'s: here the
  NEW todo must be in the OLD one's silo (or `default`), where for artifacts the OLD
  must be in the NEW one's.

### `resolve_todo`
Concept id: `mcp.tool.resolve_todo`

Close a TODO and record the memory that resolved it, atomically. Use instead of `close_todo`
when a stored memory is the evidence the work is done: `close_todo` moves status and writes
no edge, so a todo closed that way cannot answer "why did this close?".
- **`todo_uuid`** (str): TODO to close. Must be open.
- **`memory_uuid`** (str): Memory that resolved it. Must be a durable, non-structural memory
  in the todo's namespace or the shared `default` bucket.
- Returns confirmation, or the reason it was refused.

### `reopen_todo`
Concept id: `mcp.tool.reopen_todo`

Reopen a closed TODO and record the memory that reopened it. Clears `closed_at` and returns
any linked reminder to open.
- **`todo_uuid`** (str): TODO to reopen. Must be closed, and must NOT be superseded --
  reopening a superseded todo would leave an open node still pointing at its replacement.
  To revive superseded work, act on the successor instead.
- **`memory_uuid`** (str): Memory that reopened it, same eligibility as `resolve_todo`.
- Returns confirmation, or the reason it was refused.

### `link_memory_to_todo`
Concept id: `mcp.tool.link_memory_to_todo`

Point a stored memory at a TODO. Direction is always inward -- a memory references the todo,
never the reverse -- so knowledge stays in the memory and the todo stays operational.
- **`memory_uuid`** (str): The referencing memory. Must be durable and non-structural.
- **`todo_uuid`** (str): The TODO being referenced.
- **`relation`** (str): `mentions` or `addresses`. Neither moves status: closing is
  `resolve_todo`, which writes its own edge.
- Returns confirmation, or the reason it was refused.

### `close_stale_todos`
Concept id: `mcp.tool.close_stale_todos`

Close stale TODO items that have been open for too long.
- **`older_than_days`** (int, optional): Close todos open for more than this many days
  (default: 60, max: 365).
- **`dry_run`** (bool, optional): Preview only — don't actually close any todos
  (default: true).
- Returns a summary of closed or previewed stale todos, including up to 10 UUIDs.

### Choosing Between Recall Tools

| Situation | Tool |
|-----------|------|
| Session start — bootstrap pinned context | `read_flagged_memories` then `recall_context_memories` |
| Mid-task targeted search for specific knowledge | `recall_memories` |
| Building a token-budgeted context block for injection | `build_context` |
| Knowing how long since last memory interaction | `get_client_context` |

`recall_context_memories` is a two-phase bootstrap tool — it requires a prior `read_flagged_memories` call for the same `reader_id` and returns both `relevant[]` and `recent[]` arrays. Use it at session start to hydrate context efficiently.

`recall_memories` is a standalone semantic search — no bootstrap dependency, returns ranked results with score breakdowns. Use it mid-task when you need targeted retrieval for a specific question.

### `build_context`
Concept id: `mcp.tool.build_context`

Build a token-budget-limited context string from recalled memories.
- **`query`** (str): Natural language query to recall relevant memories.
- **`max_tokens`** (int, optional): Token budget for the returned context (default: 2000).
- **`preset`** (str, optional): Recall preset — `knowledge` (default), `recent`, `session`.
- **`session_id`** (str, optional): Optional session ID for session-scoped recall.
- **`include_scores`** (bool, optional): Include relevance scores in output (default: false).
- Uses tiktoken when available, falls back to heuristic estimation with conservative budget fraction.
- Returns assembled context, token estimate, estimation mode, memory count, and truncation status.
- TODO injection: open TODOs whose content keyword-matches the query are pre-fetched before the memory packing loop. Their token cost is pre-reserved from `max_tokens` so the total payload stays within budget. Matched TODOs are appended as a `### TODOs (N matching)` section at the end of the context string.

### `get_client_context`
Concept id: `mcp.tool.get_client_context`

Return caller identity and elapsed time since last memory access.
- No parameters.
- Returns `client_id`, `client_name`, `session_id`, `session_started_at`, `last_accessed`, and `elapsed_since_last_access` (formatted as `Nm`, `N.Nh`, or `Nd`).
- Shows `(first session)` when no prior access exists for this session.
- Shows `_No caller session bound_` in local/unauthenticated mode.
- Use at session start to orient temporal context without a full recall query.

### `query_structure`
Concept id: `mcp.tool.query_structure`

Query the structural code graph for project layout, files, imports, tests, and endpoints.
- **`query_type`** (str): One of `projects`, `overview`, `files`, `imports`, `tests`, `endpoints`, `dependencies`, `cross_refs`, `blast_radius`, `affected_tests`, `symbols`, `context`.
- **`project`** (str, optional): Project name — required for all query types except `projects`.
- **`path`** (str, optional): File path filter for `files`, `imports`, `tests`, `blast_radius`, `affected_tests`, `symbols`, and `context` queries. For `blast_radius` and `affected_tests`, accepts comma-separated paths.
- Faster and more context-efficient than globbing/grepping for structural questions.
- `blast_radius`: traces direct importers, transitive importers, affected tests, and cross-project refs for changed files.
- `affected_tests`: returns the minimal test set for changed files with a ready-to-run pytest command.
- `symbols`: list all classes/functions/methods in a file (exact path), directory prefix (trailing `/`), or whole project.
- `context`: combined view — file summary + symbols + import graph for one file. Replaces 3 separate queries.
- `files` output includes `[hot:N]` tag when a file has been modified N times since last full scan.
- Use `ingest_project` to scan a project directory into the graph; a background watcher auto-refreshes every 30 minutes.

### `rate_recall`
Concept id: `mcp.tool.rate_recall`
Required tier: `agent`

Report how useful a prior recall/context result was — an operational quality signal for
the usage dashboard. Self-reported (`agent_inference` grade); never affects memory heat,
promotion, or ranking, so rate honestly. Call shortly after acting on a
`recall_memories`/`recall_context_memories`/`read_flagged_memories`/`build_context` result.
- **`score`** (str): One of `useful` (you cited/reused specific content), `partial`
  (confirmed/narrowed direction but needed other sources), `noise` (irrelevant/stale/wrong),
  or `unused` (not consulted).
- **`recall_id`** (str, optional): The `recall_id` token from the recall response. If
  omitted, rates the most recent unrated recall in this session.
- **`reason`** (str, optional): Short note on why.
- Returns confirmation of the rated recall, or an error if no matching recall exists.

### `get_memory_stats`
Concept id: `mcp.tool.get_memory_stats`

Return operational health summary for the memory system.
- **`since_hours`** (int, optional): Lookback window in hours (default: 24).
- Returns 6 sections: ingestion rate, recall latency (p50/p95), failure summary, enrichment queue depth, circuit breaker state, and lifecycle summary.

### `view_entropy`
Concept id: `mcp.tool.view_entropy`

D0 view-reachability probe: for each current View, the rank + token footprint at which
recall returns the View's own canonical surface. Deterministic, label-free, LLM-free;
reads without bumping access stats (`update_access=False`). Reachability only — it does
not validate that the View's value is correct or current (that is the belief-gate's axis).
- **`namespace`** (str, optional): Silo to probe. Empty = the default/shared namespace set.
- **`kind`** (str, optional): View kind filter (`counter`, `timeline`). Empty = all kinds.
- **`top_k`** (int, optional): Search depth per View before declaring it unreached (default: 20).
- **`max_views`** (int, optional): Max Views probed per call (default: 50).
- Returns `summary` (views_probed, reached/unreached, median rank, median tokens-to-view) plus per-View rows.
- Healthy = rank 1 / ~one surface of tokens; a rising rank or `reached=false` means the recall path is burying current state.

### `mint_client`
Concept id: `mcp.tool.mint_client`
Required tier: `operator`

Mint a new per-client token; returns the raw token ONCE. Requires the per-client token
tier to be enabled (`MENHIR_CLIENT_TOKENS_ENABLED=1`).
- **`client_name`** (str): Human-readable name for the client.
- **`tier`** (str, optional): `operator`, `agent`, or `readonly` (default: `readonly`).
- Returns `client_id`, `client_name`, `tier`, and the raw `token` (shown only this once).

### `list_clients`
Concept id: `mcp.tool.list_clients`
Required tier: `operator`

List registered (non-revoked) per-client tokens (no token material). Requires the
per-client token tier to be enabled.
- No parameters.
- Returns `clients[]` with `client_id`, `client_name`, `tier`, and `created_at` per entry.

### `revoke_client`
Concept id: `mcp.tool.revoke_client`
Required tier: `operator`

Revoke a client token by `client_id`. Requires the per-client token tier to be enabled.
- **`client_id`** (str): The client identifier to revoke.
- Returns `client_id` and `revoked` (bool).

## Resources

### `memory://system/dependency-health`
Concept id: `mcp.resource.system.dependency_health`

Lightweight dependency checks that do not require full memory runtime init.
- Currently reports Neo4j socket reachability from `NEO4J_URI`.
- Use this first when MCP memory tools fail early and you suspect the remote Neo4j service is down.

### `memory://system/metadata`
Concept id: `mcp.resource.system.metadata`

Server runtime metadata and high-level graph counts, including compact scheduler and queue metrics.

### `memory://recent`
Concept id: `mcp.resource.memory.recent`

Most recently accessed or created memory nodes (limited to 10) using the compact memory shape.

The local, not-yet-deployed implementation uses the same fail-closed View policy as bootstrap
recall: a current, non-retired View requires complete live provenance. Explicit UUID inspection
remains available for operator/audit use. Coordinated schema activation, reconciliation, and writer
deployment are still required before this describes production behavior.

### `memory://system/lifecycle-trace`
Concept id: `mcp.resource.system.lifecycle_trace`

Recent lifecycle/debug events captured by the MCP SQLite sidecar.
- Returns compact JSON with recent `component`, `event`, `state`, optional `episode_uuid`, and decoded `details`.
- Intended for MCP boot, runtime-init, queue, and Graphiti request-boundary debugging.

### `memory://system/processing-queue`
Concept id: `mcp.resource.system.processing_queue`

Compact processing-queue snapshot for lockup debugging.
- Returns compact JSON with:
  - `count`
  - `queue_depth`
  - `max_attempts`
  - `counts.pending`
  - `counts.enriching`
  - `counts.stale_enriching`
  - `counts.exhausted_pending`
  - `items[]` rows with `state`, `stage`, `substage`, `attempts`, `owner`, `heartbeat_at`, `llm_last_task_at`, plus derived `stale_lease`, `exhausted_pending`, and `status_hint`.
- Intended for quick operator checks when `list_enrichment_queue` is too slow or you need a machine-readable view of stale/exhausted rows.

### `memory://node/{node_uuid}` (Template)
Concept id: `mcp.resource.memory.node`

Resolve a single memory node by UUID using the detailed memory shape.

### `memory://scope/{scope}` (Template)
Concept id: `mcp.resource.memory.scope`

List memories filtered by scope (`SESSION`, `PERSISTENT`, `PROMOTED`) using the compact memory shape.

### `memory://search/{term}` (Template)
Concept id: `mcp.resource.memory.search`

Recall memories for a natural-language search term using the knowledge preset and compact scored result entries.

### `memory://type/{memory_type}` (Template)
Concept id: `mcp.resource.memory.type`

List entity memories filtered by type using the compact memory shape.

## Hook Integration

The memory system integrates with Claude Code's hook system via the `menhir hook run` CLI command. Hooks are configured in `~/.claude/settings.json`.

### Active Hooks

| Event | Command | Behavior |
|-------|---------|----------|
| `UserPromptSubmit` | `hook run --frequency 10` | Recalls memories every 10 turns; emits write nudges on correction signals every turn |
| `Stop` | `hook run --event stop --frequency 10` | Emits a memory save checkpoint reminder every 10 turns at session end |
| `PostCompact` | `hook run --event postcompact` | Always runs after context compaction; uses `compact_summary` from stdin as the recall query to inject relevant memories into the fresh context window |

### PostCompact Behavior

When Claude Code compacts the conversation context, the `PostCompact` hook fires with the generated `compact_summary` in stdin. The hook:
1. Parses `compact_summary` as the recall query (falls back to `"recent session context and active work"` if too short)
2. Fetches flagged memories and open TODOs (fast path, no HTTP server needed)
3. Runs a full context recall against the memory graph using the summary as the query
4. Injects the result into the fresh context window

No frequency gate — this hook always runs regardless of turn count.

### Hook Output Format

Every hook injection (UserPromptSubmit and PostCompact) produces a structured block containing:
1. **Temporal line** — current UTC time and elapsed since last session access
2. **`### TODOs (N open)`** — up to 5 open TODOs, sorted by priority then age. Always present when any TODOs exist, regardless of prompt content
3. **`### Pinned`** — flagged memories (always injected)
4. **`### Context (query="...")`** — semantically relevant memories for the current prompt (only when prompt is long enough and services are available)
5. **Memory Write Reminder** — emitted when the prompt contains correction or confirmation signals

Open TODOs are injected automatically on every turn where recall fires — no query needed. This makes them visible to the agent without explicit `list_todos` calls.
