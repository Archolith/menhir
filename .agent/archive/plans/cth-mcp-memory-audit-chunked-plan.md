# Plan: Chunked cth.mcp.memory Organization Audit

**Date:** 2026-06-05
**Parent Goal:** Audit cth.mcp.memory for code/module organization correctness and ability to accomplish stated goals, then write remediation plans. This project will be ported to archolith at some point.

---

## Project Overview

`cth.mcp.memory` is a Python service for long-term memory graph research and policy experiments for agent context. It targets a local Neo4j + Graphiti stack and serves as an MCP-backed recall/ingest service.

**Stats:** ~100 Python source files across 10 subpackages, 75 test files, 7 scripts, rich `.agent/` docs (37 files).

**Status:** Post-v1 hardening (M0-M7 complete). Scoring, lifecycle, consolidation, conflict governance, and ops hardening are all complete.

---

## Audit Chunks

### Chunk 1: Core + Config + Domain (~20 source files)

**Files:**
- `core/`: `backend_impl.py`, `backend_protocol.py`, `bootstrap.py`, `runtime_preflight.py`, `runtime.py`, `__init__.py`
- `config/`: `settings.py`, `feature_scope.py`, `__init__.py`
- `domain/`: `edges.py`, `ingest.py`, `memory_types.py`, `models.py`, `recall.py`, `session.py`, `utils.py`, `__init__.py`
- `__init__.py`, `__main__.py`, `main.py`
- Associated tests: `test_main_checks.py`, `test_settings.py`, `test_milestone_zero_scope.py`, `test_utils.py`, `test_scaffold.py`, `test_degraded_startup.py`, `test_budget_caps.py`

**Focus:**
- Backend protocol/impl contract: does `BackendProtocol` match `BackendImpl` / `RuntimeProvider` / `BackendClient`?
- `main.py` startup checks: are they current with the actual runtime shape?
- Settings: env var naming consistency (yawn vs cth vs memory), stale defaults
- Domain models: do they match what's documented in `data_models.md`?
- `runtime.py` lifecycle state management: are all states handled?
- Package naming: `cth_mcp_memory` vs `yawn_memory` (architecture.md uses both)
- Domain recall types: are presets complete and consistent across domain/mcp/services?

### Chunk 2: Infrastructure — Graph/Neo4j Layer (~33 source files)

**Files:**
- `infrastructure/`: `circuit_breaker.py`, `consolidation_queries.py`, `correlation_queries.py`, `cypher.py`, `embedding_cache.py`, `embedding_dimensions.py`, `episode_lifecycle.py`, `episode_maintenance.py`, `episode_repository.py`, `episode_stamping.py`, `graphiti_client.py`, `graphiti_helpers.py`, `graphiti_patches.py`, `llama_endpoint.py`, `llm.py`, `logging_config.py`, `memory_graph_adapter.py`, `memory_queries.py`, `neo4j.py`, `observability.py`, `paths.py`, `pending_actions.py`, `project_scanner.py`, `providers.py`, `scheduler_trace.py`, `schema.py`, `structural_anchoring.py`, `structure_queries.py`, `telemetry/` (`recorders.py`, `store.py`), `temporal_repository.py`, `todo_repository.py`, `__init__.py`
- Associated tests: `test_circuit_breaker.py`, `test_cypher.py`, `test_embedding_cache.py`, `test_embedding_dimensions.py`, `test_episode_lifecycle.py`, `test_graphiti_client.py`, `test_llama_endpoint.py`, `test_memory_graph_adapter_methods.py`, `test_neo4j_repository.py`, `test_observability.py`, `test_project_scanner.py`, `test_structural_anchoring.py`, `test_structure_queries.py`, `test_scheduler_trace.py`, `test_telemetry_stats.py`, `test_thread_safety.py`, `test_provider_backends.py`

**Focus:**
- Neo4j repository: Cypher query correctness, parameter binding, injection risks
- Graphiti client: patches, helper compatibility with current graphiti-core version
- Schema bootstrap: are indexes/constraints current with actual node/edge types?
- Episode lifecycle: state machine transitions (PENDING → ENRICHING → ENRICHED/FAILED), race conditions
- Memory graph adapter: does it delegate all protocol methods? Any missing?
- Embedding cache: LRU correctness, invalidation, thread safety
- Circuit breaker: state transitions, half-open recovery
- Project scanner: AST correctness, language coverage gaps, incremental diff reliability
- LLM provider scaffold: `anthropic` stub, `gemini` completeness
- Telemetry store: SQLite schema, concurrent write safety, cleanup/rotation

### Chunk 3: Services (~14 source files)

**Files:**
- `services/`: `context_builder.py`, `correlation_service.py`, `enrichment_failures.py`, `enrichment_steps.py`, `ingest_service.py`, `lifecycle_service.py`, `maintenance_scheduler.py`, `recall_service.py`, `scheduler_lease.py`, `scheduler_protocols.py`, `scheduler_tasks.py`, `scoring_service.py`, `__init__.py`
- `pipeline/`: `__init__.py` (empty — is this dead code?)
- Associated tests: `test_context_builder.py`, `test_correlation_service.py`, `test_enrichment_failures.py`, `test_ingest_live.py`, `test_lifecycle_service.py`, `test_lifecycle_live.py`, `test_recall_service.py`, `test_recall_live.py`, `test_scoring_service.py`, `test_services_pipeline.py`, `test_regression_state_machines.py`, `test_conflict_history.py`, `test_regression_conflicts.py`

**Focus:**
- Enrichment pipeline: steps ordering, failure classification, retry semantics
- Ingest service: single-flight lock correctness, timeout handling, oversized rejection
- Recall service: two-phase pipeline correctness, preset validation, file_context integration
- Scoring service: formula correctness, preset weight alignment with docs, threshold behavior
- Lifecycle service: decay transitions, conflict routing thresholds, merge logic
- Maintenance scheduler: job completeness, interval tuning, idempotency
- Scheduler lease: SQLite singleton correctness, stale recovery, force-takeover safety
- Correlation service: threshold constants alignment with architecture.md
- `pipeline/` directory: is it dead code? Should it be removed or repurposed?

### Chunk 4: MCP Tools + Contracts + Resources (~40 source files)

**Files:**
- `mcp/`: `contracts.py`, `formatters.py`, `lifecycle.py`, `resources.py`, `server.py`, `service_access.py`, `__init__.py`
- `mcp/telemetry/`: (contents TBD)
- `mcp/tools/base.py`, `mcp/tools/__init__.py`
- `mcp/tools/ingest/`: `add_memory.py`, `add_memory_and_track.py`, `close_memory.py`, `delete_memory.py`, `flag_memory.py`, `ingest_document.py`, `ingest_project.py`, `__init__.py`
- `mcp/tools/recall/`: `build_context.py`, `query_structure.py`, `read_flagged_memories.py`, `recall_context_memories.py`, `recall_memories.py`, `__init__.py`
- `mcp/tools/ops/`: `add_todo.py`, `close_stale_todos.py`, `close_todo.py`, `force_reenrich.py`, `force_release_lease.py`, `force_scheduler_takeover.py`, `get_client_context.py`, `get_enrichment_status.py`, `get_episode_trace.py`, `get_memory_stats.py`, `list_enrichment_queue.py`, `list_todos.py`, `pause_scheduler.py`, `recover_orphans.py`, `repair_stale_enrichment.py`, `resume_scheduler.py`, `watch_enrichment.py`, `__init__.py`
- `mcp/tools/conflict/`: `list_conflicts.py`, `requeue_for_review.py`, `resolve_conflict.py`, `run_llm_review.py`, `scan_conflicts.py`, `__init__.py`
- Associated tests: `test_mcp_formatters.py`, `test_mcp_gateway.py`, `test_mcp_server.py`, `test_mcp_remote.py`, `test_mcp_telemetry.py`, `test_cli_hook.py`, `test_conflict_tools.py`, `test_scheduler_tools.py`, `test_query_structure_tool.py`, `test_query_auth_policy.py`, `test_ingest_document_tool.py`, `test_todo.py`

**Focus:**
- Tool registration completeness: do all tool classes appear in their `__init__.py`?
- Contract compliance: do all tools inherit from `BaseTextTool`/`BaseJsonTool`? Any ad-hoc handlers?
- Service access layer: does `service_access.py` correctly bridge MCP→backend?
- Formatters: output format consistency, score breakdown serialization
- Lifecycle: stdio MCP lifespan wrapper correctness, backend URL requirement
- Resources: do all resources use `BaseJsonResource`? Any old `get_services()` bridges?
- MCP server wiring: are tools/resources registered in the right order?
- Tool parameter validation: are required params enforced? Optional params typed?
- Ops tools: are admin/ops tools properly gated behind auth?
- Conflict tools: scan/resolution flow, LLM review orchestration

### Chunk 5: API + Explorer + CLI (~18 source files)

**Files:**
- `api/`: `auth.py`, `errors.py`, `mcp_remote.py`, `request_context.py`, `routes.py`, `server.py`, `__init__.py`
- `explorer/`: `app.py`, `static/`, `templates/`, `__init__.py`
- `cli/`: `__main__.py`, `_backend_context.py`, `bootstrap.py`, `hook.py`, `output.py`, `__init__.py`
- Associated tests: `test_api_auth.py`, `test_api_routes.py`, `test_explorer_app.py`, `test_milestone_two_contract.py`, `test_milestone_three_contract.py`, `test_sidecar_consistency.py`, `test_sidecar_expansion.py`, `test_m0_retrieval_baseline.py`

**Focus:**
- API auth: is it enforced consistently? Any unauthenticated routes that should be?
- API routes: do they match `.agent/endpoints.md`? Backend method allowlist current?
- MCP remote: tool-only surface correctness, transport setup
- Request context: caller-session binding, middleware chain
- Explorer: session query limitation (noted in architecture), Cytoscape visualization
- CLI: hook bootstrap, output formatting, backend context for stdio mode
- Error handling: HTTP error codes consistent? Internal errors leaked?

### Chunk 6: Cross-Cutting + Git Hygiene + Scripts + `.agent/` Docs (~15 files)

**Files:**
- `scripts/`: `cth-mcp-memory.ps1`, `profile_recall.py`, `repair_embedding_dimensions.py`, `run_mcp_gateway.py`, `run-hidden.vbs`, `start-server.ps1`, `start-server.sh`
- Root: `integration_test.py`, `smoke_test.py`, `.env.example`, `pyproject.toml`, `docker-compose.yml`, `pytest.ini`, `.gitignore`
- `.agent/`: `README.md`, `architecture.md`, `data_models.md`, `endpoints.md`, `glossary.md`, `memory-design.md`, `memory-foundations.md`, `memory-policy.md`, `memory-ingest-queries.md`, `memory-futures.md`, `memory-backlog.md`, `maintenance.md`, `concept-ids.md`, `verified-current-findings.md`, `file-index.md`, `workflows/`
- Dotfiles: `CLAUDE.md`, `QWEN.md`, `gemini.md`, `AGENTS.md`, `.cursorrules`, `.clinerules`, `.windsurfrules`

**Focus:**
- Package naming audit: `yawn_memory` vs `cth_mcp_memory` vs `cth-mcp-memory` — inconsistent references
- Architecture doc: does `src/yawn_memory/` package map match actual `src/cth_mcp_memory/`?
- `.agent/` doc accuracy vs actual codebase (similar to archolith-context chunk 7)
- `pyproject.toml`: dependencies current? Version correct? Entry points working?
- `.gitignore` completeness: `.env`, telemetry DB, logs, `__pycache__`, `.venv`
- Scripts: production vs one-off? Current with actual config?
- Docker config correctness
- Cross-package naming sweep: any stale `yawn.memory` references that should be `cth.mcp.memory`?

---

## Execution Notes

- Each chunk should produce a **findings list** with severity (CRITICAL/HIGH/MEDIUM/LOW) and exact file:line references
- After all 6 chunks complete, write per-chunk remediation plans (following the archolith pattern)
- **No consolidation** — keep per-chunk plans separate to preserve detail
- Chunks 1–5 can technically run in parallel but sequential is safer for cross-cutting context
- Chunk 6 (cross-cutting) should run last since it depends on findings from 1–5
- The `yawn_memory` → `cth_mcp_memory` naming issue is likely the biggest cross-cutting concern

---

## Estimated Effort

| Chunk | Source Files | Test Files | Est. Session Fraction |
|-------|-------------|------------|----------------------|
| 1: Core + Config + Domain | ~20 | ~7 | 1 session |
| 2: Infrastructure | ~33 | ~17 | 1.5 sessions |
| 3: Services | ~14 | ~13 | 0.75 session |
| 4: MCP Tools | ~40 | ~12 | 1.5 sessions |
| 5: API + Explorer + CLI | ~18 | ~8 | 0.75 session |
| 6: Cross-cutting + Docs | ~15 | n/a | 0.75 session |
| **Total** | ~100+ | ~57 | ~6.25 sessions |

Can be compressed by running Chunks 1+3 or 3+5 together if context budget allows.
