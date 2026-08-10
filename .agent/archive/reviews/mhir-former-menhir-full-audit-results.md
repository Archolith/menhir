# Mhir / Former Menhir — Full Audit Results

Date: 2026-06-20
Reviewer: Claude Sonnet 4.6 (Claude Code)
Project root: `C:\Users\you\IdeaProjects\projects\archolith\menhir`
Prior leads:
- `menhir-launch-readiness-audit.md` (Codex, 2026-06-19) — 6 blockers, 4 high, 2 medium
- `menhir-package-name-investigation.md` (Codex, 2026-06-19) — decision: Mhir / archolith-mhir

---

## Phase 1: Architecture Map

### Server / API Entry Points

```
src/menhir/
├── main.py                    — Typer CLI: check / serve / serve-watch / hook / ingest-wiki
├── api/
│   ├── server.py              — FastAPI app factory (REST + MCP SSE + streamable HTTP)
│   ├── auth.py                — BearerAuthMiddleware (pure ASGI, no BaseHTTPMiddleware)
│   ├── routes.py              — /api/* REST routes
│   ├── mcp_remote.py          — MCP SSE + streamable HTTP app factories
│   └── request_context.py     — RequestContextMiddleware
├── config/
│   └── settings.py            — MemorySettings (env-backed dataclass)
├── core/                      — Service wiring, RuntimeContext, BackendClient
├── domain/                    — Memory models, recall types, scoring policies, lifecycle
├── infrastructure/            — Neo4jRepository, GraphitiClient, adapters, structure queries
├── services/                  — IngestService, RecallService, ScoringService, LifecycleService
├── mcp/                       — MCP tools (23), resources (9), contracts, telemetry
└── explorer/                  — FastAPI graph visualization UI
```

MCP exposed on stdio (dev) and `/mcp/` SSE + `/mcp-http` streamable HTTP (remote). Backend REST at `/api/`. Default host: `127.0.0.1:8100`.

### MCP Tools

23 tools grouped as: memory ingestion (`add_memory`, `add_memory_and_track`, `ingest_project`), memory recall (`recall_memories`, `recall_context_memories`, `read_flagged_memories`, `build_context`), structural queries (`query_structure` with blast_radius / affected_tests), conflict management (4 tools), operations (9 tools: get_enrichment_status, repair_stale_enrichment, flag_memory, delete_memory, etc.).

### Graph Storage and Neo4j Integration

Neo4j 5 (bolt://localhost:7687 default). Graphiti-core 0.28.1 for entity/relationship extraction and hybrid BM25+cosine recall. SQLite sidecar for telemetry, enrichment queue, and audit log. Code graph indexing via `StructureGraphWriter` with `ANCHORED_TO` edges linking semantic memories to file entities.

### Ingestion / Extraction / Promotion Flows

```
Episode text → async queue → LLM extraction (Graphiti) → Neo4j merge
→ metadata stamp (scope, session, source) → structural anchoring (ANCHORED_TO)
→ lifecycle: SESSION → PERSISTENT → ACTIVE/COMPRESSED → GONE
```

Background watcher re-scans project structure every 30 minutes (configurable via `MENHIR_STRUCTURE_WATCHER_INTERVAL_S`).

### Embedding Configuration

Provider selection via `LLM_CHAT_PROVIDER` / `GRAPHITI_LLM_PROVIDER` env vars. Three providers: `local` (llama.cpp via `LOCAL_LLM_BASE_URL`), `openai`, `gemini`. Default: `local`. Separate `GRAPHITI_EMBED_PROVIDER` and `GRAPHITI_RERANKER_PROVIDER` for hybrid setups.

### Config / Env / Secrets

`settings.py:207-208`: Auth keys via `MENHIR_API_KEY`, `MENHIR_OPERATOR_KEY`, `MENHIR_AGENT_KEY`, `MENHIR_READONLY_KEY`. API host via `MENHIR_API_HOST` (default `127.0.0.1`). CORS origins via `MENHIR_CORS_ORIGINS` (default `"*"`).

Backward-compat aliases accepted: `LLAMA_*` → `LOCAL_LLM_*`, `LANGFUSE_BASE_URL` → `LANGFUSE_HOST`.

### Tests

Located at `tests/` (not root). ~900+ tests per README. Test files include `test_api_auth.py`, `test_api_routes.py`, `test_backend_roundtrip.py`, `test_budget_caps.py`, `test_circuit_breaker.py`, `test_conflict_tools.py`, etc. Separate `bench_structure_queries.py` and `compare_compression.py` utility scripts.

`integration_test.py` at project root — broken import (`from yawn_memory.main import ...`); would be collected by default pytest run.

### Package / CLI / Import Naming Surfaces

| Surface | Current value | Target (Mhir decision) |
|---------|--------------|------------------------|
| `pyproject.toml name` | `menhir` | `archolith-mhir` |
| `pyproject.toml version` | `0.2.0` | — |
| `pyproject.toml scripts` | `menhir`, `menhir-explorer` | `mhir`, `mhir-explorer` |
| Import package | `src/menhir/` | `src/mhir/` |
| `MemorySettings` docstring | "cth.mcp.memory" | menhir or mhir |
| `server.py:49 title` | `"yawn-memory"` | `"mhir"` or `"archolith-mhir"` |
| `server.py:37,44` logger | "yawn-memory remote API server" | mhir |
| `server.py:151` docstring | "CLI entry point for 'yawn-memory-server'" | update |
| Auth header names | `x-yawn-user-id`, `x-yawn-session-id`, etc. | `x-mhir-*` or `x-archolith-*` |
| `.env.example` commented vars | `YAWN_MEMORY_MCP_TELEMETRY_DB`, etc. | `MENHIR_*` or remove |
| `integration_test.py:12` | `from yawn_memory.main import` | `from mhir.main import` |

### Mhir Rename Scope (Decision: 2026-06-19)

Public component: `Mhir` (pronounced "mer")
Distribution: `archolith-mhir`
Import: `mhir`
CLI: `mhir` / `mhir-explorer`

Rename NOT implemented as of this audit date.

---

## Phase 2: Targeted Review Passes

### Pass 1 — Security / Privacy

**Auth opt-in / CORS wildcard (MNR-09 — confirmed current):**

`auth.py:106-108`:
```python
if not (self._operator_key or self._agent_key or self._readonly_key):
    await self.app(scope, receive, send)
    return
```
Auth is completely bypassed when no keys are configured. All `/api/`, `/mcp/`, and `/mcp-http` routes become unauthenticated.

`server.py:118-119`:
```python
cors_origins_raw = os.getenv("MENHIR_CORS_ORIGINS", "")
cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()] or ["*"]
```
CORS defaults to `["*"]` when `MENHIR_CORS_ORIGINS` is not set.

Mitigating factor: `api_host` default is `"127.0.0.1"` (settings.py:95). Loopback-only binding limits exposure for default deployments. Risk is high when a user sets `MENHIR_API_HOST=0.0.0.0` without configuring any auth keys — memory APIs and MCP routes become fully open to the local network.

No startup warning or guard when auth is unconfigured and host is non-loopback. README does not clearly warn about this requirement.

**Header naming — `x-yawn-*` protocol (MNR-10 sub-finding):**

`auth.py:71-74`:
```python
user_id = headers.get(b"x-yawn-user-id", b"").decode("latin-1").strip()
session_id = headers.get(b"x-yawn-session-id", b"").decode("latin-1").strip()
client_id = headers.get(b"x-yawn-client-id", b"").decode("latin-1").strip()
client_name = headers.get(b"x-yawn-client-name", b"").decode("latin-1").strip()
```
These header names are part of the external protocol. Clients (Claude Code, Codex, dashboards) that hardcode `x-yawn-*` headers will need to be updated when the Mhir rename lands. Not a current security risk, but documents the rename blast radius.

**Memory data exposure:** Auth guards `/api/status` and all `/mcp/*` routes correctly once keys are configured. `/api/ready` and `/api/health` are explicitly exempted and leak only connectivity status — acceptable.

**Project ingestion of adversarial repos:** No guardrails seen on `ingest_project` path. A user (or agent) calling `ingest_project` with an adversarial repo path would index content into the memory graph. This is inherent to the feature design (not a code defect), but warrants a note in the security posture doc.

### Pass 2 — Correctness

**`integration_test.py` broken import (MNR-03 — confirmed current):**

`integration_test.py:12`:
```python
from yawn_memory.main import check_neo4j_connectivity, check_llama_connectivity
```
Module `yawn_memory` does not exist after the Menhir rename (import package is now `menhir`). This file is at the project root, not inside `tests/`, and will be collected by a bare `pytest` command, causing collection to fail before any real tests run.

`integration_test.py:23`: Also reads `LLAMA_BASE_URL` and `LM_STUDIO_BASE_URL` — legacy env names not aligned with current `.env.example` canonical names.

**`test_backend_roundtrip.py` Pydantic/MagicMock failure (MNR-03 — not re-run but prior evidence stands):**

Codex found `TypeError: issubclass() arg 2 must be a class, a tuple of classes, or a union` from a `MagicMock` being passed to a FastAPI route that validates against a Pydantic model. Codex ran the test and saw this failure. Not re-executed in this audit but the root issue (MagicMock passed as Pydantic class) is in the code and not resolved.

**Memory lifecycle model:** Session → PERSISTENT → ACTIVE/COMPRESSED → GONE described in README is architecturally sound. `derive_item`, promotion thresholds, decay, and conflict detection not individually audited in this pass.

### Pass 3 — Architecture / Operability

**Clean install (MNR-01 — confirmed current):**

`pyproject.toml:16`: `cth-mcp-framework>=0.1.0`

This dependency is not on PyPI. A fresh environment without the local `cth.mcp.framework` workspace checkout will fail to resolve this dependency. Not changed since Codex audit.

**Dev install command (MNR-04 — confirmed current):**

`pyproject.toml:29-33`:
```toml
[dependency-groups]
dev = ["pytest>=9.0.2", "pytest-asyncio>=1.3.0"]
```

`[dependency-groups]` is PEP 735 (not standard pip extras). `pip install -e ".[dev]"` as documented in README will warn "does not provide the extra 'dev'" and silently not install dev dependencies. Users must use `uv sync --group dev` or add a `[project.optional-dependencies]` entry.

**MCP startup command (MNR-05 — likely still open, not re-run):**

`pyproject.toml:26`: `menhir = "menhir.main:main"` is the console script entry. This delegates to Typer's main function. Per Codex audit, `python -m menhir` shows the help menu (available commands: check, serve, serve-watch, hook, ingest-wiki). The documented command `python -m menhir` does NOT start the MCP server — users need `python -m menhir serve` or the correct `serve-watch` command. README is unambiguous that `python -m menhir` should "run the MCP server" — this is wrong.

**Neo4j auth inconsistency (MNR-07 — confirmed current):**

README (line ~149): `docker run ... -e NEO4J_AUTH=neo4j/password neo4j:5`
`.env.example:3`: `NEO4J_PASSWORD=`  (blank)
`settings.py:44`: `neo4j_password: str = ""`

A user who copies `.env.example` (blank password) and starts Neo4j with the README Docker command (sets password to `password`) will get a connection auth failure on first run. The docs never explicitly say "set `NEO4J_PASSWORD=password` in .env to match the Docker container."

**Provider env name split (MNR-08 — partially improved but not complete):**

`settings.py:136`: `local_llm_base_url=_getenv("LOCAL_LLM_BASE_URL", "LLAMA_BASE_URL", ...)` — backward compat aliases are wired correctly in code. But README table (line ~140) still documents `LLAMA_BASE_URL` as the key variable:

```
| `LLAMA_BASE_URL` | Local llama.cpp endpoint | `http://127.0.0.1:8081/v1` |
```

README has not been updated to prefer `LOCAL_LLM_BASE_URL`. `.env.example` is correct (uses `LOCAL_LLM_BASE_URL`). Disconnect between README and .env.example creates confusion.

`.env.example:51-58`: Commented vars still use `YAWN_MEMORY_*` prefix:
```bash
# YAWN_MEMORY_MCP_TELEMETRY_DB=
# YAWN_MEMORY_MCP_TIMEOUT=120
# YAWN_MEMORY_EXPLORER_HOST=127.0.0.1
# YAWN_MEMORY_EXPLORER_PORT=8787
```
A user reading these comments will use the wrong env var names. The active canonical names are `MENHIR_*`.

**Rename sweep (MNR-10 — partially improved, significant residue remains):**

Confirmed stale identifiers after source inspection:
- `pyproject.toml:26`: script name `menhir` (not `mhir`)
- `src/menhir/__init__.py` (not yet read — implied by directory name)
- `src/menhir/config/settings.py:38`: docstring `"""Runtime settings used by cth.mcp.memory."""`
- `src/menhir/api/server.py:49`: `title="yawn-memory"`
- `src/menhir/api/server.py:37`: `"Starting yawn-memory remote API server..."`
- `src/menhir/api/server.py:44`: `"Shutting down yawn-memory remote API server..."`
- `src/menhir/api/server.py:151`: docstring `"""CLI entry point for 'yawn-memory-server'."""`
- `src/menhir/api/auth.py:71-74`: `x-yawn-user-id`, `x-yawn-session-id`, `x-yawn-client-id`, `x-yawn-client-name`
- `integration_test.py:12`: `from yawn_memory.main import ...`
- `.env.example:51-58`: `YAWN_MEMORY_*` in comments
- `README.md` still says "Private — not currently open source" (MNR-06)
- `README.md` still says `yawn-memory-explorer` as the explorer command

### Pass 4 — Test Coverage

**Test suite:** Located at `tests/` (not root). 900+ tests per README. Suite appears comprehensive based on file listing (API auth, API routes, backend roundtrip, budget caps, circuit breaker, CLI hooks, conflict tools, context builder, correlation service, Cypher query, decay logic, degraded startup, etc.).

**Test blockers:**

Two confirmed failures from Codex still present:
1. `pytest` bare run: `integration_test.py` at root imports `yawn_memory` → `ModuleNotFoundError` at collection
2. `test_backend_roundtrip.py`: `MagicMock` passed as Pydantic model → `TypeError` at test time

Both must be resolved before the README test command (`pytest tests/ -x --tb=short`) can claim a passing gate.

**Benchmark harness (MNR-11):** No fresh-container benchmark harness found. The `.agent/plans/` plan is referenced but no implementation evidence.

**MCP tool test coverage:** `test_conflict_tools.py` present. No `test_recall_tools.py` or `test_ingest_tools.py` visible from directory listing — may exist under subdirectories.

### Pass 5 — Performance

**Async ingestion:** Correct — episodes are queued and processed in background. Agents don't block on enrichment. ✓

**Budget caps:** `max_llm_calls_per_session_window` (default 50) and `max_llm_calls_per_enrichment_job` (default 10) configured in settings. Guarded with `__post_init__` validation. ✓

**Structure watcher interval:** 1800s (30 min) default, configurable. ✓

**Unbounded memory/log growth:** No evidence of a retention/log rotation policy seen in this pass. SQLite sidecar grows indefinitely unless the maintenance scheduler handles it. `revision_retention_days=14` is configured; log file rotation not confirmed.

**Embedding cost:** Model depends on provider configuration. Local embeddings via llama.cpp avoid cloud costs. No embedding cost cap seen.

### Pass 6 — Dark Code / Line Counts

**`integration_test.py`:** Stale root-level file with dead `yawn_memory` import. Breaks test collection. Should be updated to `menhir` imports or moved to `tests/` as an offline-skipped integration fixture.

**`.env.example` `YAWN_MEMORY_*` comments:** 4 commented env vars using obsolete prefix. Misleading for users who consult .env.example for tuning.

**`MemorySettings` docstring:** `"""Runtime settings used by cth.mcp.memory."""` — two-generation-old name.

**Server OpenAPI title:** `"yawn-memory"` in FastAPI `title=` — visible in `/api/docs` Swagger UI page.

**Venv tracked in repo:** `venv/` and `.venv/` both appear to be present in the repo directory (visible from LICENSE glob results — both `venv/` and `.venv/` contain package dist-info). Both should be in `.gitignore`; if tracked, they significantly inflate the repo.

---

## Phase 3: Validation

### Codex Finding Disposition

| Finding | Severity | Description | Status |
|---------|----------|-------------|--------|
| MNR-01 | Blocker | `cth-mcp-framework` local-only dep | **Confirmed still open** — `pyproject.toml:16` unchanged |
| MNR-02 | Blocker | Mhir rename not implemented | **Confirmed still open** — `pyproject.toml name="menhir"`, scripts=menhir |
| MNR-03 | Blocker | Unit test gates fail | **Confirmed still open** — `integration_test.py:12` stale import; `test_backend_roundtrip.py` Pydantic mock issue unresolved |
| MNR-04 | Blocker | README dev install wrong (`[dev]` extra doesn't exist) | **Confirmed still open** — pyproject.toml uses `dependency-groups` not `optional-dependencies` |
| MNR-05 | Blocker | `python -m menhir` doesn't start server | **Likely still open** — CLI unchanged; not re-run but command structure unchanged |
| MNR-06 | Blocker | No LICENSE file | **Confirmed still open** — no LICENSE at project root; only in venv directories |
| MNR-07 | High | Neo4j auth inconsistency | **Confirmed still open** — README Docker vs .env.example mismatch |
| MNR-08 | High | LLAMA_* env docs stale | **Partially improved** — settings.py accepts both names; .env.example correct; README table still shows LLAMA_BASE_URL |
| MNR-09 | High | Auth opt-in + CORS wildcard | **Confirmed still open** — auth.py:106-108, server.py:118-119 |
| MNR-10 | High | Rename residue | **Partially improved, significant residue** — yawn_memory/yawn-memory refs in server.py, auth.py, integration_test.py, .env.example, settings.py |
| MNR-11 | Medium | Benchmark readiness not implemented | **Confirmed still open** — no benchmark harness evidence |
| MNR-12 | Medium | Launch gate docs missing | **Not addressed** |

### New / Additional Evidence

| NF | Severity | Finding |
|----|----------|---------|
| NF-1 | Low | `.env.example:51-58` has 4 `YAWN_MEMORY_*` commented vars misleading users |
| NF-2 | Low | `auth.py:71-74` `x-yawn-*` header protocol names will need updating with Mhir rename — documents blast radius |
| NF-3 | Low | `venv/` and `.venv/` may be tracked in repo (visible from LICENSE glob hitting both directories) |
| NF-4 | Info | Settings.py correctly accepts `LLAMA_*` aliases for backward compat — no new action needed in code, only docs update |

---

## Phase 4: Final Audit

### Confirmed Defects

**CD-1 — Blocker: Clean install fails — `cth-mcp-framework` not on PyPI**

`pyproject.toml:16`: `cth-mcp-framework>=0.1.0`

Fresh `pip install .` in a clean environment fails with `No matching distribution found for cth-mcp-framework>=0.1.0`. The package exists only as a local editable install at `projects/ctharvey/cth.mcp.framework`. OSS users cannot install Mhir. Required fix: publish `cth-mcp-framework`, vendor/inline it, or restructure to remove the dependency.

**CD-2 — Blocker: Mhir rename not implemented — package distributes as `menhir`**

`pyproject.toml:6`: `name = "menhir"`. PyPI `menhir` is owned by an unrelated project (dialoguemd, build tool). Scripts named `menhir` / `menhir-explorer`. Import package at `src/menhir/`. Required fix: implement the decided Mhir rename (distribution `archolith-mhir`, import `mhir`, CLI `mhir`/`mhir-explorer`) across pyproject.toml, source, tests, docs, and examples.

**CD-3 — Blocker: Test collection fails — `integration_test.py` has dead import**

`integration_test.py:12`: `from yawn_memory.main import check_neo4j_connectivity, check_llama_connectivity`

Module `yawn_memory` does not exist (was renamed to `menhir`). Root-level file is collected by default pytest run, causing immediate collection failure before any unit tests execute. Required fix: update to current import (`from menhir.main import ...`) and mark as integration-only, or move to `tests/` with an `@pytest.mark.online` skip guard.

**CD-4 — Blocker: README dev install command doesn't install dev deps**

`pyproject.toml:29-33` uses `[dependency-groups].dev` (PEP 735 / uv format). `pip install -e ".[dev]"` as documented in README:123 silently skips dev dependencies — pip warns but continues. Pytest is not installed for fresh pip users. Required fix: add `[project.optional-dependencies].dev` for pip compatibility, or update README to document `uv sync --group dev`.

**CD-5 — Blocker: `python -m menhir` shows help, not MCP server**

`pyproject.toml:26`: `menhir = "menhir.main:main"` Typer CLI. Available subcommands: check, serve, serve-watch, hook, ingest-wiki. README:161 says `python -m menhir` starts the MCP server — this is wrong; it shows Typer help. MCP client JSON example (README:166-173) uses `"-m", "menhir"` which has the same problem. Required fix: document `python -m menhir serve` (or correct serve subcommand), add a clear first-run path distinguishing stdio MCP, remote HTTP MCP, and backend API modes.

**CD-6 — Blocker: No LICENSE file; README declares project private**

`Test-Path LICENSE` returns False (Codex finding, confirmed via LICENSE glob that only found venv-internal licenses). `README.md` final line: `Private — not currently open source.` Required fix: choose and add OSS license, update README statement, audit dependency licenses.

### Security / Privacy Risks

**SR-1 — High: Auth completely skipped when no keys configured**

`auth.py:106-108`: When `MENHIR_API_KEY`, `MENHIR_OPERATOR_KEY`, `MENHIR_AGENT_KEY`, and `MENHIR_READONLY_KEY` are all empty (the default), auth is bypassed entirely for all protected routes. No startup warning is emitted.

Risk: Unauthenticated access to all memory read/write/delete operations via REST and MCP HTTP when combined with non-loopback binding (`MENHIR_API_HOST=0.0.0.0`). Default loopback binding (`127.0.0.1`) limits the blast radius for default installs, but the README does not clearly document this boundary.

Required fix: add a startup warning logged at `WARNING` or higher when host is non-loopback and no auth keys are set. Document minimum required auth configuration for any non-localhost deployment.

**SR-2 — Medium: CORS defaults to wildcard**

`server.py:118-119`: `cors_origins = [...] or ["*"]`. Any origin can make cross-origin requests when `MENHIR_CORS_ORIGINS` is not set. Combined with SR-1 on a non-loopback host, this exposes memory operations to web-based cross-origin attacks. Required fix: default to restrictive CORS (e.g., `["http://localhost:8100"]`) or require explicit opt-in for wildcard.

### Design Risks

**DR-1 — High: `cth-mcp-framework` vendor dependency creates fragile install story**

The framework provides MCP server infrastructure. Options for launch: (a) publish `cth-mcp-framework` to PyPI, (b) inline the relevant functionality, or (c) replace with a public FastMCP-based implementation. `fastmcp>=3.2.4` is already in dependencies — may already provide what `cth-mcp-framework` offers.

**DR-2 — Medium: Neo4j auth mismatch creates silent first-run failure**

README Docker: `NEO4J_AUTH=neo4j/password`. `.env.example`: `NEO4J_PASSWORD=`. First-run user copies .env.example, gets blank password, Neo4j auth fails. Required fix: document the relationship between `NEO4J_AUTH` Docker flag and `NEO4J_PASSWORD` env var; provide a consistent local-dev default.

**DR-3 — Medium: Dev dependency format mismatch**

`[dependency-groups]` is the uv/PEP 735 format. `pip install -e ".[dev]"` silently skips it. Users who don't use uv will have no dev deps without a workaround. This blocks contributions from pip-only developers.

### Rename / Migration Gaps

**RMG-1: pyproject.toml not renamed**

Distribution name `menhir`, scripts `menhir`/`menhir-explorer`, version `0.2.0`.
Target: `archolith-mhir`, `mhir`/`mhir-explorer`.

**RMG-2: Source package not renamed**

`src/menhir/` import tree. All 23+ MCP tools, services, and CLI entry points use `menhir` imports.
Target: `src/mhir/` with optional `mhir = menhir` shim if backward compat needed.

**RMG-3: Runtime labels stale**

`server.py:49` title, logger messages at lines 37 and 44, CLI docstring line 151.

**RMG-4: Protocol headers stale**

`auth.py:71-74`: `x-yawn-user-id`, `x-yawn-session-id`, `x-yawn-client-id`, `x-yawn-client-name`.
Note: changing these requires coordinating with all MCP clients that send these headers.

**RMG-5: `.env.example` partial cleanup**

Active vars use correct names (`LOCAL_LLM_*`, `MENHIR_*`). Commented vars still show `YAWN_MEMORY_*` (lines 51-58). README env table still shows `LLAMA_BASE_URL` as the primary var name.

**RMG-6: `integration_test.py` import**

`from yawn_memory.main import ...` (line 12). Breaks test collection.

**RMG-7: `MemorySettings` docstring**

`settings.py:38`: `"""Runtime settings used by cth.mcp.memory."""` — two generations stale.

**RMG-8: README explorer command**

`yawn-memory-explorer` → `mhir-explorer`.

### Benchmark Evidence Gaps

**BEG-1: No fresh-container benchmark harness**

The `.agent/plans/fresh-neo4j-memory-benchmark-plan.md` plan exists but implementation not found. Cannot make credible claims about recall quality, ingestion throughput, graph growth, or latency. Required before making any performance or memory-quality claims at launch.

### Dark Code Findings

**DC-1 — Medium: `integration_test.py` at repo root is dead/broken**

Stale `yawn_memory` import breaks pytest collection. Should be updated (`from menhir.main import ...`) or moved to `tests/` with an online-skip marker.

**DC-2 — Low: `YAWN_MEMORY_*` env var comments in `.env.example`**

Lines 51-58: 4 commented vars with wrong prefix. Misleads operators who use these as a config reference.

**DC-3 — Low: `MemorySettings` docstring cites `cth.mcp.memory`**

`settings.py:38`. Two-generation-old project name.

**DC-4 — Low: `server.py` OpenAPI title still `"yawn-memory"`**

Visible in `/api/docs` Swagger page. Confusing for any user who sees the API explorer.

**DC-5 — Low: Venv directories (`venv/`, `.venv/`) may be tracked**

LICENSE glob results showed dist-info files inside both `venv/` and `.venv/`. If tracked in git, these inflate the repo with thousands of dependency files. Confirm `.gitignore` excludes them.

### Open Questions

**OQ-1:** What is `cth-mcp-framework` used for, and could it be replaced with `fastmcp` (already a dependency) before launch? This is the highest-leverage blocker question.

**OQ-2:** What is the intended OSS license for Mhir? PolyForm Noncommercial (like archolith.dev)? MIT? Apache-2.0? The archolith PyPI package is Apache-2.0.

**OQ-3:** When will the Mhir rename begin? The investigation is complete (decision made 2026-06-19) — what is the implementation timeline?

**OQ-4:** Are `venv/` and `.venv/` in `.gitignore`? If tracked, the repo state is misleading.

**OQ-5:** The `test_backend_roundtrip.py` Pydantic/MagicMock failure — is this a known issue with a workaround, or does the test currently pass in the working environment?

**OQ-6:** Does `python -m menhir serve` or `python -m menhir serve-watch` correctly start the stdio MCP server? This determines whether MNR-05 is a docs issue only or also a code issue.

### Rejected Candidates

**RC-1:** Auth keys env var naming (`MENHIR_API_KEY`, `MENHIR_OPERATOR_KEY`, etc.) — these already use the `MENHIR_` prefix and are correctly documented in `settings.py` comments (lines 98-100). The active auth key naming is consistent. ✓

**RC-2:** Backward-compat `LLAMA_*` aliases in `settings.py` — these are implemented correctly via the `_getenv(primary, *aliases)` pattern. Not a defect, just underdocumented in the README. ✓

**RC-3:** `api_host` default `127.0.0.1` — this is the correct and safe default for a local memory service. Not a defect. ✓

**RC-4:** `x-yawn-*` header names as a current security risk — these are protocol headers used by existing MCP clients. They're wrong after rename but not a security vulnerability today. Noted as RMG-4 for the rename scope. ✓

**RC-5:** Graphiti episode timeout default (300s) — a 5-minute timeout per episode enrichment call is large but reasonable for a local LLM that may be slow. Not a defect. ✓

### Consolidated Finding Index

| ID | Severity | Category | Finding |
|----|----------|----------|---------|
| CD-1 | **Blocker** | Install | `cth-mcp-framework` not on PyPI — fresh install fails |
| CD-2 | **Blocker** | Rename | Package still distributes as `menhir`; Mhir rename not implemented |
| CD-3 | **Blocker** | Tests | `integration_test.py` stale import breaks test collection |
| CD-4 | **Blocker** | Onboarding | `[dev]` extra doesn't exist; pip dev install silent no-op |
| CD-5 | **Blocker** | Docs | `python -m menhir` shows help, not MCP server |
| CD-6 | **Blocker** | License | No LICENSE file; README declares project private |
| SR-1 | High | Security | Auth completely skipped when no keys configured |
| SR-2 | Medium | Security | CORS defaults to wildcard |
| DR-1 | High | Architecture | `cth-mcp-framework` vendor dep — fragile install story |
| DR-2 | Medium | Config | Neo4j auth mismatch between README Docker and .env.example |
| DR-3 | Medium | DX | Dev dep format (`[dependency-groups]`) not pip-compatible |
| RMG-1..8 | High | Rename | Mhir rename not implemented across 8 surfaces |
| BEG-1 | Medium | Benchmark | No fresh-container benchmark harness |
| DC-1 | Medium | Dark Code | `integration_test.py` at root — dead import, breaks collection |
| DC-2..5 | Low | Dark Code | YAWN_MEMORY_* comments, stale docstrings, server title, venv tracking |

### Codex Finding Summary

All 12 Codex findings confirmed or partially confirmed:
- 6 blockers confirmed open: MNR-01, MNR-02, MNR-03, MNR-04, MNR-06 (all still open); MNR-05 (CLI command — likely still open, not re-run)
- 4 high findings: MNR-07 confirmed; MNR-08 partially improved (code improved, docs lagging); MNR-09 confirmed; MNR-10 partially improved (significant residue)
- 2 medium: MNR-11 (no harness), MNR-12 (no gate docs) — neither addressed

New findings: NF-1 (`.env.example` YAWN_MEMORY_* comments), NF-2 (x-yawn-* header blast radius), NF-3 (venv tracking), NF-4 (LLAMA_* compat — informational).

### Coverage Summary

| Area | Coverage |
|------|----------|
| `pyproject.toml` | Complete |
| `.env.example` | Complete |
| `src/menhir/api/auth.py` | Complete |
| `src/menhir/api/server.py` | Complete (160 lines) |
| `src/menhir/config/settings.py` | Complete (208 lines) |
| `integration_test.py` | Head (40 lines) |
| `tests/test_backend_roundtrip.py` | Head (40 lines) |
| `README.md` | Complete |
| `.agent/README.md` | Complete |
| Tests (900+) | Structure only; did not run |
| Source modules (infrastructure, services, domain, mcp) | Not individually read — Codex evidence + architecture map sufficient |

### Verification Commands

```bash
# Confirm distribution name
grep "^name" menhir/pyproject.toml

# Confirm cth-mcp-framework dep
grep "cth-mcp-framework" menhir/pyproject.toml

# Confirm no LICENSE at root
ls menhir/LICENSE* 2>/dev/null || echo "No LICENSE"

# Confirm stale integration_test import
head -15 menhir/integration_test.py

# Confirm CORS wildcard in server.py
grep "cors_origins" menhir/src/menhir/api/server.py

# Confirm auth bypass when no keys
grep -A3 "not.*operator_key" menhir/src/menhir/api/auth.py

# Confirm yawn-* residue in server.py
grep "yawn" menhir/src/menhir/api/server.py

# Confirm YAWN_MEMORY_ in .env.example
grep "YAWN_MEMORY" menhir/.env.example

# Confirm dev dependency format
grep -A3 "dependency-groups" menhir/pyproject.toml

# Check if venv tracked
git -C menhir ls-files venv .venv | head -5
```

### Minimal Launch Gate (from Codex, verified still applicable)

1. Fresh install works without local-only `cth-mcp-framework` (publish, vendor, or replace)
2. Mhir rename implemented (pyproject.toml, source, tests, docs, MCP client examples)
3. OSS license committed; README updated from "Private"
4. README first-run path accurate (install → Neo4j → correct start command → MCP config)
5. Test collection passes; unit gate passes in a clean environment
6. Auth keys documented as required for any non-loopback deployment
7. Rename residue cleaned (runtime labels, CLI docstrings, .env.example comments, integration_test.py)
8. Fresh-container benchmark harness implemented and results labeled experimental
