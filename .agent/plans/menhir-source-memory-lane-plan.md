---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- source-memory recall lane (step 1)

Project Scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-source-memory-lane`,
branch `feat/source-memory-lane`, base `origin/main` `f034bfc5`. Nothing outside this worktree.

## Why
AMA-Bench supersession A/B (205 questions, 2 repeats): adding the session's top-15 source memories by
embedding similarity, each with its time, NEXT TO the normal recall items fixed 41 and broke 19
answers vs production (sign test p=0.006); "latest X" stale answers in TEXT2SQL fell 9 -> 3.
Grouping the same memories into pools was neutral (43/20). Recall today returns entity summaries
and never the memories themselves. Design notes: `.agent/plans/menhir-general-state-timelines-2026-09-29.md`
section 4.1c (owner's copy; not required to implement this).

## Behavior (contract)

1. **Flag, default OFF.** With every new setting at its default, recall output, ingest and every
   existing test must be byte-for-byte unchanged, and no embedding call is made for this feature.
2. **Settings** (`src/menhir/config/settings_model.py`, mirror `frontier_content_vector*` at lines
   ~400, ~712 and ~906 exactly, including env parsing and validation style):
   - `frontier_source_memories: bool = False` <- `MENHIR_FRONTIER_SOURCE_MEMORIES`
   - `frontier_source_memory_limit: int = 10` <- `MENHIR_FRONTIER_SOURCE_MEMORY_LIMIT` (1..50)
   - `frontier_source_memory_max_chars: int = 600` <- `MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS` (100..4000)
   - `frontier_source_memory_pools: bool = False` <- `MENHIR_FRONTIER_SOURCE_MEMORY_POOLS`
   Mapped into `RetrievalTuningConfig` (`src/menhir/domain/retrieval_tuning.py`) as
   `enable_source_memories`, `source_memory_limit`, `source_memory_max_chars`, `source_memory_pools`,
   with `__post_init__` range validation, and into `RecallLabTuning` (`src/menhir/explorer/recall_lab.py`).
3. **Per-call override** `source_memory_limit: int | None = None` on `RecallService.recall`
   (`services/recall_service.py:110`), on `RecallRequest` (`api/routes_support.py:179`,
   `Field(default=None, ge=0, le=50)`) and on the MCP tool `recall_memories`
   (`mcp/tools/recall/recall_memories.py`, documented in its docstring). Effective limit:
   `None` -> `tuning.source_memory_limit` if `tuning.enable_source_memories` else 0; `0` -> off for this
   call; `>0` -> that limit (explicit request enables it for the call even if the flag is off).
4. **Write: episode content embedding.** New `EpisodeLifecycleRepository`-side (or memory-queries-side,
   follow where similar `SET` writes on `:Episodic` live, e.g. `infrastructure/episode_lifecycle.py`)
   methods, exposed through `infrastructure/memory_graph_adapter.py`:
   - `episode_has_content_embedding(episode_uuid) -> bool`
   - `set_episode_content_embedding(episode_uuid, embedding, model)`:
     `MATCH (n:Episodic {uuid:$uuid}) WHERE NOT coalesce(n.is_evidence_projection,false)
      SET n.content_embedding=$embedding, n.content_embedding_model=$model, n.content_embedding_at=datetime()`
   The node is Menhir's own `:Episodic` (the one created by `create_pending_episode`-style `MERGE` at
   `episode_lifecycle.py:~125`, uuid = `ctx.episode_uuid` / `claimed["uuid"]`), NOT Graphiti's
   resolved episode node.
5. **Ingest step.** New `async def embed_episode_content(ctx) -> None` in
   `services/enrichment_steps.py`, called in `services/ingest_worker.py` immediately before
   `await run_graphiti_extraction(ctx, finalize_under_gate=True)` (line ~233). It runs only when the
   deployment setting `frontier_source_memories` is on; skips evidence projections
   (`claimed.get("is_evidence_projection")` or equivalent claim field; the `SET` guard is the
   backstop) and episodes that already have an embedding; embeds `claimed["content"]` truncated to
   8,000 chars via the Graphiti embedder (`graphiti_client.embed_query`, the same embedder recall
   uses); records the embedder model name if resolvable, else `"unknown"`. **Any exception is
   logged at WARNING and swallowed: it must never fail, delay-retry, or requeue enrichment.**
6. **Backfill script** `scripts/backfill_episode_embeddings.py`: `--namespace`, `--batch 100`,
   `--limit`, `--dry-run`; pages through `list_episodes_missing_content_embedding(namespace, limit)`
   (same visibility predicates as item 7) and writes embeddings with the same helper. Idempotent
   (re-running writes nothing new). Model the bootstrap/CLI on an existing script such as
   `scripts/backfill_admitted_on.py`. Add it to `.agent/scripts-index.md`.
7. **Read query** `search_episode_embeddings(query_vector, *, limit, namespace)` in
   `infrastructure/memory_queries.py` next to `search_content_embeddings` (line ~348) plus adapter
   passthrough (`memory_graph_adapter.py:~848`). Read-only, `limit` clamped 1..50:
   ```
   MATCH (n:Episodic)
   WHERE n.content_embedding IS NOT NULL
     AND <non_structural_memory_cypher("n")>        -- domain/structural_memory.py
     AND <default_recall_visibility_cypher("n")>     -- domain/recall_visibility.py
     AND <namespace predicate>                       -- same helper/spelling rule fetch_recent_memories uses
   WITH n, vector.similarity.cosine(n.content_embedding, $query_vector) AS cosine
   WHERE cosine IS NOT NULL
   RETURN n.uuid AS uuid, n.content AS content, n.source AS source, n.session_id AS session_id,
          toString(coalesce(n.reference_time, n.created_at)) AS reference_time, cosine
   ORDER BY cosine DESC, n.uuid LIMIT $limit
   ```
   Use the shared predicate functions; do not respell them. Namespace `''` and `'default'` are the
   same silo (see `domain/namespace.py` `namespace_spellings`).
8. **Recall section.** In `services/recall_pipeline.py`, after final ranking (next to the POST-RANK
   temporal-facts enrichment), when effective limit > 0: embed the query once (reuse the vector if the
   content-vector lane already computed one), call the search, and build
   `source_memories: tuple[SourceMemory, ...]` ordered **oldest first** by `reference_time`, then uuid.
   `SourceMemory` is a new frozen dataclass in `domain/recall.py`:
   `uuid, content, reference_time, cosine, source, pool_id: str | None, pool_anchor: str | None`.
   `content` is whitespace-collapsed and cut to `source_memory_max_chars` with a trailing `…` when cut.
   **The ranked `results` list, `candidates_evaluated` and every other field must be identical with
   the lane on or off** (it is additive, never fused). On any lane failure: log ERROR, set
   `source_memories=None`, and append the note `"Source-memory lane unavailable"` (same note-joining
   style as `search_error`). Include the lane in the trace when `trace=True` if a natural slot exists
   (limit, hits, elapsed ms); otherwise skip.
   `RecallResult` gets `source_memories: tuple[SourceMemory, ...] | None = None`. Propagate it to
   every place `event_authority_layer` is propagated (grep it: `api/routes.py`,
   `api/routes_support.py` response model `SourceMemoryResponse`, `mcp/tools/recall/recall_memories.py`
   payload key `source_memories` kept in compact mode, `core/backend_runtime_data_ops.py`,
   `services/context_builder.py`, `explorer/*` where they serialize recall results). Omitted from the
   wire when None.
9. **Pools (optional, `source_memory_pools`).** New pure module `domain/source_memory_pools.py`:
   `extract_anchors(text) -> set[str]` and `assign_pools(memories) -> list[(memory, pool_id|None, anchor|None)]`.
   Anchors, after removing step labels `\b(step|turn|attempt)\s*\d+\b` (case-insensitive):
   file paths `[\w./-]+\.(py|js|ts|csv|sql|json|md|txt|html|yaml|yml|cfg|toml)\b`, URLs, backtick- or
   double-quoted strings of 2..60 chars, identifiers containing `_` or `.` (`\b[A-Za-z_]\w*[._]\w+\b`),
   `test_\w+`, `key=number`, and lowercase noun + number (`\b([a-z]{2,})\s(\d{1,4})\b`, e.g. `cd 3`).
   Lowercase all anchors. Greedy grouping over the RETURNED memories only: repeatedly take the anchor
   shared by the most remaining memories (ties: lexicographic), if shared by >= 2 form a pool of
   those memories and remove them; stop when no anchor is shared by 2. Each memory is in at most one
   pool. `pool_id = "pool:" + sha256("v1|" + "|".join(sorted(member uuids)))[:16]`. Pure and
   deterministic. When pools are off, `pool_id`/`pool_anchor` are None.
10. **Recall Lab arms** in `DEFAULT_ARMS`: `sm` "SM · production + source memories"
    (`enable_source_memories=True`) and `smp` "SMP · source memories grouped into pools"
    (`enable_source_memories=True, source_memory_pools=True`). Check how the Lab serializes and
    displays arm results; make the section visible in its JSON output. No JS/UI work.

## Tests (new files only; do NOT edit existing test assertions)
Adding stub methods to the shared test doubles in `tests/conftest.py` (mirroring
`search_content_embeddings` / `content_embedding_calls` at lines ~322 and ~462) is allowed.
`tests/test_source_memory_lane.py` (split if large) must cover:
- query text contains the structural, visibility and namespace predicates, is read-only, clamps limit
  (mirror `tests/test_content_vector_retrieval.py`);
- flag off: `source_memories is None`, zero embed/search calls, results identical;
- flag on: section present, oldest-first, char cap with `…`, results list identical to flag off;
- per-call `0` disables with flag on; per-call `3` enables with flag off and returns 3;
- lane exception: recall still returns ranked results, note contains `Source-memory lane unavailable`;
- pools: deterministic ids across input order, each memory in at most one pool, step labels ignored,
  no pool for anchors shared by one memory;
- ingest step: skipped when setting off; skipped when embedding exists; skipped for projections;
  embedder exception is swallowed and enrichment continues;
- settings env parsing and range validation for the four settings;
- API `RecallRequest.source_memory_limit` bounds, response field omitted when None; MCP payload key.

## Docs
`.agent/CHANGELOG.md` (dated entry), `.agent/default-off-features.md` (new entry: what it does, flags,
evidence numbers from "Why", backfill script), `.env.example` (the four env vars, commented, off),
`.agent/scripts-index.md` (backfill script).

## Non-goals
No change to Graphiti or the fork; no fusion into ranked results; no vector index creation; no
`recall_history` tool; no default flip; no Bench changes; no refactors of surrounding code; no
dependency changes.

## Test policy
Executor: focused tests only (`uv run pytest -q tests/test_source_memory_lane*.py
tests/test_content_vector_retrieval.py tests/test_recall_service.py tests/test_settings*.py` and
`uv run ruff check` on changed files), at most once more after a fix. No full suite.
Reviewer runs the full offline suite once.

## Deferred
- One-node episodes: let the fork's `add_episode` accept a caller uuid so Menhir's queue node and
  Graphiti's episode node are the same node (fork change, separate PR).
- `recall_history` drill-down tool; non-state corpus run to decide the default; vector index if
  brute-force cosine per namespace gets slow.
