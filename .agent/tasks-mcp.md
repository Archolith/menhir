# Tasks: MCP

Use this file when the work is about tool selection, resource usage, bootstrap reads, or operator actions.

## Common Tasks

### Pick the right tool
- read first:
  - `mcp.group.ingest`
  - `mcp.group.processing`
  - `mcp.group.recall`
  - `mcp.group.operator`
- then open:
  - [endpoints.md](endpoints.md)
  - [mcp-tools.yaml](mcp-tools.yaml)

### Query project structure safely
- start with `query_structure("projects")`
- if the target repo is not listed, run `ingest_project(path="<absolute repo path>", name="<project-name>")` before using `overview`, `files`, `tests`, or `blast_radius`
- do not assume `No files found ...` or `No test mappings found ...` means the repo is empty until you have confirmed the project is ingested
- the structure watcher only refreshes already-ingested repos; it does not discover new repos automatically

### Bootstrap context for an agent
- read first:
  - `mcp.tool.read_flagged_memories`
  - `mcp.tool.recall_context_memories`
  - `mcp.tool.recall_memories`
- Codex lean path:
  - use `memory_gateway(action="bootstrap_context", payload_json=...)` to perform the two-phase bootstrap without registering the full recall tool set in prompt context
- then open:
  - [endpoints.md](endpoints.md)
- register and pass one stable explicit workspace key at project/session start; do not infer it from the current working directory
- call `read_flagged_memories(reader_id=..., workspace=...)` and then `recall_context_memories` with the same reader/workspace; receipts do not cross workspace selections

### Choose between recall tools
- **Session start bootstrap** → `read_flagged_memories` then `recall_context_memories` (two-phase, returns flagged + relevant + recent)
- **Codex token-light bootstrap** → `memory_gateway(action="bootstrap_context", payload_json=...)` on the Codex gateway server
- **Mid-task targeted search** → `recall_memories` (standalone semantic search, no bootstrap dependency)
- **Token-budgeted context block** → `build_context` (packs memories + TODOs within a token limit)
- **How long since last access** → `get_client_context` (no recall, just identity + elapsed time)

### Codex gateway payloads

When Codex only exposes the lean `memory_gateway`, call:

```text
memory_gateway(action="help", payload_json="{}")
memory_gateway(action="recall", payload_json="{\"query\":\"auth decisions\",\"preset\":\"knowledge\",\"limit\":5}")
memory_gateway(action="search", payload_json="{\"query\":\"auth decisions\",\"preset\":\"knowledge\",\"limit\":5}")  # alias for recall
memory_gateway(action="query_structure", payload_json="{\"query_type\":\"overview\",\"project\":\"menhir\"}")
memory_gateway(action="add_memory", payload_json="{\"text\":\"Stable fact\",\"source\":\"session note\",\"diff\":\"\"}")
# Aliases accepted: text (canonical), content, summary — precedence: text > content > summary
```

The gateway currently exposes `bootstrap_context`, `read_flagged`, `recall_context`, `recall`, `query_structure`, `build_context`, `add_memory`, `get_client_context`, `flag_memory`, `delete_memory`, `ingest_project`, `ingest_document`, `add_todo`, `list_todos`, and `close_todo`.
`search` is accepted as an alias for `recall`.

### Decide: TODO or memory?
- **TODO** (`add_todo`): explicit task with open/closed lifecycle, optionally file-linked. Use when the item needs to be tracked and deliberately closed.
- **Memory** (`add_memory`): fact, decision, preference, or observation to recall and reason about. Use when the item informs future work but has no completion state.
- Rule of thumb: task board → TODO. Notebook → memory.

### Store a memory with code-change context
- call `add_memory` with `diff=<git diff output>` after a commit
- the diff is appended to the episode body so Graphiti can reason about what changed alongside the memory text
- use for: refactors, bug fixes, architecture changes, anything where the code change is inseparable from the decision

### Inspect runtime health without full init
- read first:
  - `mcp.resource.system.dependency_health`
  - `mcp.resource.system.metadata`
  - `mcp.resource.system.lifecycle_trace`
- then open:
  - [endpoints.md](endpoints.md)
  - [architecture.md](architecture.md)

### Investigate one episode deeply
- read first:
  - `mcp.tool.get_episode_trace`
  - `mcp.tool.watch_enrichment`
  - `mcp.resource.system.processing_queue`
- then open:
  - [endpoints.md](endpoints.md)
  - [tasks-debugging.md](tasks-debugging.md)

### When MCP tools fail or return errors

MCP tools require the HTTP backend (`menhir serve`) running on port 8090. The backend is not auto-started — if it is down, all MCP tool calls will fail.

Diagnostic steps:
1. Check `memory://system/dependency-health` — lightweight Neo4j socket check, does not require full runtime init
2. If Neo4j is unreachable: the remote `menhir-neo4j.service` on the host specified by `NEO4J_URI` is likely down
3. If Neo4j is up but tools still fail: `menhir serve` is not running — start it before retrying

The CLI hook (`menhir hook run`) bypasses the HTTP backend entirely and talks directly to Neo4j. Hook output may still work even when MCP tools are failing.

The `session-init.sh` hook fires `yawn.scheduler` as a background process at session start. If the scheduler is not running, enrichment will stall but recall and ingest queuing still work.

## Recall Quality and Confidence Floor

### Legacy RRF floor (MIN_SIMILARITY_THRESHOLD = 0.15)

Recall filters the default vector lane at raw Graphiti RRF < 0.15 **before scoring**. This is not cosine similarity. Results now expose `retrieval_score_kind` and `relevance_basis=legacy_rrf_threshold_unvalidated` so consumers do not misread the legacy tier as calibrated confidence.

- Junk/gibberish queries return zero results instead of "best of bad options"
- No amount of adjacency, recency, or prominence can override a sub-threshold similarity
- Borderline matches (similarity 0.15-0.30) still pass through but get `relevance: low`

If `recall_memories` returns empty for a real query, the query text may be too vague or orthogonal to stored content. Try rephrasing with more specific domain terms.

### Relevance tier labels

Every recall result includes a `relevance` field based on raw semantic similarity:

| Tier | Similarity range | Meaning |
|------|-------------------|---------|
| high | >= 0.70 | Strong semantic match |
| medium | 0.40 - 0.69 | Moderate match, contextually useful |
| low | 0.15 - 0.39 | Weak match, may be tangential |

The tier is based on similarity alone, not the final combined score (which adds adjacency/recency/prominence boosts).

### When recall returns suspicious results

Flag these patterns and investigate:

1. **Junk returns high relevance** — should not happen after the confidence floor. If it does, check whether Graphiti's RRF reranker is returning inflated similarity scores for BM25-only matches (no cosine). This can happen if the embedding model is down and Graphiti falls back to bm25-only mode.

2. **Real query returns empty** — likely the query is too vague or uses terms not present in any stored memory. Check with a more specific query. Also check if Graphiti is reachable (degraded mode returns empty).

3. **Same entity always tops results** — may indicate stale `last_accessed` boosting recency, or an entity with extremely high edge_count dominating prominence. The scoring weights are preset-tunable.

4. **Structural entities in recall results** — should not appear in normal recall (they are filtered by scope). If you see file/symbol/directory nodes in recall output, the enrichment pipeline may be assigning wrong scope values.

5. **Conflicting memories both returned** — both sides of an unresolved conflict will appear in recall (with a conflict_signal boost so they surface for review). Use `confirm_pending_conflicts` to resolve.

### Reporting bad recall results

When you encounter recall results that don't make sense:

1. Note the query text, the returned entity names/scores, and what you expected instead
2. Store a memory with `add_memory(text="Recall quality issue: <query> returned <bad results>, expected <better results>")`
3. Check the `breakdown` field if available — the per-lane scores (sim/adj/rec/prom) reveal which lane is pushing a weak result up
4. If the issue is systemic (many queries affected), check Graphiti health and whether vector search is falling back to bm25-only

## Machine-Readable Helpers

- [mcp-tools.yaml](mcp-tools.yaml)
- [processing-states.yaml](processing-states.yaml)
