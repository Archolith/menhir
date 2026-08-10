# Remediation Plan: cth.mcp.memory Chunk 1 — Core + Config + Domain

**Date:** 2026-06-05
**Parent:** Chunked cth.mcp.memory Organization Audit
**Scope:** `core/`, `config/`, `domain/`, `main.py`, `__init__.py`, `__main__.py`

---

## Audit Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 4 |
| HIGH | 12 |
| MEDIUM | 21 |
| LOW | 21 |
| **Total** | **58** |

---

## Findings Inventory

### CRITICAL

| ID | Source | File:Line | Description |
|----|--------|-----------|-------------|
| C1 | Config | `settings.py:177-218` | Mixed-case `cth_mcp_memory_*` env var prefix broken on Linux. Auth keys use uppercase `CTH_MCP_MEMORY_*` but ~10 other settings use lowercase `cth_mcp_memory_*`. `os.getenv("cth_mcp_memory_API_HOST")` will NOT match user-set `CTH_MCP_MEMORY_API_HOST` on case-sensitive platforms. |
| C2 | Config | `.env.example` | Missing 22+ env vars that `settings.py` or codebase reads. All `cth_mcp_memory_*`-prefixed vars, tiered auth keys, out-of-band vars (`CORS_ORIGINS`, `LOG_DIR`, `MCP_TELEMETRY_DB`, `EXPLORER_HOST/PORT`) absent. |
| C3 | Config | `.env.example:51-52,57-58` | Documents `YAWN_MEMORY_MCP_TELEMETRY_DB`, `YAWN_MEMORY_MCP_TIMEOUT`, `YAWN_MEMORY_EXPLORER_HOST`, `YAWN_MEMORY_EXPLORER_PORT` — stale names. Actual code reads `cth_mcp_memory_*` equivalents. Setting documented names has no effect. |
| C4 | Config | `settings.py:177-200` | 10 settings use `cth_mcp_memory_` lowercase prefix which is invalid on case-sensitive OS (same root cause as C1). |

### HIGH

| ID | Source | File:Line | Description |
|----|--------|-----------|-------------|
| H1 | Config | `settings.py` | No validation for required secrets. `neo4j_password`, `openai_api_key`, `gemini_api_key`, all auth keys default to `""` with no startup guard. Silently fails at runtime instead of failing fast. |
| H2 | Config | `settings.py` | 6+ env vars read outside `settings.py` via `os.getenv()` in `llama_endpoint.py`, `paths.py`, `telemetry/tracker.py`, `api/server.py`, `logging_config.py`, `mcp/resources.py`, `explorer/app.py` — invisible to anyone reading only `settings.py`. |
| H3 | Config | `settings.py:136-140` | `LLAMA_*` alias env vars are stale legacy from `yawn_memory` era, not in `.env.example`, add cognitive overhead. |
| H4 | Config | `.env.example:14-19` | 6 `SCHEDULER_*` env vars documented but not in `settings.py` — read directly from `os.getenv()`, bypassing centralized settings. |
| H5 | Config | `feature_scope.py:1-43` | `MilestoneZeroScope` and `load_scope()` are dead code — never imported or used outside `config/` package. |
| H6 | Domain | `models.py:52-74` | `MemoryNode` is dead code — never instantiated or imported anywhere. Misleading contract. |
| H7 | Domain | `models.py:57` | `MemoryNode.id` field named `id` but Neo4j/cypher uses `uuid` everywhere. |
| H8 | Domain | `models.py:52-74` | `MemoryNode` missing fields present in Neo4j: `promoted_at`, `rehydration_count`, `name`, `summary`, `target_date`. |
| H9 | Domain | `edges.py:10-16` | `EdgeType` enum lists 6 semantic edge types but missing 10+ structural/operational edge types actually in Neo4j (`ANCHORED_TO`, `CONTAINS`, `DEPENDS_ON`, `TESTS`, `IMPORTS`, `EXPOSES`, `CALLS`, `REFERENCES_FILE`, `CREATED_FROM`, `CONCERNS`). |
| H10 | Domain | `ingest.py:12` | `IngestStatus.QUEUED` is undocumented in `data_models.md`. Doc only lists `INGESTED`, `SKIPPED`, `FAILED` but `QUEUED` is actively returned. |
| H11 | Core | `runtime.py:100-101` | `RuntimeState` uses `object` type for `built` and `session` fields instead of `BuildArtifacts` and `MemorySession`. Forces pervasive `getattr()` usage and defeats type checking. |
| H12 | Core | `bootstrap.py:107-120` | `BuildArtifacts` dataclass lacks `scheduler` field. Runtime dynamically attaches it via `setattr()`, making it invisible to IDEs, `asdict()`, and type checkers. |

### MEDIUM

| ID | Source | File:Line | Description |
|----|--------|-----------|-------------|
| M1 | Core | `backend_protocol.py:237` vs `backend_impl.py:538` | `scan_for_conflicts` default `limit` mismatch: protocol declares 150, both implementations use 100. |
| M2 | Core | `runtime.py:160-204` vs `architecture.md` | Init step numbering inconsistent: logs `[init 1/5]` then `[init 3/6]` — skips step 2, denominator changes. |
| M3 | Core | `runtime_preflight.py:25` | Direct submodule import `from cth_mcp_memory.infrastructure.neo4j import Neo4jRepository` bypasses package public API surface. |
| M4 | Core | `backend_impl.py:402,425` | `RuntimeProvider` uses stale `yawn-memory-` prefix in `asyncio.create_task` names. |
| M5 | Core | Multiple | 32 locations with `yawn-memory` branding in user-facing strings: asyncio task names, HTTP headers (`x-yawn-*`), CLI messages. HTTP headers are a wire contract — breaking change if renamed. |
| M6 | Core | `architecture.md:113` | Architecture doc Package Map still uses `src/yawn_memory/` instead of `src/cth_mcp_memory/`. |
| M7 | Config | `settings.py:88` | `conflict_cooldown_days=0` means "permanent suppression" — aggressive default, no `__post_init__` bound check, negative values accepted. |
| M8 | Config | `settings.py:150-153` | Three different naming conventions for env vars: `GRAPHITI_*` (no prefix), `MEMORY_*` (MEMORY_ prefix), `cth_mcp_memory_*` (cth prefix). |
| M9 | Config | `settings.py:150` | `LLM_CHAT_PROVIDER` has no project prefix — could collide with env vars from other tools. |
| M10 | Config | `settings.py:67` | `graphiti_embed_provider` and `graphiti_reranker_provider` use empty-string-means-inherit pattern without `__post_init__` enforcement. |
| M11 | Config | `feature_scope.py:12-27` | Feature list outdated — references M0 scope but project is well past that. |
| M12 | Config | `settings.py:79` | Boolean parsing via `.lower() in ("true", "1", "yes")` silently treats invalid values as `False`. |
| M13 | Domain | `edges.py:29` | `Edge.source_label` field diverges from Neo4j property `source`. |
| M14 | Domain | `edges.py:19-30` | `Edge` dataclass is dead code — never imported/instantiated outside `__init__.py`. |
| M15 | Domain | `ingest.py:14` | `IngestStatus.SKIPPED` is defined but never used — dead code. |
| M16 | Domain | `models.py:11-18` vs `memory_types.py:131-200` | `MemoryType` enum and `MEMORY_TYPE_POLICIES` dict must be manually kept in sync — no programmatic guard. |
| M17 | Domain | `recall.py:9-11,21,24-26,29-36` | `InvalidQueryPresetError`, `VALID_QUERY_PRESET_VALUES`, `format_query_preset_values()`, `parse_query_preset()` not exported from `__init__.py`. |
| M18 | Domain | `session.py:17-18` | `MemorySession.client_id` and `client_name` fields undocumented in `data_models.md`. |
| M19 | Domain | `edges.py:16` | `EdgeType.PREFERRED_FOR` defined but has zero usage in codebase. |
| M20 | Domain | `models.py:52-74` | `MemoryNode` not frozen but all other domain dataclasses are `frozen=True` — inconsistent. |
| M21 | Config | `.env.example:29` | `OPENAI_CHAT_MODEL` default mismatch: `.env.example` says `gpt-4.1-nano`, `settings.py` defaults to `gpt-4.1-mini`. |

### LOW

(21 LOW findings from all three audits — see subagent reports for full detail. Key items: redundant imports, dead `.clear()` on popped deque, `RuntimeState.clear()`/`clear_all()` dual naming, `__iter__` returns materialized list, `llm_ready` alias fragility, `local_llm_api_key` sentinel value, missing `__post_init__` bound checks, empty-string vs Optional semantics for auth keys, `.env.example` missing timeout vars, etc.)

---

## Phase Plan

### Phase 1: Env Var Standardization (CRITICAL — C1, C4, E-01)

**Goal:** All `cth_mcp_memory_*` env vars use uppercase `CTH_MCP_MEMORY_*` prefix.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 1.1 | Change all `cth_mcp_memory_*` lowercase references in `settings.py:from_env()` to uppercase `CTH_MCP_MEMORY_*` | `settings.py:177-218` | C1/C4 |
| 1.2 | Update all out-of-band `os.getenv("cth_mcp_memory_*")` calls to uppercase | `paths.py:54`, `telemetry/tracker.py:15`, `api/server.py:118`, `logging_config.py:121`, `explorer/app.py:563-564` | C1 |
| 1.3 | Add backward-compat lowercase aliases with deprecation warnings | `settings.py` | C1 |
| 1.4 | Update `.env.example` to use `CTH_MCP_MEMORY_*` consistently | `.env.example` | C1 |
| 1.5 | Verify: `grep -ri "cth_mcp_memory_" --include="*.py"` returns zero lowercase hits | all | C1 |

### Phase 2: .env.example Synchronization (CRITICAL — C2, C3)

**Goal:** `.env.example` is the single source of truth for all configurable env vars.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 2.1 | Remove stale `YAWN_MEMORY_*` entries from `.env.example` | `.env.example:51-52,57-58` | C3 |
| 2.2 | Add all 22+ missing `CTH_MCP_MEMORY_*` env vars | `.env.example` | C2 |
| 2.3 | Add `SCHEDULER_*` vars with comment noting they're read by infrastructure | `.env.example` | H4 |
| 2.4 | Add `MEMORY_GRAPHITI_ADD_EPISODE_TIMEOUT_SECONDS`, `MEMORY_GRAPHITI_REQUEST_STALL_TIMEOUT_SECONDS` | `.env.example` | M (X-06) |
| 2.5 | Fix `OPENAI_CHAT_MODEL` default: align `.env.example` and `settings.py` | `.env.example:29`, `settings.py:55` | M21 |
| 2.6 | Add `MEMORY_BUILD_ID` | `.env.example` | H4 |

### Phase 3: Settings Centralization + Validation (HIGH — H1, H2, D-01, D-02)

**Goal:** All env vars flow through `MemorySettings`; required secrets validated at startup.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 3.1 | Move `SCHEDULER_*` env vars from `llama_endpoint.py`/`paths.py` `os.getenv()` to `MemorySettings` | `settings.py`, `llama_endpoint.py`, `paths.py` | H2 |
| 3.2 | Move `CTH_MCP_MEMORY_CORS_ORIGINS`, `LOG_DIR`, `MCP_TELEMETRY_DB`, `EXPLORER_HOST/PORT` to `MemorySettings` | `settings.py`, `api/server.py`, `logging_config.py`, `paths.py`, `explorer/app.py` | H2 |
| 3.3 | Move `MEMORY_BUILD_ID` to `MemorySettings` | `settings.py`, `mcp/resources.py` | H2 |
| 3.4 | Add `__post_init__` validation: when `chat_provider=openai`, require `openai_api_key` non-empty | `settings.py` | H1 |
| 3.5 | Add startup warning when all auth keys are empty (running with no auth) | `settings.py` or `main.py` | H1 |
| 3.6 | Add `__post_init__` bound check: `conflict_cooldown_days >= 0` | `settings.py` | M7 |
| 3.7 | Remove or deprecate `LLAMA_*` stale aliases (add comment + CHANGELOG note) | `settings.py:136-140` | H3 |

### Phase 4: Runtime State Typing (HIGH — H11, H12)

**Goal:** `RuntimeState` and `BuildArtifacts` use proper types instead of `object`/`setattr`.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 4.1 | Add `scheduler: MaintenanceScheduler | None = None` field to `BuildArtifacts` | `bootstrap.py` | H12 |
| 4.2 | Retype `RuntimeState.built` as `BuildArtifacts | None = None` | `runtime.py:100` | H11 |
| 4.3 | Retype `RuntimeState.session` as `MemorySession | None = None` | `runtime.py:101` | H11 |
| 4.4 | Update `_initialize_services` return type from `tuple[object, object]` to `tuple[BuildArtifacts, MemorySession]` | `runtime.py` | H11 |
| 4.5 | Remove `setattr(built, "scheduler", ...)` and use direct field assignment | `runtime.py:177,203` | H12 |
| 4.6 | Remove `getattr(self.built, "scheduler", None)` and use `self.built.scheduler` | `backend_impl.py` | H12 |
| 4.7 | Fix init step numbering: sequential `[init 1/6]` through `[init 6/6]` | `runtime.py:418-443` | M2 |

### Phase 5: Domain Model Cleanup (HIGH — H6-H10, M13-M20)

**Goal:** Domain types accurately reflect Neo4j reality; dead code removed or documented.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 5.1 | Decide on `MemoryNode`: either (a) align with Neo4j schema (rename `id`→`uuid`, add missing fields) and adopt as canonical, or (b) remove from exports and deprecate | `models.py:52-74`, `__init__.py` | H6/H7/H8 |
| 5.2 | Expand `EdgeType` to include all actual Neo4j relationship types, or remove the enum and make `data_models.md` the authoritative registry | `edges.py:10-16` | H9 |
| 5.3 | If keeping `Edge`: rename `source_label` → `source` to match Neo4j property | `edges.py:29` | M13 |
| 5.4 | If keeping `Edge`: remove `PREFERRED_FOR` (zero usage) or document as planned | `edges.py:16` | M19 |
| 5.5 | If removing `Edge`/`EdgeType`/`MemoryNode`: remove from `__init__.py` exports | `__init__.py` | D-06/D-15 |
| 5.6 | Update `data_models.md` IngestResult: add `QUEUED`, remove `SKIPPED` (or implement skip path) | `data_models.md:202`, `ingest.py:14` | H10/M15 |
| 5.7 | Add `InvalidQueryPresetError`, `VALID_QUERY_PRESET_VALUES`, `format_query_preset_values`, `parse_query_preset` to `domain/__init__.py` exports | `__init__.py` | M17 |
| 5.8 | Add `MemorySession.client_id` and `client_name` to `data_models.md` | `data_models.md` | M18 |
| 5.9 | Add programmatic guard: `assert set(MemoryType) == set(MEMORY_TYPE_POLICIES.keys())` in `memory_types.py` module init | `memory_types.py` | M16 |

### Phase 6: Feature Scope + Naming (HIGH — H5, M11, M5, M6)

**Goal:** Remove dead code, document naming migration path.

| # | Task | Files | Severity |
|---|------|-------|----------|
| 6.1 | Delete `feature_scope.py` (dead code) and remove from `config/__init__.py` exports | `feature_scope.py`, `config/__init__.py` | H5 |
| 6.2 | Update `architecture.md` Package Map: `src/yawn_memory/` → `src/cth_mcp_memory/` | `architecture.md:113` | M6 |
| 6.3 | Plan `yawn-memory` → `cth-mcp-memory` branding migration: internal strings (asyncio task names, CLI messages) can change immediately; HTTP headers (`x-yawn-*`) need versioned wire-protocol migration | Multiple | M5 |
| 6.4 | Update `main.py` docstring from "yawn-memory" to "cth-mcp-memory" | `main.py:1` | Low |
| 6.5 | Update `backend_impl.py` asyncio task names from `yawn-memory-*` to `cth-mcp-memory-*` | `backend_impl.py:402,425` | Low |
| 6.6 | Align `scan_for_conflicts` default `limit`: change protocol from 150→100 (match implementations) | `backend_protocol.py:237` | M1 |

---

## Task Summary

| Phase | Tasks | Severity Range | Priority |
|-------|-------|----------------|----------|
| 1. Env Var Standardization | 5 | CRITICAL | Immediate |
| 2. .env.example Sync | 6 | CRITICAL | Immediate |
| 3. Settings Centralization | 7 | HIGH | High |
| 4. Runtime State Typing | 7 | HIGH | High |
| 5. Domain Model Cleanup | 9 | HIGH + MEDIUM | Medium |
| 6. Feature Scope + Naming | 6 | HIGH + MEDIUM | Medium |
| **Total** | **40** | | |

---

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Env var rename breaks deployed instances | Phase 1.3 adds backward-compat aliases with deprecation warnings |
| HTTP header rename (`x-yawn-*`) breaks API clients | Phase 6.3 defers header migration to a versioned API change |
| Removing `MemoryNode` breaks hypothetical consumers | Verify zero imports with grep before removal; add CHANGELOG entry |
| `BuildArtifacts.scheduler` field addition changes `asdict()` output | `scheduler=None` default means existing callers see no change |
| Moving env vars into `settings.py` changes initialization order | Test that `MemorySettings` can be constructed before Neo4j is available |

---

## Dependencies

- Phase 1 and 2 can run in parallel (both are CRITICAL)
- Phase 3 depends on Phase 1 (env var naming must be settled first)
- Phase 4 is independent of Phases 1-3
- Phase 5 is independent but should align with Chunk 2 (infrastructure) findings about actual Neo4j schema
- Phase 6 is independent
