# Cross-cutting + Scripts + Docs + Config Audit

**Date:** 2026-06-06
**Auditor:** OpenCode (z-ai/glm-5.1)
**Scope:** Project root files (pyproject.toml, docker-compose.yml, .env.example, pytest.ini, .gitignore), scripts/ (7 files), integration_test.py, smoke_test.py, .agent/ docs (README.md, maintenance.md, file-index.md, verified-current-findings.md, memory-design.md, memory-foundations.md), dotfiles (CLAUDE.md, AGENTS.md, QWEN.md, gemini.md, .cursorrules, .clinerules, .windsurfrules, copilot-instructions.md), cross-cutting naming sweep across entire project
**Chunk:** 6 of the cth.mcp.memory codebase audit

---

## Summary

The project root configuration is largely clean — `pyproject.toml`, `pytest.ini`, and `.gitignore` are well-maintained and use current naming. The main problems cluster around four areas: (1) a dead import in `integration_test.py` that will crash on execution, (2) hardcoded Windows paths in a shell script and VBS launcher, (3) the `yawn-neo4j` container name in docker-compose that doesn't match the `cth-mcp-memory` identity, and (4) the pervasive `yawn_memory`/`yawn-memory`/`YAWN_MEMORY` naming residue that spans 18 Python files (39 matches) and 2 non-Python files (19 matches). The `.env.example` still documents 4 `YAWN_MEMORY_*` env var names. Scripts are functional but carry portability and credential-exposure issues.

**Total findings:** 15 — 1 CRITICAL, 3 HIGH, 6 MEDIUM, 5 LOW

---

## Findings

### X-01 — Dead import `from yawn_memory.main` in integration_test.py will crash

| Field | Value |
|-------|-------|
| **ID** | X-01 |
| **Severity** | CRITICAL |
| **File** | `integration_test.py:12` |
| **Category** | Broken import / dead code |
| **Description** | The file imports `from yawn_memory.main import check_neo4j_connectivity, check_llama_connectivity` but the package was renamed to `cth_mcp_memory`. This import will raise `ModuleNotFoundError` on any run, making the entire integration test non-functional. |
| **Fix** | Replace `from yawn_memory.main` with `from cth_mcp_memory.main`. Verify the function signatures still match. |
| **Test coverage** | Not covered by pytest — integration_test.py is run standalone. |

---

### X-02 — Stale env var names in integration_test.py

| Field | Value |
|-------|-------|
| **ID** | X-02 |
| **Severity** | HIGH |
| **File** | `integration_test.py:23-29` |
| **Category** | Configuration drift |
| **Description** | The integration test reads `LLAMA_BASE_URL`, `LLAMA_API_KEY`, `LLAMA_CHAT_MODEL`, `LLAMA_EMBED_MODEL` with `LM_STUDIO_*` fallbacks. The current codebase uses `LOCAL_LLM_BASE_URL`, `LOCAL_LLM_API_KEY`, `LOCAL_LLM_CHAT_MODEL`, `LOCAL_LLM_EMBED_MODEL` (set via `MemorySettings`). The `LLAMA_*` names still work as legacy aliases in settings.py but `LM_STUDIO_*` does not. The fallback chain is wrong. |
| **Fix** | Update env var names to `LOCAL_LLM_*` variants. Remove `LM_STUDIO_*` fallbacks. Use `MemorySettings.from_env()` instead of manual `os.getenv()` chains. |
| **Test coverage** | N/A — standalone script. |

---

### X-03 — Hardcoded Windows venv path in start-server.sh

| Field | Value |
|-------|-------|
| **ID** | X-03 |
| **Severity** | HIGH |
| **File** | `scripts/start-server.sh:8` |
| **Category** | Portability |
| **Description** | The bash script sets `VENV_PYTHON="$PROJECT_DIR/.venv/Scripts/python.exe"` — a Windows-specific path. On Linux/macOS the venv Python is at `.venv/bin/python`. This script will fail on any non-Windows system, which defeats the purpose of a `.sh` launcher. |
| **Fix** | Add platform detection: `if [ -f "$PROJECT_DIR/.venv/bin/python" ]; then VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"; else VENV_PYTHON="$PROJECT_DIR/.venv/Scripts/python.exe"; fi`. |
| **Test coverage** | Not tested. |

---

### X-04 — Hardcoded absolute path in run-hidden.vbs

| Field | Value |
|-------|-------|
| **ID** | X-04 |
| **Severity** | HIGH |
| **File** | `scripts/run-hidden.vbs:2` |
| **Category** | Portability / hardcoded path |
| **Description** | The VBS file hardcodes `C:\Users\you\IdeaProjects\projects\ctharvey\cth.mcp.memory\scripts\start-server.ps1`. This is developer-machine-specific. The `install-task` action in `start-server.ps1` actually regenerates this VBS dynamically (lines 308-310), making the committed VBS redundant and misleading. |
| **Fix** | Remove `run-hidden.vbs` from version control. Add it to `.gitignore`. Let `install-task` generate it at runtime. |
| **Test coverage** | Not tested. |

---

### X-05 — `yawn-neo4j` container name in docker-compose.yml

| Field | Value |
|-------|-------|
| **ID** | X-05 |
| **Severity** | MEDIUM |
| **File** | `docker-compose.yml:4`; `scripts/start-server.ps1:27` |
| **Category** | Naming residue |
| **Description** | The docker-compose service uses `container_name: yawn-neo4j`. The PowerShell launcher references the same `$neo4jContainer = "yawn-neo4j"`. Both should align with the `cth-mcp-memory` identity. Renaming the container requires stopping and recreating it. |
| **Fix** | Change to `cth-mcp-memory-neo4j`. Update `start-server.ps1` `$neo4jContainer`. Add migration note: `docker stop yawn-neo4j && docker rm yawn-neo4j` before `docker compose up -d`. |
| **Test coverage** | Not tested. |

---

### X-06 — Hardcoded Neo4j credentials in profile_recall.py

| Field | Value |
|-------|-------|
| **ID** | X-06 |
| **Severity** | MEDIUM |
| **File** | `scripts/profile_recall.py:12-13` |
| **Category** | Security / credential exposure |
| **Description** | `AUTH = ("neo4j", "password")` is hardcoded. While this is a dev/profiling script, it normalizes bad practice and will fail against any non-default Neo4j instance. |
| **Fix** | Read from env vars or `MemorySettings`: `AUTH = (os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "password"))`. Add `load_dotenv()` call. |
| **Test coverage** | Not tested. |

---

### X-07 — `YAWN_MEMORY_*` env var names in .env.example

| Field | Value |
|-------|-------|
| **ID** | X-07 |
| **Severity** | MEDIUM |
| **File** | `.env.example:51-57` |
| **Category** | Naming residue |
| **Description** | Four commented-out env var names use the `YAWN_MEMORY_` prefix: `YAWN_MEMORY_MCP_TELEMETRY_DB`, `YAWN_MEMORY_MCP_TIMEOUT`, `YAWN_MEMORY_EXPLORER_HOST`, `YAWN_MEMORY_EXPLORER_PORT`. The current codebase uses `CTH_MCP_MEMORY_*` prefix. |
| **Fix** | Replace `YAWN_MEMORY_*` with `CTH_MCP_MEMORY_*` (uppercase, consistent with env var convention). |
| **Test coverage** | N/A. |

---

### X-08 — integration_test.py hardcodes "yawn.rip" in validation text

| Field | Value |
|-------|-------|
| **ID** | X-08 |
| **Severity** | MEDIUM |
| **File** | `integration_test.py:34` |
| **Category** | Naming residue / domain coupling |
| **Description** | `VALIDATION_EPISODE = "The yawn.rip project is a Spring Boot REST API for Pokemon card pricing."` and the validation function at line 44 checks for "yawn.rip" in results. This is semantically tied to a specific project. |
| **Fix** | Change to: `"The cth.mcp.memory project is a graph-based long-term memory system for AI agents."` Update `_fact_has_expected_terms` to check for "cth.mcp.memory" / "memory" / "graph" / "agents". |
| **Test coverage** | N/A — standalone script. |

---

### X-09 — Pervasive `yawn_memory`/`yawn-memory` naming residue across 18 Python files

| Field | Value |
|-------|-------|
| **ID** | X-09 |
| **Severity** | MEDIUM |
| **File** | 18 Python files (39 matches), 2 non-Python files (19 matches) |
| **Category** | Naming residue (cross-cutting) |
| **Description** | The project was renamed from `yawn_memory`/`yawn-memory` to `cth_mcp_memory`/`cth-mcp-memory`, but many references remain. This is the single largest cross-cutting migration item. Python files: `main.py`, `cli/_backend_context.py`, `cli/hook.py` (9 matches), `cli/bootstrap.py`, `infrastructure/llama_endpoint.py`, `services/maintenance_scheduler.py`, `api/server.py` (4), `services/ingest_service.py`, `api/mcp_remote.py`, `mcp/server.py`, `mcp/resources.py`, `core/backend_impl.py`, `core/backend_protocol.py`, `core/runtime.py` (4), `tests/test_cli_hook.py`, `tests/test_mcp_remote.py`, `tests/test_mcp_server.py` (3). Non-Python: `CHANGELOG.md` (15), `README.md` (4). |
| **Fix** | Systematic find-and-replace across all files. The `x-yawn-*` HTTP headers are wire-protocol and require a versioned migration (see X-10). |
| **Test coverage** | Existing tests assert old names — must be updated. |

---

### X-10 — `x-yawn-*` HTTP headers are wire-protocol, require versioned migration

| Field | Value |
|-------|-------|
| **ID** | X-10 |
| **Severity** | MEDIUM |
| **File** | `api/auth.py:55-58`; `api/routes.py:82,84,138`; `core/backend_impl.py:105,107,108,112,115`; `tests/test_backend_roundtrip.py` |
| **Category** | Wire-protocol naming |
| **Description** | HTTP headers `x-yawn-session-id`, `x-yawn-user-id`, `x-yawn-client-id`, `x-yawn-client-name`, `x-yawn-bg-warnings` are part of the API contract. Renaming these breaks backward compatibility with any deployed client. |
| **Fix** | Accept both old and new headers during a transition period. Add `x-cth-mcp-memory-*` equivalents as canonical. Log deprecation warning when old names are used. Remove old-name support after a versioned cutoff. |
| **Test coverage** | `test_backend_roundtrip.py` tests old header names — add parallel tests for new names. |

---

### X-11 — Docker Compose lacks resource limits

| Field | Value |
|-------|-------|
| **ID** | X-11 |
| **Severity** | LOW |
| **File** | `docker-compose.yml` |
| **Category** | Operational hardening |
| **Description** | The Neo4j service has no `mem_limit`, `cpus`, or `restart: unless-stopped` policy. In a long-running dev environment, Neo4j can consume unbounded resources or silently stop without recovery. |
| **Fix** | Add `restart: unless-stopped` and consider `mem_limit: 2g`. |
| **Test coverage** | N/A. |

---

### X-12 — integration_test.py uses raw Graphiti API instead of project service layer

| Field | Value |
|-------|-------|
| **ID** | X-12 |
| **Severity** | LOW |
| **File** | `integration_test.py:62-82` |
| **Category** | Test architecture |
| **Description** | The integration test manually constructs a `Graphiti` client, LLM client, and embedder instead of using the project's `build_memory_services()` or `MemorySettings`. This means the test doesn't validate the actual service wiring and drifts when settings change (as seen in X-02). |
| **Fix** | Refactor to use `build_memory_services()` and `MemorySettings` for construction. |
| **Test coverage** | N/A — the test itself. |

---

### X-13 — smoke_test.py and integration_test.py are not in pytest tree

| Field | Value |
|-------|-------|
| **ID** | X-13 |
| **Severity** | LOW |
| **File** | `smoke_test.py`, `integration_test.py` (project root) |
| **Category** | Test organization |
| **Description** | Both scripts sit at the project root instead of under `tests/`. The `cth-mcp-memory.ps1` script references them by relative path. They aren't picked up by pytest discovery. |
| **Fix** | Move to `tests/smoke_test.py` and `tests/integration_test.py`. Update `cth-mcp-memory.ps1` paths. Or convert to pytest fixtures with `@pytest.mark.online` markers. |
| **Test coverage** | N/A. |

---

### X-14 — .env.example documents `LLAMA_BASE_URL` alongside `LOCAL_LLM_BASE_URL`; README out of sync

| Field | Value |
|-------|-------|
| **ID** | X-14 |
| **Severity** | LOW |
| **File** | `.env.example`; `README.md:141` |
| **Category** | Configuration clarity |
| **Description** | The `.env.example` uses `LOCAL_LLM_*` names (current) but the `README.md` still references `LLAMA_BASE_URL` (line 141) and `SCHEDULER_URL` pointing to "yawn.scheduler". The README is out of sync with `.env.example`. |
| **Fix** | Update README.md env var table to match `.env.example` names. Remove `LLAMA_BASE_URL` reference. |
| **Test coverage** | N/A. |

---

### X-15 — README.md references `python -m yawn_memory` and `yawn-memory-explorer`

| Field | Value |
|-------|-------|
| **ID** | X-15 |
| **Severity** | LOW |
| **File** | `README.md:160-174,181` |
| **Category** | Naming residue / documentation |
| **Description** | The README still shows `python -m yawn_memory` as the run command, `yawn_memory` in the MCP client config, and `yawn-memory-explorer` as the explorer CLI entry point. The actual entry points are `python -m cth_mcp_memory` and `cth-mcp-memory-explorer`. |
| **Fix** | Update all references to `cth_mcp_memory` / `cth-mcp-memory-explorer`. |
| **Test coverage** | N/A. |

---

## Cross-chunk naming summary

| Pattern | Python files | Python matches | Non-Py files | Non-Py matches | Total |
|---------|-------------|----------------|-------------|----------------|-------|
| `yawn_memory` / `yawn-memory` / `YAWN_MEMORY` / `yawn.memory` | 18 | 39 | 2 | 19 | 58 |
| `x-yawn-*` (HTTP headers) | 4 | 16 | 0 | 0 | 16 |
| `yawn-neo4j` (container name) | 1 (ps1) | 2 | 1 (yml) | 1 | 3 |
| `yawn.rip` (in test text) | 1 | 2 | 0 | 0 | 2 |

Total naming residue: **79 occurrences** across **22 files**.

---

## Files audited

| Category | Files |
|----------|-------|
| Root config | `pyproject.toml`, `docker-compose.yml`, `.env.example`, `pytest.ini`, `.gitignore` |
| Scripts | `start-server.sh`, `start-server.ps1`, `cth-mcp-memory.ps1`, `run-hidden.vbs`, `profile_recall.py`, `repair_embedding_dimensions.py`, `run_mcp_gateway.py` |
| Root test scripts | `smoke_test.py`, `integration_test.py` |
| .agent/ docs | `README.md`, `maintenance.md`, `file-index.md`, `verified-current-findings.md`, `memory-design.md`, `memory-foundations.md` |
| Dotfiles | `CLAUDE.md`, `AGENTS.md` |

---

## Out of scope

- `CHANGELOG.md` — naming residue noted but changelog is historical; only new entries need updated names
- `.agent/` heavy reference docs (`architecture.md`, `data_models.md`, `endpoints.md`, `memory-roadmap.md`, `memory-policy.md`, etc.) — naming residue tracked but lower priority than code
- `QWEN.md`, `gemini.md`, `.cursorrules`, `.clinerules`, `.windsurfrules`, `copilot-instructions.md` — checked and clean
