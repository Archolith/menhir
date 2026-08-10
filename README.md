# Menhir

Menhir is a graph-based memory service for AI agents. It stores memories in
[Neo4j](https://neo4j.com/), uses
[Graphiti](https://github.com/getzep/graphiti) for extraction and graph search, and
serves its tools through the [Model Context Protocol](https://modelcontextprotocol.io/).

The current package version is `0.2.0`. Menhir is built for a single operator and
requires Python 3.12 or newer.

## What Menhir does

Menhir keeps durable context outside an agent's context window. An agent can add a
memory, recall related information later, inspect conflicts, and query a project's
structure from the same service.

A running Menhir instance provides:

- asynchronous memory ingestion with entity and relationship extraction
- typed scalar assertions with rebuildable current-state and history views
- hybrid recall followed by graph-aware reranking
- project indexing for files, imports, tests, endpoints, and dependencies
- lifecycle compression, rehydration, conflict tracking, and manual retention controls
- HTTP MCP, a backend-first stdio bridge, REST endpoints, and a graph explorer
- SQLite telemetry for queue, recall, and lifecycle diagnostics

Menhir registers 52 MCP tools and 9 read-only MCP resources. The gateway keeps a small
set of common tools visible and exposes the rest through tool discovery.

## How memory moves through the system

### Ingestion

`add_memory` writes an episode to the queue and returns without waiting for extraction.
A background worker sends the episode to the configured LLM, merges the extracted
entities and relationships into Neo4j, and records scope and provenance metadata.

If the text contains file paths that already exist in the structure graph, enrichment
can connect the memory to those files with `ANCHORED_TO` relationships.

```text
episode -> queue -> LLM extraction -> Neo4j merge -> metadata -> structural anchors
```

### Recall

Graphiti supplies candidates using hybrid BM25 and vector search. Menhir then reranks
them with semantic similarity, graph adjacency, recency, prominence, and conflict
signals. Passing `file_context` adds memories anchored to that file and nearby imports
to the candidate pool.

The scoring model is intended to make relationships and current project context useful
during recall. The repository does not claim that this is universally better than a
vector-only system; retrieval quality depends on the data, provider, and tuning.

### Lifecycle

Memories can be session-scoped, persistent, active, compressed, promoted, flagged, or
marked gone. The maintenance scheduler runs lifecycle consolidation and decay checks
daily. Eligible inactive memories may be compressed, and compressed content can be
rehydrated when new context arrives. Flagged and promoted memories receive stronger
retention protection.

Automatic transitions from `COMPRESSED` to `GONE` are currently disabled. The old
deletion threshold was tied to a score that did not provide a safe basis for irreversible
deletion. Manual deletion remains available to an operator, while automatic decay favors
retention until a replacement policy is validated.

Event-history recall authority, deterministic typed-scalar routing, and frontier
retrieval experiments ship behind default-off flags. The
[activation ledger](.agent/default-off-features.md) records why each feature is off and
what must happen before its default changes.

### Project structure

`ingest_project` indexes files, imports, tests, endpoints, and dependencies. After a
project has been indexed, the structure watcher checks it every 30 minutes using file
fingerprints.

`query_structure` supports questions such as:

- which files depend on a changed module
- which tests cover the affected files
- which endpoints are defined in a module
- which memories are anchored to nearby code

Project indexing is explicit. Menhir does not know an editor's current file unless the
client supplies file context or an operator enables an integration that provides it.

## Typed scalar memory and current priorities

Recent work has focused on turning grounded statements into typed, auditable state. This
is different from storing another prose summary. The scalar path keeps the original
observation and derives a current value that can be rebuilt:

```text
TurnEvidence
  -> typed scalar perception and admission
  -> immutable TypedAssertion
  -> deterministic fold
  -> ScalarStateView and ScalarHistoryView
```

A typed assertion records the subject, attribute, value, unit, operation, source span,
namespace, and time. The fold can combine an absolute value with later deltas, handle
corrections and supersession, and retain the assertions that contributed to the current
View. `ScalarStateView` represents current state. `ScalarHistoryView` is an advisory
record of changes rather than a competing source of current truth.

The scalar assertion, persistence, fold, repair, and inspection infrastructure is
implemented. Scalar-state activation and recall authority remain opt-in while the system
is checked against held-out extraction, namespace, replay, and repair cases. The
deterministic extractor and router are also default-off; the extractor can run as an
observe-only shadow without changing persistence or recall. Event-history authority
follows the same rollout discipline and remains default-off.

### Why ingestion comes first

Menhir treats retrieval as the evidence selector, not the place where missing semantic
structure should be invented. Recall cannot repair a fact that was never extracted, was
bound to the wrong subject or namespace, lost its provenance, or folded into the wrong
current value.

That makes ingest and projection correctness the current priority. The work is ordered
around four questions:

1. Did perception extract the atomic claim from an exact source span?
2. Was the claim admitted, bound, and namespaced correctly?
3. Can the durable assertions deterministically rebuild the expected View?
4. Can replay, repair, and coverage checks account for every assertion and projection?

Retrieval and context presentation come after those checks. This is also why tentative
intent is planned as an ingest-owned assertion and View instead of a recall-time phrase
classifier. Until admitted Intent Views exist, ordinary prose remains general content.

The [typed recall packet decision](.agent/plans/typed-recall-packet-prototype.md) records
the ingest-owned boundary. The
[projection and realization coverage plan](.agent/plans/menhir-projection-realization-coverage-implementation.md)
describes the next reliability and observability work. The
[activation ledger](.agent/default-off-features.md) lists the paths that are shipped but
not enabled by default.

## Interfaces

`menhir serve` starts one long-lived FastAPI process. That process owns the Neo4j pool,
the enrichment queue, and the maintenance scheduler.

| Interface | Default location | Notes |
|-----------|------------------|-------|
| Remote MCP | `http://127.0.0.1:8100/mcp-http` | Streamable HTTP transport |
| REST API | `http://127.0.0.1:8100/api` | Health, readiness, memory, and operator routes |
| Explorer | `http://127.0.0.1:8100/explorer` | Browser-based graph inspection |
| Stdio bridge | `python -m menhir.mcp.server` | Trusted local bridge to a running backend |

The stdio bridge does not create a second runtime. Set `MENHIR_BACKEND_URL` to the
running HTTP backend before launching it.

## Quick start

### Prerequisites

- Python 3.12 or newer
- Git, because two first-party dependencies install from public GitHub repositories
- Neo4j 5 with APOC
- a local OpenAI-compatible server, OpenAI, or Gemini

### Install

```bash
git clone https://github.com/Archolith/menhir.git
cd menhir
python -m pip install .
```

For an editable development install with the PEP 735 development dependency group:

```bash
python -m pip install -e . --group dev
```

The dependency-group command requires pip 25.1 or newer.

### Configure

```bash
cp .env.example .env
# Edit .env for your Neo4j and LLM provider.
```

The default configuration expects Neo4j and a local OpenAI-compatible model server on
the same machine.

| Variable | Purpose | Default |
|----------|---------|---------|
| `NEO4J_URI` | Neo4j connection | `bolt://localhost:7687` |
| `NEO4J_USER` | Neo4j user | `neo4j` |
| `NEO4J_PASSWORD` | Neo4j password | empty |
| `LLM_CHAT_PROVIDER` | Chat provider: `local`, `openai`, or `gemini` | `local` |
| `GRAPHITI_LLM_PROVIDER` | Graphiti extraction provider | `local` |
| `GRAPHITI_EMBED_PROVIDER` | Optional separate embedding provider | inherits Graphiti provider |
| `LOCAL_LLM_BASE_URL` | Local OpenAI-compatible chat endpoint | `http://127.0.0.1:8081/v1` |
| `OPENAI_API_KEY` | Credential used when the provider is `openai` | empty |
| `GEMINI_API_KEY` | Credential used when the provider is `gemini` | empty |
| `SCHEDULER_URL` | Optional external model scheduler | `http://localhost:8082` |

See [`.env.example`](.env.example) for model names, separate embedding endpoints, OAuth,
telemetry, and experimental flags.

### Start Neo4j

The root compose file starts a local Neo4j 5 instance with APOC:

```bash
docker compose up -d
```

The root compose file uses `neo4j/password`, so set `NEO4J_PASSWORD=password` in `.env`.
To use an existing database, configure it directly:

```dotenv
NEO4J_URI=bolt://neo4j-host:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=replace-me
```

### Check and run Menhir

```bash
menhir check
menhir diagnostics
menhir serve
```

`menhir diagnostics --json` reports the redacted local security posture without printing
secrets or connecting to the network or database. Once the server starts, use these
endpoints for process and dependency checks:

```bash
curl -fsS http://127.0.0.1:8100/api/health
curl -fsS http://127.0.0.1:8100/api/ready
```

Other CLI commands include `menhir console` for an interactive shell and
`menhir serve-watch` for a local restart watchdog.

### Connect an MCP client

Point an HTTP-capable MCP client at `/mcp-http`:

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

Static credentials can be configured with `MENHIR_API_KEY`, `MENHIR_AGENT_KEY`,
`MENHIR_OPERATOR_KEY`, or `MENHIR_READONLY_KEY`. The credential tier controls which tools
the client may call.

If no credential is configured, Menhir permits open access only on a loopback bind. See
[Security](#security) before exposing the service to another machine.

## Docker test stack

The deployment compose file starts Menhir and an isolated, disposable Neo4j instance.
It is a test stack, not a production template, and its supplied configuration uses
OpenAI for extraction and embeddings.

```bash
cp deploy/.env.deploy.example deploy/.env.deploy
# Set OPENAI_API_KEY and MENHIR_OPERATOR_KEY in deploy/.env.deploy.
docker compose -f deploy/docker-compose.full.yml up -d --build
curl -fsS http://127.0.0.1:8099/api/health
```

The compose file publishes Menhir on loopback and enables client tokens. If you run the
image directly with a non-loopback bind, configure authentication or startup will fail.
Do not connect this stack or its tests to a graph that contains data you want to keep.

Two path and networking details matter in Docker:

- `ingest_project` records absolute paths. Mount source code at the same path seen by the
  MCP client if you want `file_context` and structural anchors to match.
- `127.0.0.1` inside a container is the container itself. To use a model server on the
  host, set `LOCAL_LLM_BASE_URL` to an address such as `host.docker.internal`.

See [`deploy/README.md`](deploy/README.md) for client-token bootstrap and deployment
details.

## Security and privacy

Menhir is a single-operator service, not a multi-tenant boundary. Run a separate instance
for each operator or trust domain. The full threat model and known limitations are in
[`docs/security-posture.md`](docs/security-posture.md).

With no key configured, the server refuses non-loopback binds. Bypassing that guard with
`MENHIR_ALLOW_INSECURE_REMOTE_NO_AUTH=1` is unsafe and is intended only for an isolated
local lab network.

For OAuth resource-server deployments, follow the
[remote MCP checklist](docs/runbooks/oauth-remote-mcp-checklist.md). It covers metadata,
bearer challenges, query-string credential rejection, and token smoke tests without
claiming compatibility with a particular client.

Installing or starting Menhir does not install editor or agent hooks. If you enable the
included Claude Code, Codex, or OpenCode hooks, accepted prompts may be stored with the
working directory and transcript path. Review the
[turn-evidence producer documentation](docs/turn-evidence-producers.md) before using hooks
for sensitive work.

Report vulnerabilities privately as described in [`SECURITY.md`](SECURITY.md). Do not
open a public issue for a security report.

## Selected MCP tools

Menhir registers 52 tools. These are the main entry points; clients can discover the full
set through the MCP gateway.

### Store and retain memory

| Tool | Purpose |
|------|---------|
| `add_memory` | Queue a memory for enrichment |
| `add_memory_and_track` | Queue a memory and stream processing progress |
| `ingest_document` | Ingest a document as memory episodes |
| `flag_memory` | Protect a memory from normal lifecycle decay |
| `promote_memory` | Promote a memory to durable scope |
| `delete_memory` | Delete a memory under operator control |

### Recall and context

| Tool | Purpose |
|------|---------|
| `recall_memories` | Search and rerank memories, with optional file context |
| `recall_context_memories` | Retrieve recent and relevant startup context |
| `read_flagged_memories` | Read memories selected for bootstrap |
| `build_context` | Assemble context within a token budget |
| `rate_recall` | Record explicit retrieval feedback |

### Inspect projects and operations

| Tool | Purpose |
|------|---------|
| `ingest_project` | Index a repository's structure |
| `query_structure` | Query files, imports, tests, endpoints, and dependencies |
| `get_enrichment_status` | Inspect one episode's processing state |
| `watch_enrichment` | Monitor enrichment changes |
| `get_episode_trace` | Read queue and telemetry history for an episode |
| `get_memory_stats` | Summarize latency, failures, and queue depth |

### Resolve conflicts

| Tool | Purpose |
|------|---------|
| `list_conflicts` | List grouped contradictions |
| `resolve_conflict` | Keep both memories, replace one, or discard the new one |
| `scan_for_conflicts` | Scan for similarity-based conflict candidates |
| `run_llm_conflict_review` | Ask the configured LLM to review unresolved conflicts |

The tool set also includes todo and artifact operations, client-token administration,
scheduler controls, provenance inspection, and enrichment repair.

## Retrieval scoring

Menhir combines five signals after candidate retrieval:

```text
score = similarity + alpha*adjacency + beta*recency + gamma*prominence + delta*conflict
```

| Signal | Meaning |
|--------|---------|
| Similarity | Semantic and lexical relevance from Graphiti retrieval |
| Adjacency | Connections to other candidates in the graph |
| Recency | How recently the memory was accessed |
| Prominence | The memory's graph connectivity |
| Conflict | A boost for unresolved contradictions when requested |

Presets named `knowledge`, `recent`, `connected`, `emotional`, and `conflict` adjust the
weights for different recall tasks.

## Tests

The default pytest run covers the offline suite. Tests marked `online` skip unless
`--run-online` is present.

```bash
pytest
pytest -m unit
```

The CI graph-backed job uses a disposable Neo4j instance and excludes the small subset
that requires a live LLM:

```bash
docker compose -f docker-compose.test.yml up -d
MENHIR_TEST_NEO4J_URI=bolt://localhost:7688 \
  pytest -m "online and not needs_llm" --run-online
docker compose -f docker-compose.test.yml down -v
```

Online tests run destructive, unscoped graph queries. Never point them at a database that
contains data you want to keep.

## Project layout

```text
src/menhir/
|-- api/             FastAPI routes, authentication, OAuth, and remote MCP
|-- cli/             Command-line interface and hook commands
|-- config/          Environment-backed runtime settings
|-- core/            Runtime construction and service wiring
|-- domain/          Memory models, policies, recall types, and scoring
|-- explorer/        Browser-based graph explorer
|-- infrastructure/  Neo4j, Graphiti, telemetry, and structure queries
|-- mcp/             MCP tools, resources, contracts, and stdio bridge
`-- services/        Ingestion, recall, lifecycle, conflict, and scheduler logic
```

## License

Menhir is licensed under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for
attribution and [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt) for dependency
licenses.
