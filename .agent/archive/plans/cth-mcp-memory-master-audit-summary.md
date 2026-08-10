# cth.mcp.memory — Full Audit Master Summary

> **Naming note:** `cth.mcp.memory` is this same project (menhir) under its pre-rebrand
> name — package history is `yawn_memory` -> `cth_mcp_memory` -> `menhir`
> (commit `c70c5a5`, "rebrand package cth_mcp_memory -> menhir + env vars MENHIR_*").
> This audit corpus is menhir's own history, not a cross-project reference.

**Date:** 2026-06-06
**Auditor:** OpenCode (z-ai/glm-5.1)
**Scope:** Complete cth.mcp.memory codebase — 6 chunks covering all source, test, script, config, and doc files

---

## Chunk Completion Status

| Chunk | Scope | Findings | Tasks | Review File | Plan File |
|-------|-------|----------|-------|-------------|-----------|
| 1 | Core + Config + Domain | 58 | 40 + deferred | N/A (early chunk) | `cth-mcp-memory-chunk1-core-config-domain-remediation-plan.md` |
| 2 | Infrastructure (33 files) | 27 | 31 + 11 deferred | N/A | `cth-mcp-memory-chunk2-infrastructure-remediation-plan.md` |
| 3 | Services (14 files) | 20 | 29 + 6 deferred | N/A | `cth-mcp-memory-chunk3-services-remediation-plan.md` |
| 4 | MCP Tools (40 files) | 12 | 19 + 1 deferred | `MCP-TOOLS-AUDIT-2026-06-06.md` | `cth-mcp-memory-chunk4-mcp-tools-remediation-plan.md` |
| 5 | API + Explorer + CLI (18 files) | 17 | 29 + 3 deferred | `API-EXPLORER-CLI-AUDIT-2026-06-06.md` | `cth-mcp-memory-chunk5-api-explorer-cli-remediation-plan.md` |
| 6 | Cross-cutting + Scripts + Docs | 15 | 26 + 3 deferred + 10 planned (archolith rename) | `CROSS-CUTTING-SCRIPTS-DOCS-AUDIT-2026-06-06.md` | `cth-mcp-memory-chunk6-cross-cutting-scripts-docs-remediation-plan.md` |
| **Total** | | **149** | **174** (+ 24 deferred + 10 planned) | | |

---

## Severity Distribution

| Severity | Chunk 1 | Chunk 2 | Chunk 3 | Chunk 4 | Chunk 5 | Chunk 6 | **Total** |
|----------|---------|---------|---------|---------|---------|---------|-----------|
| CRITICAL | 2 | 1 | 1 | 1 | 1 | 1 | **7** |
| HIGH | 4 | 3 | 2 | 3 | 2 | 3 | **17** |
| MEDIUM | 28 | 13 | 9 | 5 | 8 | 6 | **69** |
| LOW | 24 | 10 | 8 | 3 | 6 | 5 | **56** |

---

## CRITICAL Findings (7)

| ID | Chunk | File | Description |
|----|-------|------|-------------|
| C1/C4 | 1 | `settings.py:177-200` | 10 settings use lowercase `cth_mcp_memory_*` env var prefix — invalid on case-sensitive OS |
| H1 | 1 | `settings.py` | No validation for required secrets; startup can succeed with empty Neo4j password |
| I-18 | 2 | `structure_queries.py:485` | `e.id` instead of `e.uuid` silently breaks document linking |
| S-01 | 3 | `ingest_service.py:799` | Undefined variable `project` in `ingest_episode()` — NameError at runtime |
| M-01 | 4 | `contracts.py:50` | Unbounded rate-limit dict `_query_add_memory_events` grows without key eviction |
| A-04 | 5 | `explorer/app.py:410-427` | Explorer has zero auth; all routes + Neo4j data unauthenticated |
| X-01 | 6 | `integration_test.py:12` | Dead import `from yawn_memory.main` — crashes on any run |

---

## HIGH Findings (17)

| ID | Chunk | Description |
|----|-------|-------------|
| H2 | 1 | 6+ env vars read outside settings (no validation, no defaults) |
| H4 | 1 | `MemoryNode` class is dead code — never instantiated |
| H9 | 1 | `EdgeType` enum missing 10+ Neo4j relationship types |
| H10 | 1 | `ProcessingState` enum values don't match database strings |
| I-01 | 2 | `EmbeddingCache` module-level singleton lacks thread-safe access |
| I-03 | 2 | Index names mismatch — `phase_one_schema_ready()` never reports ready |
| I-08 | 2 | `_determine_scope()` always returns "persistent" — session scope is dead |
| S-04 | 3 | `maintenance_scheduler.py` missing jobs for decay/conflicts/consolidation |
| S-06 | 3 | 38 files with `yawn-memory` naming residue |
| M-02 | 4 | `scan_conflicts` default limit mismatch (150 vs 500) |
| M-03 | 4 | `yawn-memory` naming residue in MCP server name and resource payloads |
| M-04 | 4 | Silent exception swallow in `_build_temporal_header` |
| A-02 | 5 | CORS defaults to `["*"]` wildcard with no production warning |
| A-14 | 5 | Graph traversal queries have no LIMIT — can freeze browser on large graphs |
| X-02 | 6 | Stale env var names in integration_test.py |
| X-03 | 6 | Hardcoded Windows venv path in start-server.sh |
| X-04 | 6 | Hardcoded absolute developer path in run-hidden.vbs |

---

## Cross-cutting Themes

### 1. Naming Residue (largest single issue — 79+ occurrences across 22 files)

The `yawn_memory` → `cth_mcp_memory` rename is partially complete. The Python package, `pyproject.toml`, and entry points are correct. But 18 source files, 2 doc files, docker-compose, `.env.example`, and the README still carry old names. The `x-yawn-*` HTTP headers (16 occurrences) are wire-protocol and need a versioned dual-accept migration.

The project will eventually be renamed to `archolith-memory`. The naming migration is therefore structured in two passes:

- **Pass 1 (Phase 3, do now):** `yawn_*` → `cth_mcp_memory` — fixes what's currently broken. These names are dead.
- **Pass 2 (Phase 7, planned later):** `cth_mcp_memory` → `archolith_memory` — replaces current names with final names. Coordinate with workspace-wide archolith rename.

If the archolith rename is imminent, Pass 1 could be skipped and everything done in one shot — but that creates a larger blast area and makes testing harder.

**Effort:** ~2-3 hours for Pass 1 (non-headers), ~4-6 hours for header migration with tests, ~6-8 hours for Pass 2 (package rename + all references + reinstall).

### 2. Missing Input Validation (3 CRITICAL + 2 HIGH)

No startup validation for required secrets (Chunk 1), no bounds on in-memory rate-limit dict (Chunk 4), undefined variable crash in ingest (Chunk 3), dead import crash in integration test (Chunk 6), and `e.id` vs `e.uuid` silent data corruption (Chunk 2).

**Effort:** ~3-4 hours total.

### 3. Security Gaps (1 CRITICAL + 2 HIGH)

Explorer has zero auth (Chunk 5), CORS is wide open by default (Chunk 5), and hardcoded credentials in profiling scripts (Chunk 6). The existing `verified-current-findings.md` already notes the Explorer auth and Neo4j defaults.

**Effort:** ~4-6 hours (Explorer auth is the biggest piece).

### 4. Dead Code and Stale Contracts (4 HIGH)

`MemoryNode` never instantiated (Chunk 1), `ProcessingState` values don't match DB (Chunk 1), session scope always returns "persistent" (Chunk 2), scheduler missing lifecycle jobs (Chunk 3).

**Effort:** ~2-3 hours.

---

## Recommended Execution Order

1. **CRITICAL fixes first** — undefined variable (S-01), dead import (X-01), `e.id`/`e.uuid` (I-18), rate-limit memory leak (M-01), env var case-sensitivity (C1/C4), missing secret validation (H1)
2. **Explorer auth** (A-04) — security surface
3. **Naming migration Pass 1** — `yawn_*` → `cth_mcp_memory` (code strings, docker, env vars, docs, HTTP headers with dual-accept)
4. **Dead code cleanup** — MemoryNode, ProcessingState, session scope
5. **Scheduler completion** — add decay/conflict/consolidation jobs
6. **Script portability** — start-server.sh, run-hidden.vbs, profile_recall.py credentials
7. **LOW items** — test organization, docker resource limits, README sync
8. **Naming migration Pass 2** — `cth_mcp_memory` → `archolith_memory` (coordinate with workspace-wide archolith rename; includes package dir rename, pyproject.toml, reinstall, MCP registry, all references)

---

## File Locations

All deliverables are in `projects/ctharvey/cth.mcp.memory/.agent/`:

- **Plans:** `plans/cth-mcp-memory-chunk{1-6}-*.md`
- **Reviews:** `reviews/MCP-TOOLS-AUDIT-2026-06-06.md`, `reviews/API-EXPLORER-CLI-AUDIT-2026-06-06.md`, `reviews/CROSS-CUTTING-SCRIPTS-DOCS-AUDIT-2026-06-06.md`
- **Audit strategy:** `plans/cth-mcp-memory-audit-chunked-plan.md`
