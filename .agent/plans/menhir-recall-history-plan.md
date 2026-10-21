---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- recall_history drill-down tool

Project Scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-recall-history`,
branch `feat/recall-history`, base `origin/main` `5b73a1a5` (source-memory lane, PR #206). Nothing
outside this worktree.

## Why
Normal recall now carries a small `source_memories` section (the most similar saved memories, with
times). For "how did X change / what was the sequence / what was it before" questions an agent needs
the drill-down: more matching memories, strictly in time order, optionally grouped into pools. This
tool is that drill-down. It reuses the lane's read query; it never writes.

## Behavior (contract)

1. **Service** `RecallService.recall_history(query: str, *, namespace: str | None = None,
   limit: int = 30, pools: bool = True) -> RecallHistoryResult` in `services/recall_service.py`
   (implementation may live in `services/recall_pipeline.py` next to `_build_source_memories`).
   - `limit` clamped to 1..50 (the read query's clamp). Empty/whitespace query -> ValueError.
   - Embed the query with `service.graphiti_client.embed_query`, then
     `graph_adapter.search_episode_embeddings(query_vector, limit=limit, namespace=namespace)`
     via `asyncio.to_thread` (same as the lane).
   - Build `SourceMemory` entries exactly like `_build_source_memories` does (whitespace collapse,
     no per-entry char cap here: return full content up to 4,000 chars with a trailing `…` if cut),
     then sort **oldest first** by `reference_time`, then uuid.
   - `pools=True` -> `assign_pools` from `domain/source_memory_pools.py` (same as the lane).
   - Works whether or not `MENHIR_FRONTIER_SOURCE_MEMORIES` is on (it only reads embeddings).
   - `RecallHistoryResult` (frozen dataclass in `domain/recall.py`):
     `query: str, memories: tuple[SourceMemory, ...], note: str | None`.
     `note` is set (and `memories` empty) when nothing matched:
     `"No embedded memories matched. Memories are embedded when MENHIR_FRONTIER_SOURCE_MEMORIES is on; run scripts/backfill_episode_embeddings.py for older memories."`
   - On embed/search failure: raise (the tool/route layers already convert errors), do not return
     partial results silently.
   - Read-only: no `last_accessed` touches, no access updates.
2. **Backend plumbing**, mirroring `recall` exactly: `core/backend_protocol.py` (`recall_history`),
   `core/backend_runtime_data_ops.py` (calls the service, returns a dict via `asdict`),
   `core/backend_client_ops.py` (remote: POST to the new REST route).
3. **REST** `POST /api/recall/history` in `api/routes.py` with `RecallHistoryRequest`
   (`query: str`, `namespace: str | None`, `limit: int = Field(30, ge=1, le=50)`, `pools: bool = True`)
   and `RecallHistoryResponse` (`query`, `memories: list[SourceMemoryResponse]`, `note: str | None`,
   `response_model_exclude_none=True`) in `api/routes_support.py`. Namespace resolution identical to
   `/api/recall` (`_resolve_namespace`). Same auth/tier as `/api/recall` (read).
4. **MCP tool** `recall_history` in a new file `mcp/tools/recall/recall_history.py`, modelled on
   `recall_memories.py` (`BaseJsonTool`, `scope = ToolScope.NAMESPACED`, `required_tier = "readonly"`,
   `oauth_scopes = ("menhir:read",)`, read-only/non-destructive hints). Args: `query`, `namespace=""`,
   `limit=30`, `pools=True`. Docstring/description: "List saved memories matching a subject in time
   order (oldest first), with optional pools. Use after recall_memories when a question needs how
   something changed, the sequence of states, or what it was before." Output JSON:
   `{"query", "count", "memories": [{time, content, uuid, source, pool_id?, pool_anchor?}], "note"?}`.
   Register in `RECALL_TOOLS` (`mcp/tools/recall/__init__.py`). Do NOT add it to `always_visible` in
   `mcp/server.py`.
5. **Registries** (add `recall_history` next to `recall_memories` wherever recall tools are listed):
   `access_contract.py` `AGENT_ALLOWED_TOOLS`; `explorer/feature_taxonomy.py` `PARENTS["retrieve"]`;
   `mcp/feedback.py` recall-tool set; `mcp/tools/ops/get_memory_stats.py` tool list;
   `.agent/concept-ids.yaml` (`mcp.tool.recall_history`, owner `endpoints.md`, meaning
   "Time-ordered matching memories (drill-down)"); `.agent/endpoints.md` and `.agent/tasks-mcp.md`
   entries; and any other src registry that a failing test names. If a test fixture (e.g.
   `tests/fixtures/client-policy.synthetic.json`) must list the tool for a policy test to pass,
   add it there too and say so in the report; do NOT change any test's assertions.
6. **Docs**: `.agent/CHANGELOG.md` dated entry; `docs/agent-usage.md` one line on when to use it.

## Tests (new file `tests/test_recall_history.py`)
- Service: time ordering (oldest first) regardless of cosine order; limit clamp; pools on/off;
  empty result -> note; empty query -> ValueError; failure propagates; no access-update calls.
- REST: request bounds, response shape, namespace resolution, omitted note when results present.
- MCP: tool payload shape; registered in RECALL_TOOLS; not in always_visible.
- Registries: `recall_history` present in AGENT_ALLOWED_TOOLS and feature taxonomy.

## Test policy
Executor: focused tests only: `uv run pytest -q tests/test_recall_history.py
tests/test_source_memory_lane.py tests/test_client_tool_allowlist.py tests/test_feature_taxonomy.py
tests/test_mcp_gateway.py tests/test_backend_mcp_boundaries.py tests/test_api_auth.py
tests/test_feature_report.py tests/test_mcp_agent_guidance.py`, plus `uv run ruff check` on changed
files; at most once more after a fix. No full suite (reviewer runs it once).

## Non-goals
No change to the source-memory lane or recall ranking; no writes; no new settings; no Graphiti change.
