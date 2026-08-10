# Remediation Plan: cth.mcp.memory Chunk 6 — Cross-cutting + Scripts + Docs + Config

**Date:** 2026-06-06
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** Project root config, scripts/, integration_test.py, smoke_test.py, .env.example, README.md, docker-compose.yml, cross-cutting naming residue

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH | 3 |
| MEDIUM | 6 |
| LOW | 5 |
| **Total** | **15** |

---

## Findings Inventory

### CRITICAL

| ID | File:Line | Description |
|----|-----------|-------------|
| X-01 | `integration_test.py:12` | Dead import `from yawn_memory.main` — will crash on any run |

### HIGH

| ID | File:Line | Description |
|----|-----------|-------------|
| X-02 | `integration_test.py:23-29` | Stale env var names (`LLAMA_*`/`LM_STUDIO_*`) instead of current `LOCAL_LLM_*` |
| X-03 | `scripts/start-server.sh:8` | Hardcoded Windows venv path `.venv/Scripts/python.exe` |
| X-04 | `scripts/run-hidden.vbs:2` | Hardcoded absolute developer path; redundant with `install-task` |

### MEDIUM

| ID | File:Line | Description |
|----|-----------|-------------|
| X-05 | `docker-compose.yml:4`; `start-server.ps1:27` | `yawn-neo4j` container name |
| X-06 | `scripts/profile_recall.py:12-13` | Hardcoded Neo4j credentials `("neo4j", "password")` |
| X-07 | `.env.example:51-57` | 4 `YAWN_MEMORY_*` env var names |
| X-08 | `integration_test.py:34` | Hardcoded "yawn.rip" in validation episode text |
| X-09 | 18 Python + 2 non-Py files | Pervasive `yawn_memory`/`yawn-memory` naming residue (39+19 matches) |
| X-10 | `api/auth.py`, `routes.py`, `backend_impl.py` | `x-yawn-*` HTTP headers — wire-protocol, need versioned migration |

### LOW

| ID | File:Line | Description |
|----|-----------|-------------|
| X-11 | `docker-compose.yml` | No resource limits or restart policy |
| X-12 | `integration_test.py:62-82` | Uses raw Graphiti API instead of project service layer |
| X-13 | `smoke_test.py`, `integration_test.py` | Not in pytest tree; root-level standalone scripts |
| X-14 | `.env.example`; `README.md:141` | README env var table out of sync with `.env.example` |
| X-15 | `README.md:160-174,181` | README references `python -m yawn_memory` and `yawn-memory-explorer` |

---

## Phase Plan

### Phase 1: Fix Broken Integration Test (CRITICAL — X-01, HIGH — X-02, MEDIUM — X-08)

**Goal:** Make `integration_test.py` runnable again.

| # | Task | Files | Finding |
|---|------|-------|---------|
| 1.1 | Replace `from yawn_memory.main` with `from cth_mcp_memory.main` | `integration_test.py:12` | X-01 |
| 1.2 | Replace `LLAMA_*`/`LM_STUDIO_*` env var reads with `LOCAL_LLM_*`; use `MemorySettings.from_env()` | `integration_test.py:23-29` | X-02 |
| 1.3 | Replace `check_llama_connectivity()` call with current function name (if renamed) | `integration_test.py:52-58` | X-02 |
| 1.4 | Replace `VALIDATION_EPISODE` text and `_fact_has_expected_terms` tokens | `integration_test.py:34,44` | X-08 |
| 1.5 | Run `python integration_test.py` to verify the fix works | `integration_test.py` | X-01 |

### Phase 2: Script Portability (HIGH — X-03, X-04, MEDIUM — X-06)

**Goal:** Scripts work on both Windows and Linux; no developer-machine-specific paths.

| # | Task | Files | Finding |
|---|------|-------|---------|
| 2.1 | Add platform detection to `start-server.sh`: try `.venv/bin/python` first, fall back to `.venv/Scripts/python.exe` | `scripts/start-server.sh:8` | X-03 |
| 2.2 | Add `run-hidden.vbs` to `.gitignore`; remove from version control | `.gitignore`, `scripts/run-hidden.vbs` | X-04 |
| 2.3 | Update `start-server.ps1:install-task` to always regenerate VBS at runtime (it already does — verify no other references) | `scripts/start-server.ps1:308-310` | X-04 |
| 2.4 | Replace hardcoded credentials in `profile_recall.py` with env var reads and `load_dotenv()` | `scripts/profile_recall.py:12-13` | X-06 |

### Phase 3: Immediate Naming Fix — `yawn_*` → `cth_mcp_memory` (MEDIUM — X-05, X-07, X-09, X-10)

**Goal:** Replace all `yawn_*` / `yawn-*` / `YAWN_*` naming residue with the current package identity `cth_mcp_memory`. This is the correctness fix — the old names are wrong today. The planned `archolith-memory` rename is a separate, later phase (Phase 7).

| # | Task | Files | Finding |
|---|------|-------|---------|
| 3.1 | Replace all `yawn_memory`/`yawn-memory`/`YAWN_MEMORY`/`yawn.memory` in 18 Python files with `cth_mcp_memory`/`cth-mcp-memory`/`CTH_MCP_MEMORY`/`cth.mcp.memory` | All affected source + test files | X-09 |
| 3.2 | Update README.md: `python -m yawn_memory` → `python -m cth_mcp_memory`; MCP client config; `yawn-memory-explorer` → `cth-mcp-memory-explorer` | `README.md:160-174,181` | X-15 |
| 3.3 | Update `.env.example`: `YAWN_MEMORY_*` → `CTH_MCP_MEMORY_*` | `.env.example:51-57` | X-07 |
| 3.4 | Update `README.md` env var table to match `.env.example` (remove `LLAMA_BASE_URL`) | `README.md:141` | X-14 |
| 3.5 | Rename docker-compose container `yawn-neo4j` → `cth-mcp-memory-neo4j`; update `$neo4jContainer` in `start-server.ps1` | `docker-compose.yml:4`, `start-server.ps1:27` | X-05 |
| 3.6 | Add migration note for existing Docker users: `docker stop yawn-neo4j && docker rm yawn-neo4j` | `docker-compose.yml` comment | X-05 |
| 3.7 | Accept both `x-yawn-*` and `x-cth-mcp-memory-*` header prefixes in `auth.py`; route `x-cth-mcp-memory-*` as canonical | `api/auth.py:55-58` | X-10 |
| 3.8 | Emit `x-cth-mcp-memory-*` response headers; accept `x-yawn-*` as deprecated alias in `routes.py` | `api/routes.py:82,84,138` | X-10 |
| 3.9 | Update `core/backend_impl.py` to send `x-cth-mcp-memory-*` headers (with `x-yawn-*` as fallback during transition) | `core/backend_impl.py:105-115` | X-10 |
| 3.10 | Add deprecation warning log when `x-yawn-*` headers are received | `api/auth.py` | X-10 |
| 3.11 | Add test: verify both old and new header prefixes work | `tests/test_backend_roundtrip.py` | X-10 |
| 3.12 | Document header migration timeline in `endpoints.md` | `.agent/endpoints.md` | X-10 |

### Phase 7: Project Rename — `cth_mcp_memory` → `archolith_memory` (planned, separate effort)

**Goal:** When the project is renamed to `archolith-memory`, all `cth_mcp_memory` / `cth-mcp-memory` / `CTH_MCP_MEMORY` identifiers get replaced with their `archolith` equivalents. This phase is documented here for planning continuity but should only be executed as part of a coordinated workspace-wide rename.

**Relationship to Phase 3:** Phase 3 fixes the *incorrect* `yawn_*` names to the *current* `cth_mcp_memory` names. Phase 7 replaces the *current* names with the *final* `archolith_memory` names. Doing both in one pass would skip the intermediate name, but the `cth_mcp_memory` package name is what's in `pyproject.toml`, installed in venvs, and referenced by MCP clients today — so the two-step approach is safer.

| # | Task | Files | Notes |
|---|------|-------|-------|
| 7.1 | Rename Python package: `cth_mcp_memory` → `archolith_memory`; update all imports | All `src/cth_mcp_memory/` → `src/archolith_memory/` | Requires `pyproject.toml` update, reinstall |
| 7.2 | Rename pip entry points: `cth-mcp-memory` → `archolith-memory`, `cth-mcp-memory-explorer` → `archolith-memory-explorer` | `pyproject.toml:26-27` | |
| 7.3 | Replace all `cth_mcp_memory`/`cth-mcp-memory`/`CTH_MCP_MEMORY` strings with `archolith_memory`/`archolith-memory`/`ARCHOLITH_MEMORY` | All source, test, script, config, doc files | Same scope as Phase 3.1 but targeting `archolith` |
| 7.4 | Replace `x-cth-mcp-memory-*` HTTP headers with `x-archolith-*` | `api/auth.py`, `api/routes.py`, `core/backend_impl.py`, tests | Accept `x-cth-mcp-memory-*` as deprecated alias during transition |
| 7.5 | Rename docker-compose container: `cth-mcp-memory-neo4j` → `archolith-memory-neo4j` | `docker-compose.yml`, `start-server.ps1` | Migration note for Docker users |
| 7.6 | Update `.env.example`: `CTH_MCP_MEMORY_*` → `ARCHOLITH_MEMORY_*` | `.env.example` | |
| 7.7 | Update `pyproject.toml` project name: `cth-mcp-memory` → `archolith-memory` | `pyproject.toml:6` | |
| 7.8 | Update README.md, `.agent/` docs, dotfiles, MCP registry entry | All doc files | |
| 7.9 | Rename GitHub repo / directory if applicable | Workspace config | Coordinate with other archolith sub-projects |
| 7.10 | Add dual-accept for `x-cth-mcp-memory-*` headers during transition; remove `x-yawn-*` support entirely (was deprecated in Phase 3) | `api/auth.py`, `api/routes.py` | `x-yawn-*` support can be dropped at this point |

### Phase 4: Test Script Organization (LOW — X-12, X-13)

**Goal:** Move standalone test scripts into the pytest tree.

| # | Task | Files | Finding |
|---|------|-------|---------|
| 4.1 | Move `smoke_test.py` → `tests/smoke_test.py`; update `cth-mcp-memory.ps1` reference | `smoke_test.py`, `scripts/cth-mcp-memory.ps1:76` | X-13 |
| 4.2 | Move `integration_test.py` → `tests/integration_test.py`; update `cth-mcp-memory.ps1` reference | `integration_test.py`, `scripts/cth-mcp-memory.ps1:80` | X-13 |
| 4.3 | Refactor `integration_test.py` to use `build_memory_services()` + `MemorySettings` instead of raw Graphiti construction | `tests/integration_test.py` | X-12 |

### Phase 5: Docker Hardening (LOW — X-11)

**Goal:** Make docker-compose dev experience more resilient.

| # | Task | Files | Finding |
|---|------|-------|---------|
| 5.1 | Add `restart: unless-stopped` to docker-compose Neo4j service | `docker-compose.yml` | X-11 |
| 5.2 | Add `mem_limit: 2g` to docker-compose Neo4j service | `docker-compose.yml` | X-11 |

### Phase 6: Deferred Items

| Finding | Description | Defer reason |
|---------|-------------|-------------|
| X-14 (partial) | README env var table sync with `.env.example` | Low impact; address when README is next actively edited |
| X-09 (doc files) | `CHANGELOG.md` naming residue | Historical record; only new entries need current naming |
| X-09 (agent docs) | `.agent/` heavy reference docs naming residue | Lower priority than code; address when each doc is next revised |

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Fix Integration Test | 5 | CRITICAL + HIGH + MEDIUM | Immediate |
| 2. Script Portability | 4 | HIGH + MEDIUM | High |
| 3. Naming Migration (`yawn_*` → `cth_mcp_memory`) | 12 | MEDIUM | High |
| 4. Test Script Organization | 3 | LOW | Low |
| 5. Docker Hardening | 2 | LOW | Low |
| 6. Deferred Items | 3 | LOW | Future |
| 7. Project Rename (`cth_mcp_memory` → `archolith_memory`) | 10 | MEDIUM (planned) | Future — coordinate with workspace |
| **Total** | **26** (+ 3 deferred + 10 planned rename) | | |

---

## Naming Migration Execution Order

The naming migration happens in two passes:

### Pass 1: `yawn_*` → `cth_mcp_memory` (Phase 3 — do now)

This fixes what's **currently wrong**. The `yawn_*` names are dead — the package was already renamed.

1. **Code strings first** (3.1) — replace `yawn_memory`/`yawn-memory` in log messages, server names, docstrings
2. **Docker container** (3.5-3.6) — rename requires container stop/remove
3. **Env vars in .env.example** (3.3) — documentation-only change
4. **README** (3.2, 3.4) — user-facing docs
5. **HTTP headers** (3.7-3.12) — wire-protocol, dual-accept with deprecation
6. **Run test suite** — verify no regressions

### Pass 2: `cth_mcp_memory` → `archolith_memory` (Phase 7 — planned, later)

This replaces the **current correct** names with the **final** names. Coordinate with the workspace-wide archolith rename.

1. Rename Python package directory + imports (7.1)
2. Update `pyproject.toml` name + entry points (7.2, 7.7)
3. Replace all code/config/doc strings (7.3, 7.6, 7.8)
4. HTTP header migration: `x-cth-mcp-memory-*` → `x-archolith-*` with dual-accept; drop `x-yawn-*` entirely (7.4, 7.10)
5. Docker container rename (7.5)
6. Update MCP registry + workspace config (7.8, 7.9)
7. Reinstall package; run test suite

**Why two passes instead of one:** The `cth_mcp_memory` package name is what's installed in venvs, referenced by MCP clients, and used in `pyproject.toml` today. Skipping it and going straight to `archolith_memory` would mean touching the package directory structure, reinstall, and all client configs in the same commit as the `yawn_*` cleanup — too much surface area for one change. Two passes let each rename be independently testable.

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Naming migration breaks existing MCP client configs | MCP server name change is cosmetic in tool metadata; clients connect by command, not name. Document the change. |
| `x-cth-mcp-memory-*` headers break remote MCP clients | Phase 3.7-3.9 adds dual-accept; old headers work during transition. Set cutoff version in `endpoints.md`. |
| Docker container rename disrupts running environments | Migration note (3.6) documents `docker stop/rm` before `docker compose up`. |
| `integration_test.py` fix may not match current API | Phase 1.5 runs the test; if function signatures changed, adjust accordingly. |
| Moving test scripts may break `cth-mcp-memory.ps1` | Phase 4 updates the ps1 paths in the same commit. |
| Phase 7 (`archolith_memory` rename) duplicates Phase 3 effort | Phase 3 fixes dead names; Phase 7 replaces live names. Both are independently testable. If the archolith rename is imminent, Phase 3 could be skipped and 7 done directly — but that's riskier (larger blast area). |
| Phase 7 HTTP header rename is the third wire-protocol change | Each rename adds a dual-accept layer. Phase 3 adds `x-cth-mcp-memory-*`; Phase 7 adds `x-archolith-*` and drops `x-yawn-*`. Limit to 3 active header prefixes at most; deprecate the oldest aggressively. |

---

## Dependencies on Other Chunks

| Dependency | Chunk | Finding | Notes |
|------------|-------|---------|-------|
| `settings.py` lowercase env var prefix | Chunk 1 | C1/C4 | Phase 3.3 should use uppercase `CTH_MCP_MEMORY_*` in `.env.example`; Chunk 1 must fix settings.py to accept uppercase |
| `x-yawn-*` header rename in `auth.py`/`routes.py` | Chunk 5 | A-09 | Same finding, same files; coordinate so both plans don't conflict |
| `yawn-memory` naming in `mcp/server.py`, `mcp/resources.py` | Chunk 4 | M-03 | Same naming residue; Phase 3.1 covers these files |
| `yawn-memory` naming in services | Chunk 3 | S-06 | Same naming residue; Phase 3.1 covers these files |
