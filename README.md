# menhir

Graph-based long-term memory system for AI agents. Built on [Neo4j](https://neo4j.com/) and [Graphiti](https://github.com/getzep/graphiti), exposed as an [MCP](https://modelcontextprotocol.io/) server.

## Why

Agents forget everything between conversations. Every session starts cold — no recall of past decisions, no context about what changed yesterday, nothing accumulated over time. You can stuff the context window, maintain flat markdown files, or throw everything into a vector database, but none of those actually work past a certain scale. Context windows fill up. Files go stale. Vector search finds similar text but has no concept of how memories relate to each other.

menhir is a knowledge graph. Memories are entities with relationships between them, and those relationships matter during retrieval. When the system recalls a memory, it doesn't just check cosine similarity — it also looks at how connected that memory is to other relevant ones, how recently it was accessed, and how central it is in the graph. A well-connected memory about a topic you keep revisiting will outrank an isolated note with the same keywords.

The graph also decays. Memories that stop being useful compress into summaries, and eventually expire. Memories that contradict each other get flagged. The system doesn't just accumulate — it manages what it knows.

## How it works

### Ingestion

An agent calls `add_memory` with some text. That gets queued and processed in the background — the agent doesn't block. An LLM (local or cloud) extracts entities and relationships via Graphiti, merges them into Neo4j, and stamps policy metadata (scope, session, source). If the episode text mentions file paths, those get linked to the structural code graph too.

```
Episode text → queue → LLM extraction → Neo4j merge → metadata stamp → structural anchoring
```

### Recall

Two phases:

1. **Vector search** — Graphiti runs hybrid BM25 + cosine to find candidate memories
2. **Graph scoring** — candidates get re-ranked by a formula that weighs similarity, graph adjacency, recency, and prominence

So a memory that's both relevant *and* well-connected to other things you've been working on ranks higher than a keyword match sitting by itself.

If you pass `file_context`, memories linked to that file and its import neighborhood get injected into the candidate pool — even if vector search alone wouldn't have found them.

### Lifecycle

Memories aren't permanent. They move through states:

```
 SESSION ──promote──→ PERSISTENT
                          │
                     ┌────┴────┐
                  ACTIVE   COMPRESSED
                     │         │
                     └────┬────┘
                          ↓
                        GONE ──→ (deleted)
```

- **Session** — temporary, from one conversation. Gets promoted if it builds enough connections to persistent memories.
- **Active** — working set. Full content, regularly accessed.
- **Compressed** — content replaced with an LLM summary after inactivity. Auto-rehydrated if recalled again.
- **Gone** — queued for deletion. Neighbors get bridged first so the graph stays connected.

Stuff that keeps getting recalled stays sharp. Stuff that doesn't, fades out.

### Code graph

The system also indexes project structure — files, imports, tests, endpoints, dependencies — into the same Neo4j database. A background watcher re-scans every 30 minutes using fingerprint-based change detection.

Two things this gives you:

- **Blast radius** — change a file, see what's affected downstream, which tests to run, and what memories are linked to the impacted code
- **Structure-aware recall** — pass a file path to recall and get memories related to that code, even when the query text has nothing to do with it

The link between semantic memories and structural entities is the `ANCHORED_TO` edge. Created during enrichment when episode text mentions file paths. Narrative mentions get full weight, diff mentions get 30%. These edges are walled off from lifecycle so they don't mess with decay or promotion.

## What you get

- Context carries across conversations — decisions, preferences, project knowledge don't vanish when the session ends.
- Retrieval beats plain vector search. Graph adjacency means related memories reinforce each other during scoring.
- Compression, decay, and conflict detection are automatic. You don't maintain the graph manually.
- The system knows what file you're working on and can surface relevant context and impact analysis without you asking.
- Two conflicting memories get flagged instead of silently coexisting.
- Ingestion is async — agents don't block waiting for enrichment.
- All enrichment, scoring, and lifecycle transitions log to a SQLite sidecar for debugging.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  MCP Server (stdio)                                 │
│  43 tools · 9 resources                             │
├─────────────────────────────────────────────────────┤
│  Services                                           │
│  IngestService · RecallService · ScoringService     │
│  LifecycleService · MaintenanceScheduler            │
├─────────────────────────────────────────────────────┤
│  Infrastructure                                     │
│  GraphitiClient · Neo4jRepository                   │
│  MemoryGraphAdapter · StructureGraphWriter          │
│  EmbeddingCache · CircuitBreaker · LLMAdapter       │
├─────────────────────────────────────────────────────┤
│  Storage                                            │
│  Neo4j 5 (graph) · SQLite (telemetry + audit)       │
└─────────────────────────────────────────────────────┘
```

## Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12+ |
| Graph database | Neo4j 5 |
| Graph memory framework | graphiti-core 0.29+ |
| LLM backends | Local llama.cpp (OpenAI-compatible), OpenAI, Gemini |
| Protocol | Model Context Protocol (MCP) via stdio |
| Embeddings | Local or OpenAI (configurable) |
| Observability | SQLite sidecar + optional Langfuse tracing |
| Visualization | FastAPI explorer with Cytoscape.js |

## Security

No-key/open-auth mode is **loopback-only** by default:

- If no bearer key is configured (`MENHIR_API_KEY`, `MENHIR_AGENT_KEY`,
  `MENHIR_OPERATOR_KEY`, `MENHIR_READONLY_KEY`), the server refuses to bind to
  non-loopback hosts (`0.0.0.0`, `::`, LAN/public IPs).
- Remote no-auth requires `MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1`.
  This override is unsafe and intended only for isolated local lab networks.

### Operator diagnostics

Use this command to inspect the redacted local safety posture before running
Menhir. No secrets are printed. No network or database connection is required.

```bash
python -m menhir.cli diagnostics
python -m menhir.cli diagnostics --json
```

### OAuth resource-server runbook

For remote MCP/OAuth setup checks, use the [OAuth remote MCP operator checklist](docs/runbooks/oauth-remote-mcp-checklist.md). The checklist is docs-only guidance for validating resource-server metadata, bearer challenges, query-string fallback rejection, and later real-token smoke testing. It is not a compatibility claim for any specific remote client.

## Setup

### Prerequisites

- Python 3.12+
- Neo4j 5 (remote, accessed via `NEO4J_URI` in .env)
- An LLM backend (local llama.cpp or OpenAI API key)

### Install

```bash
git clone https://github.com/Archolith/menhir.git
cd menhir
pip install -e .
```

Installing pulls two first-party dependencies straight from GitHub, so `git` must be
available on your PATH.

To also install the test tooling, use the `dev` dependency group (PEP 735, needs pip 25.1+):

```bash
pip install -e . --group dev
```

### Configure

```bash
cp .env.example .env
# Edit .env with your Neo4j and LLM settings
```

Key environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `NEO4J_URI` | Neo4j connection | `bolt://localhost:7687` |
| `NEO4J_PASSWORD` | Neo4j password | — |
| `LLM_CHAT_PROVIDER` | Chat backend (`openai_compat`, `openai`, `gemini`) | `openai_compat` |
| `GRAPHITI_LLM_PROVIDER` | Extraction backend | `openai_compat` |
| `LOCAL_LLM_BASE_URL` | Local llama.cpp endpoint (legacy alias: `LLAMA_BASE_URL`) | `http://127.0.0.1:8081/v1` |
| `OPENAI_API_KEY` | OpenAI key (if using OpenAI provider) | — |
| `SCHEDULER_URL` | Optional external scheduler that manages llama-server model endpoints | `http://localhost:8082` |

See [`.env.example`](.env.example) for the full list.

### Hook capture and privacy

Installing or starting Menhir does not automatically install editor/agent hooks. If an
operator explicitly enables the supplied Claude Code, Codex, or OpenCode hooks, accepted
user prompts may be stored with provenance such as the working directory and transcript
path. Review [the turn-evidence producer documentation](docs/turn-evidence-producers.md)
before enabling hooks, and use the privacy controls described there for sensitive work.

### Neo4j setup

The repo ships a compose file that starts a local Neo4j with the APOC plugin:

```bash
docker compose up -d
```

That matches the `NEO4J_URI` default (`bolt://localhost:7687`); set `NEO4J_PASSWORD` in
`.env` to match. To point at an existing Neo4j instead, set the connection in `.env`:

```bash
NEO4J_URI=bolt://<neo4j-host>:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<password>
```

The server's preflight check will verify connectivity and report errors clearly in `/api/ready`.

### Run the server

Menhir runs as a long-lived server. It holds the Neo4j pool, the background enrichment
queue, and the maintenance scheduler, so it is started once and left running rather than
spawned per client session:

```bash
menhir serve
```

Useful subcommands: `menhir check` and `menhir diagnostics` for preflight and redacted
safety posture, `menhir console` for the interactive shell, `menhir serve-watch` to reload
on source changes.

### Register with your MCP client

MCP is served over HTTP at `/mcp-http`. Point your client (Claude Desktop, Claude Code) at
the running server:

```json
{
  "mcpServers": {
    "memory": {
      "type": "http",
      "url": "http://127.0.0.1:8100/mcp-http",
      "headers": {
        "Authorization": "Bearer <your-key>"
      }
    }
  }
}
```

Use one of the keys you configured in `.env` (`MENHIR_API_KEY`, `MENHIR_AGENT_KEY`,
`MENHIR_OPERATOR_KEY`, `MENHIR_READONLY_KEY`) — the tier decides which tools the client may
call. On a loopback bind with no key configured, the server runs open; see
[Security](#security) before binding anywhere else.

### Run in Docker

The repo builds a self-contained runtime image. Menhir and an isolated Neo4j come up
together:

```bash
cp deploy/.env.deploy.example deploy/.env.deploy   # add your OPENAI_API_KEY
docker compose -f deploy/docker-compose.full.yml up -d --build
curl -fsS http://127.0.0.1:8099/api/health
```

The image publishes to `127.0.0.1` only and enables client tokens, because the container
binds `0.0.0.0` internally and Menhir refuses an unauthenticated non-loopback bind. If you
run the image directly rather than through the compose file, set one of the key variables
or the server will refuse to start.

Two caveats worth knowing before choosing Docker over a local install:

- **Code-graph features expect host paths.** `ingest_project` records absolute paths as it
  scans them, so a containerized server indexes container paths while your MCP client
  reports host paths. `file_context`, `blast_radius`, and structural anchoring only line up
  if you mount your code at a path identical to the host's.
- **A local LLM on the host is not reachable at `127.0.0.1` from inside the container.**
  Point `LOCAL_LLM_BASE_URL` at `host.docker.internal` instead.

### Run the graph explorer

The explorer is automatically mounted on the main server at `/explorer`:

```bash
menhir serve
# Explorer available at http://127.0.0.1:8100/explorer
```

The explorer shares the backend's Neo4j pool and supervised lifecycle. On non-loopback binds, it requires a bearer token (same as the `/api/*` surface).

## MCP Tools

### Memory ingestion

| Tool | Purpose |
|------|---------|
| `add_memory` | Queue a memory for background enrichment |
| `add_memory_and_track` | Queue and track enrichment progress live |
| `ingest_project` | Scan a codebase and index its structure |

### Memory recall

| Tool | Purpose |
|------|---------|
| `recall_memories` | Semantic search with ranked scoring and optional `file_context` |
| `recall_context_memories` | Startup context retrieval (relevant + recent) |
| `read_flagged_memories` | Read permanently flagged memories for bootstrap |
| `build_context` | Token-budget-limited context assembly |

### Structural queries

| Tool | Purpose |
|------|---------|
| `query_structure` | Query the code graph — files, imports, tests, endpoints, dependencies |
| ↳ `blast_radius` | Trace impact of file changes with related memories |
| ↳ `affected_tests` | Minimal test set for changed files with pytest command |

### Conflict management

| Tool | Purpose |
|------|---------|
| `list_conflicts` | View grouped memory contradictions |
| `resolve_conflict` | Resolve via keep_both, replace, or discard_new |
| `scan_for_conflicts` | Scan for new similarity-based conflicts |
| `run_llm_conflict_review` | LLM contradiction confirmation |

### Operations

| Tool | Purpose |
|------|---------|
| `get_enrichment_status` | Inspect episode processing state |
| `list_enrichment_queue` | Queue overview with stale-state hints |
| `watch_enrichment` | Live delta-oriented enrichment monitoring |
| `get_episode_trace` | Debug bundle: queue row + telemetry history |
| `repair_stale_enrichment` | Fix stuck ENRICHING episodes |
| `get_memory_stats` | Health summary: latency, failures, queue depth |
| `flag_memory` / `delete_memory` | Manual retention control |

## Scoring

The ranking formula has four signals beyond raw similarity:

```
relevance = similarity + α×adjacency + β×recency + γ×prominence + δ×conflict
```

- **Adjacency** — memories that cluster together in the graph reinforce each other. More connections to other candidates = higher score.
- **Recency** — recently accessed memories get a boost.
- **Prominence** — well-connected memories (more edges) are treated as more central.
- **Conflict** — unresolved contradictions get surfaced. The conflict preset cranks this signal up.

Five presets shift the balance depending on what you're doing:

| Preset | Use case | Favors |
|--------|----------|--------|
| knowledge | General recall, Q&A | Similarity + graph structure |
| recent | "What was I just working on?" | Recency above all |
| connected | Exploring related context | Graph adjacency + prominence |
| emotional | Sentiment-laden recall | Recency + adjacency balance |
| conflict | Surfacing contradictions | Conflict signal at 0.4 weight |

## Tests

```bash
pytest                  # full offline suite; online tests skip unless explicitly enabled
pytest -m unit          # faster local subset only; CI intentionally does not use this filter
pytest --run-online     # adds the live suite; requires Neo4j and an LLM
```

Online tests run unscoped, destructive queries and must never point at a real graph. Use
the throwaway instance for them:

```bash
docker compose -f docker-compose.test.yml up -d
MENHIR_TEST_NEO4J_URI=bolt://localhost:7688 pytest --run-online
docker compose -f docker-compose.test.yml down -v
```

## Project structure

```
src/menhir/
├── config/          Settings (env-backed)
├── core/            Service wiring (build_memory_services, BuildArtifacts)
├── domain/          Models, recall types, memory type policies, scoring
├── infrastructure/  Neo4j, Graphiti, adapters, structure queries, anchoring
├── services/        Ingest, Recall, Scoring, Lifecycle, Scheduler
├── mcp/             MCP server, tools, resources, contracts, telemetry
└── explorer/        FastAPI graph visualization UI
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for
attribution and [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt) for dependency licenses.
