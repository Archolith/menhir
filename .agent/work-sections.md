# Menhir Work Sections & Subsections

Status: canonical work-organization bisection, approved 2026-11-25.

This document divides `menhir` into **8 large sections** (`S1`–`S8`) and **27 subsections** for planning, issue triage, ownership, and targeted test selection.

- **Alignment with `archolith-atlas`:** `S1`–`S6` and `S8` map 1:1 to the 7 product areas in `../../archolith-atlas` (synced to Menhir `v0.3.15`). `S7` (**Platform, Storage & Release**) splits cross-cutting storage, provider, telemetry, and release/CI work out of `S6` so engineering workstreams have a clear home without creating a competing product taxonomy.
- **How to use it:**
  - Tag new plans, investigations, and workwaves with a primary section (`S1`–`S8`) and subsection (`S1.1`–`S8.3`).
  - Use the listed **Code** and **Test** anchors as the starting point for `workflows/run_and_test.md` targeted verification.
  - Shared-infra changes in `S7` (e.g., `settings.py`, `runtime.py`, `schema.py`, `memory_graph_adapter*`, `pyproject.toml`) escalate to broader/full CI verification.
  - Keep product-capability changes in sync with `../../archolith-atlas` (`npm run check`).

---

## Summary Map

| ID | Large Section | Atlas Area | Subsections | Primary Focus |
|---|---|---|---|---|
| **S1** | Ingest & Retain | `ingestAndRetain` | `S1.1`–`S1.3` | Intake, TurnEvidence, scrubbing, async Graphiti extraction, self-binding, decay, pins, erasure |
| **S2** | Typed State & Consolidation | `consolidateState` | `S2.1`–`S2.3` | Scalar perception, Event → Fold → View projections, counting, anchored time, counters |
| **S3** | Retrieve & Decide | `retrieveAndDecide` | `S3.1`–`S3.5` | Dual-lane candidates, scoring/rerank, Oracles, Wardens, scalar authority, context packing, timelines |
| **S4** | Govern, Curate & Track Work | `governAndCurate` | `S4.1`–`S4.3` | Trust tiers, candidates, promotion, provenance, conflict governance, WorkArtifacts, TODOs |
| **S5** | Code Graph & Impact | `understandCode` | `S5.1`–`S5.3` | Repo scanning, incremental watcher, blast radius, affected tests, `ANCHORED_TO`, stale advisories |
| **S6** | Interfaces, Auth & Operations | `exposeAndOperate` | `S6.1`–`S6.3` | `menhir serve`, MCP/REST/Hermes, OAuth 2.1, tiers/namespaces, CLI, Explorer/Labs, scheduler/sagas |
| **S7** | Platform, Storage & Release | *(split from `exposeAndOperate`)* | `S7.1`–`S7.4` | Neo4j/FalkorDBLite, schema/vectors, Graphiti fork & LLM/embed resilience, SQLite telemetry, flags, CI/release |
| **S8** | Research Frontier & Retired | `researchAndRetired` | `S8.1`–`S8.3` | Opt-in/unwired code slices, speculative/parked research, `#399` retired experiments |

---

## S1. Ingest & Retain (`atlas: ingestAndRetain`)

Owns how raw episodes, documents, and user prompts enter Menhir, pass secret scrubbing and async extraction, bind canonical self identity, and age, stay pinned, or erase cleanly.

### S1.1 Intake & Evidence Capture (`intakeSurface`)
- **Scope:** Episode & document write intake, payload/diff size bounds, TurnEvidence capture hooks, secret redaction/blocking.
- **Code:** `src/menhir/services/ingest_intake.py`, `src/menhir/services/ingest_limits.py`, `src/menhir/infrastructure/turn_evidence_repository.py`, `src/menhir/domain/secret_scrub.py`, `src/menhir/services/secret_scan.py`
- **Tests:** `tests/test_ingest_document_tool.py`, `tests/test_secret_scan.py`
- **Docs / Plans:** `tasks-ingest.md`, `adr/0001-conversation-turn-capture-surface.md`, `../docs/turn-evidence-producers.md`

### S1.2 Async Enrichment & Graphiti Extraction (`enrichmentEngine`)
- **Scope:** Durable Neo4j enrichment queue, worker leases/heartbeats, retry/manual-review classification, Graphiti combined extraction & edge invalidation policy, two-tier canonical self binding.
- **Code:** `src/menhir/services/ingest_queue.py`, `src/menhir/services/ingest_worker.py`, `src/menhir/infrastructure/graphiti_extraction_policy.py`, `src/menhir/infrastructure/graphiti_edge_invalidation.py`, `src/menhir/domain/self_identity.py`, `src/menhir/infrastructure/self_binding.py`
- **Tests:** `tests/test_heartbeat_lease_renewal.py`, `tests/test_graphiti_combined_extraction_patch.py`
- **Docs / Plans:** `workflows/troubleshoot_enrichment_stalls.md`, `workflows/canonical-self-migration-runbook.md`, `adr/0010-deterministic-canonical-self-identity.md`, `plans/menhir-two-tier-self-identity-plan-2026-10-21.md`

### S1.3 Lifecycle, Retention & Erasure (`retentionAndErasure`)
- **Scope:** Freshness & compression decay, direct/source-linked retention flags & bootstrap pins, provenance-aware erasure and derived re-derivation.
- **Code:** `src/menhir/services/lifecycle_decay.py`, `src/menhir/domain/retention.py`, `src/menhir/domain/bootstrap_scope.py`, `src/menhir/services/erasure_coordinator.py`, `src/menhir/services/derived_rederive.py`
- **Tests:** `tests/test_decay_logic.py`, `tests/test_live_source_retention.py`, `tests/test_erasure_coordinator.py`
- **Docs / Plans:** `memory-policy.md`, `../docs/your-data.md`

---

## S2. Typed State & Consolidation (`atlas: consolidateState`)

Owns turning grounded statements into auditable, rebuildable typed state via the Event → Fold → View boundary, plus counting, anchored time, and operational counters.

### S2.1 Typed Scalar Perception & Grounding (`scalarPerception`)
- **Scope:** K-sample span-grounded scalar perception, per-kind source-grounding guards (`#411`), approximate/hedged scalar abstention.
- **Code:** `src/menhir/services/typed_scalar_perception.py`, `src/menhir/services/typed_scalar_rules.py`, `src/menhir/services/perception.py`, `src/menhir/domain/truth/assertion_spans.py`
- **Tests:** `tests/test_typed_scalar_perception.py`, `tests/test_scalar_source_grounding_411.py`, `tests/test_scalar_hedged_abstention.py`
- **Docs / Plans:** `../docs/personal-memory-readiness.md`, `plans/menhir-compositional-scalar-identity-2026-08-05.md`, `plans/menhir-deterministic-first-event-scalar-2026-07-30.md`

### S2.2 Deterministic Fold & Projections (`foldAndViews`)
- **Scope:** Immutable `TypedAssertion` store, deterministic fold algebra, `ScalarStateView` & `ScalarHistoryView` rebuild/repair, counting consolidation, View-evidence lifecycle.
- **Code:** `src/menhir/domain/typed_assertion.py`, `src/menhir/domain/fold_algebra.py`, `src/menhir/infrastructure/typed_assertion_repository.py`, `src/menhir/infrastructure/view_repository.py`, `src/menhir/services/scalar_state_service.py`, `src/menhir/services/scalar_consolidation.py`, `src/menhir/services/counting_consolidation.py`, `src/menhir/domain/count_decide.py`
- **Tests:** `tests/test_typed_assertion_repository.py`, `tests/test_scalar_state_view.py`, `tests/test_counting_consolidation.py`
- **Docs / Plans:** `adr/0003-event-fold-view-projection-boundary.md`, `reference/fold-algebra.md`, `workflows/scalar_state_measurement.md`, `plans/menhir-projection-realization-coverage-implementation.md`, `plans/menhir-view-evidence-lifecycle-2026-08-28.md`, `plans/menhir-scalar-counting-plan-2026-10-15.md`

### S2.3 Anchored Time & Experience Counters (`temporalAndCounters`)
- **Scope:** Anchored-time resolution & attachment, experience/failure counters, retrieval entropy probes (`view_entropy`).
- **Code:** `src/menhir/infrastructure/anchored_time_resolver.py`, `src/menhir/infrastructure/anchored_time_attach.py`, `src/menhir/infrastructure/quantstate_repository.py`, `src/menhir/services/failure_counter_bridge.py`, `src/menhir/services/view_entropy.py`
- **Tests:** `tests/test_anchored_time_attach.py`, `tests/test_experience_counters_task.py`, `tests/test_view_entropy.py`
- **Docs / Plans:** `memory-aggregation-under-uncertainty.md`

---

## S3. Retrieve & Decide (`atlas: retrieveAndDecide`)

Owns candidate generation, relevance scoring, local reranking, read-only Oracles, assertion-boundary Wardens, authority presentation, context packing, and historical/timeline navigation.

### S3.1 Dual-Lane Candidate Generation (`candidateGeneration`)
- **Scope:** Entity hybrid search (BM25 + vector), `file_context` structural neighbor injection, raw source-memory episode lane and observation dedup.
- **Code:** `src/menhir/services/hybrid_retrieval.py`, `src/menhir/services/recall_pipeline.py`, `src/menhir/domain/structural_expansion.py`, `src/menhir/domain/source_memory_spread.py`
- **Tests:** `tests/test_source_memory_lane.py`, `tests/test_structural_anchoring.py`
- **Docs / Plans:** `retrieval-profiles.md`, `reference/menhir-recall-experiment-record.md`

### S3.2 Blended Scoring & Cross-Encoder Rerank (`scoringAndRerank`)
- **Scope:** Preset-weighted relevance scoring (`similarity`, `adjacency`, `recency`, `prominence`, `conflict`), stale-lead handling, digest-pinned local ONNX cross-encoder reranking.
- **Code:** `src/menhir/services/scoring_service.py`, `src/menhir/domain/stale_lead.py`, `src/menhir/services/cross_encoder_rerank.py`
- **Tests:** `tests/test_scoring_service.py`, `tests/test_recall_cross_encoder_rerank.py`
- **Docs / Plans:** `memory-ingest-queries.md`, `domain/retrieval_trace_models.py`

### S3.3 Read-Only Oracles & Combiner (`oracles`)
- **Scope:** The 6 read-only Oracles (`semantic`, `structure`, `scope`, `temporal`, `evidence`, `intent`) and `LogSpaceOracleCombiner`.
- **Code:** `src/menhir/services/retrieval_oracles.py`, `src/menhir/services/oracle_executor.py`, `src/menhir/domain/oracle_combiner.py`, `src/menhir/domain/query_intent.py`, `src/menhir/domain/temporal.py`, `src/menhir/domain/self_reinforcement.py`
- **Tests:** `tests/test_retrieval_oracles.py`, `tests/test_intent_oracle.py`, `tests/test_oracle_combiner.py`

### S3.4 Assertion-Boundary Warden Chain (`wardens`)
- **Scope:** `ExhaustionWarden`, `ScopeWarden`, `OracleAdmissionWarden`, `ContradictionWarden`, and `WardenChain` safety verdicts/traces.
- **Code:** `src/menhir/domain/warden.py`, `src/menhir/domain/exhaustion.py`, `src/menhir/domain/scope.py`
- **Tests:** `tests/domain/test_warden.py`, `tests/test_warden_safety_matrix.py`, `tests/test_warden_execution_status.py`

### S3.5 Authority, Context Packing & Time Navigation (`contextAndNavigation`)
- **Scope:** Separate `ScalarAuthorityLayer` presentation & contributor expansion, startup bootstrap (`read_flagged_memories`, `recall_context_memories`), token-budgeted `build_context`, `recall_history`, `recall_timeline`, and `rate_recall`.
- **Code:** `src/menhir/domain/scalar_view_authority.py`, `src/menhir/domain/scalar_view_suppression.py`, `src/menhir/services/recall_service.py`, `src/menhir/services/context_builder.py`, `src/menhir/services/timeline_service.py`, `src/menhir/mcp/feedback.py`
- **Tests:** `tests/test_scalar_view_authority.py`, `tests/test_context_builder.py`, `tests/test_recall_timeline.py`

---

## S4. Govern, Curate & Track Work (`atlas: governAndCurate`)

Owns trust separation, provenance inspection, contradiction governance, engineering `WorkArtifact` lifecycles, and graph-linked TODOs.

### S4.1 Trust Tiers, Staging & Provenance (`authorityAndProvenance`)
- **Scope:** `CANDIDATE` staging/review, `PROMOTED` ground-truth authority, `domain/truth/` vocabulary SSOT, `get_provenance` expansion.
- **Code:** `src/menhir/domain/truth/`, `src/menhir/services/candidate_service.py`, `src/menhir/infrastructure/candidate_repository.py`, `src/menhir/mcp/tools/ingest/promote_memory.py`, `src/menhir/mcp/tools/ops/get_provenance.py`
- **Tests:** `tests/test_candidate_review_431.py`, `tests/test_promote_memory.py`, `tests/test_recall_provenance.py`
- **Docs / Plans:** `memory-governance.md`, `artifacts-provenance-governance-status.md`, `adr/0009-source-bound-admission-authority.md`

### S4.2 Contradiction Scan, Review & Resolution (`conflictGovernance`)
- **Scope:** Similarity-based conflict scanning, LLM contradiction confirmation, conflict resolution (`keep_both`, `replace`, `discard_new`), operator `review_inbox` and warning acknowledgement.
- **Code:** `src/menhir/services/lifecycle_conflicts.py`, `src/menhir/mcp/tools/conflict/`, `src/menhir/mcp/tools/ops/review_inbox.py`
- **Tests:** `tests/test_conflict_detection_signal.py`, `tests/test_conflict_tools.py`, `tests/test_review_inbox_tool.py`
- **Docs / Plans:** `conflict-resolution-history-proposal.md`, `plans/menhir-conflict-similarity-floor-plan-2026-11-05.md`, `plans/menhir-conflict-suggestion-remediation-2026-08-09.md`

### S4.3 Engineering WorkArtifacts & Graph TODOs (`workArtifactsAndTodos`)
- **Scope:** `WorkArtifact` lifecycle state machine, typed links/supersession, corpus reconciliation & relocation, direct Neo4j `:Todo` nodes and repository-relative locations.
- **Code:** `src/menhir/domain/work_artifact.py`, `src/menhir/infrastructure/work_artifact_repository.py`, `src/menhir/services/artifact_reconciliation_service.py`, `src/menhir/infrastructure/todo_repository.py`, `src/menhir/domain/todo_location.py`
- **Tests:** `tests/test_work_artifact.py`, `tests/test_artifact_reconciliation.py`, `tests/test_todo.py`
- **Docs / Plans:** `workflows/artifact_authoring.md`, `adr/0008-separate-identity-embodiment-and-locator.md`, `plans/menhir-work-artifact-reconciliation-2026-08-11.md`

---

## S5. Code Graph & Impact (`atlas: understandCode`)

Owns the deterministic structural code graph in Neo4j and its bridge to semantic memory.

### S5.1 Deterministic Scanner & Watcher (`projectScanner`)
- **Scope:** `ingest_project` repository scanning (files, AST symbols, imports, calls, endpoints, tests, deps), 30-minute incremental mtime watcher, project identity binding & write fence.
- **Code:** `src/menhir/services/project_ingest.py`, `src/menhir/infrastructure/project_scanner.py`, `src/menhir/services/project_identity_service.py`, `src/menhir/infrastructure/structure_write_fence.py`
- **Tests:** `tests/test_project_ingest_service.py`, `tests/test_structure_watcher.py`

### S5.2 Structure Queries & Coverage-Aware Impact (`impactAnalysis`)
- **Scope:** All 13 `query_structure` modes (`projects`, `overview`, `files`, `imports`, `tests`, `endpoints`, `dependencies`, `cross_refs`, `blast_radius`, `affected_tests`, `symbols`, `context`, `documents`) and index-coverage caveats.
- **Code:** `src/menhir/infrastructure/structure_queries*.py`, `src/menhir/mcp/tools/recall/query_structure.py`
- **Tests:** `tests/test_structure_queries.py`, `tests/test_query_structure_tool.py`

### S5.3 Code Anchors & Staleness (`codeMemoryBridge`)
- **Scope:** Path normalization & `ANCHORED_TO` linking from narratives/diffs, optional file-event hooks (`POST /api/tool-events`), advisory stale-anchor labeling & verification.
- **Code:** `src/menhir/infrastructure/structural_anchoring.py`, `src/menhir/services/stale_labeling.py`, `src/menhir/infrastructure/tool_event_repository.py`
- **Tests:** `tests/test_diff_attachment.py`, `tests/test_hook_center_tool_events.py`
- **Docs / Plans:** `../docs/hook-center-tool-events.md`

---

## S6. Interfaces, Auth & Operations (`atlas: exposeAndOperate`)

Owns the single-owner runtime process, all agent/human access surfaces, authentication/tenancy boundaries, CLI, Explorer/Labs, and background scheduling/sagas.

### S6.1 Single-Owner Runtime, MCP & REST (`runtimeAndSurfaces`)
- **Scope:** `menhir serve` canonical runtime owner, `RuntimeProvider` / `BackendClient` protocol seam, Streamable HTTP (`/mcp-http`) and stdio MCP bridge, 9 MCP resources, 68 profiled tools, public REST API, Hermes adapter.
- **Code:** `src/menhir/core/runtime*.py`, `src/menhir/core/backend_*.py`, `src/menhir/mcp/server.py`, `src/menhir/mcp/contracts.py`, `src/menhir/mcp/tool_profiles.py`, `src/menhir/mcp/resources.py`, `src/menhir/api/routes.py`, `src/menhir/api/mcp_remote.py`, `integrations/hermes/menhir/`
- **Tests:** `tests/test_mcp_server.py`, `tests/test_tool_profiles.py`, `tests/test_hermes_memory_provider.py`
- **Docs / Plans:** `endpoints.md`, `tasks-mcp.md`, `workflows/backend-first-mcp.md`, `adr/0004-single-runtime-owner-and-backend-first-access.md`

### S6.2 OAuth 2.1, Credential Tiers & Namespace Silos (`authAndIsolation`)
- **Scope:** Embedded OAuth 2.1 AS (PKCE S256, CIMD/DCR, rotating refresh tokens), client-policy & bearer tiers, fail-closed allowlists, core-enforced namespace isolation & teardown.
- **Code:** `src/menhir/api/oauth*.py`, `src/menhir/api/client_policy.py`, `src/menhir/api/auth*.py`, `src/menhir/config/oauth.py`, `src/menhir/config/auth_mode.py`, `src/menhir/core/tenancy.py`, `src/menhir/domain/namespace.py`
- **Tests:** `tests/test_oauth_authorize.py`, `tests/test_client_policy_v3.py`, `tests/test_namespace_isolation.py`
- **Docs / Plans:** `../docs/security-posture.md`, `../docs/runbooks/oauth-remote-mcp-checklist.md`, `adr/0005-core-enforced-namespace-isolation.md`, `plans/menhir-namespace-contract-2026-08-09.md`

### S6.3 CLI, Explorer/Labs, Scheduler & Sagas (`opsCliAndExplorer`)
- **Scope:** `menhir` CLI onboarding/ops commands (`up`, `setup`, `check`, `diagnostics`, `serve-watch`, `console`, `saga`, etc.), Explorer & Recall/Extraction Labs, SQLite-leased maintenance scheduler, recoverable cross-store sagas.
- **Code:** `src/menhir/cli/`, `src/menhir/explorer/`, `src/menhir/services/maintenance_scheduler.py`, `src/menhir/services/scheduler_lease.py`, `src/menhir/services/scheduler_tasks.py`, `src/menhir/services/saga_reconcile_dispatcher.py`
- **Tests:** `tests/test_cli_up.py`, `tests/test_explorer_app.py`, `tests/test_feature_taxonomy.py`
- **Docs / Plans:** `workflows/operations_runbook.md`, `workflows/logging-and-troubleshooting.md`, `adr/0006-recoverable-sagas-for-cross-store-mutations.md`, `plans/menhir-427-saga-journal-truth-plan-2026-11-12.md`

---

## S7. Platform, Storage & Release *(engineering split from `exposeAndOperate`)*

Owns shared infrastructure, storage engines, external-provider boundaries, configuration/flags, and CI/release mechanics. Changes here typically have wide blast radius and trigger escalated verification.

### S7.1 Graph Storage, Schema & Vector Indexes
- **Scope:** Neo4j driver/repository, `MemoryGraphAdapter` composition facade, schema/constraint bootstrap, Neo4j vector indexes (`#574`), and the `#109` FalkorDBLite backend seam.
- **Code:** `src/menhir/infrastructure/neo4j.py`, `src/menhir/infrastructure/memory_graph_adapter*.py`, `src/menhir/infrastructure/schema*.py`, `src/menhir/infrastructure/vector_indexes.py`, `src/menhir/infrastructure/graphiti_vector_search.py`, `src/menhir/core/bootstrap.py`
- **Tests:** `tests/test_vector_indexes_574.py` (plus online Neo4j CI lane)
- **Docs / Plans:** `data_models.md`, `plans/menhir-574-vector-indexes-2026-11-09.md`, root `../../../../.agent/plans/menhir-falkordblite-roadmap.md`

### S7.2 Model Providers, Graphiti Fork & Resilience
- **Scope:** `archolith-graphiti-core` pin & extension hooks, LLM/embedding provider adapters (`openai`, `openai_compat`, `anthropic` scaffold), `EmbeddingCache`, `CircuitBreaker`, DNS/request guards, rolling LLM budgets.
- **Code:** `src/menhir/infrastructure/graphiti_client.py`, `src/menhir/infrastructure/graphiti_llm_adapter.py`, `src/menhir/infrastructure/graphiti_resolution_policy.py`, `src/menhir/infrastructure/llm.py`, `src/menhir/infrastructure/sync_llm.py`, `src/menhir/infrastructure/embedding_cache.py`, `src/menhir/infrastructure/circuit_breaker.py`
- **Tests:** Provider/adapter unit tests + `smoke_test.py`
- **Docs / Plans:** `architecture.md` (`runtime.providers`)

### S7.3 SQLite Telemetry & Configuration / Feature Flags
- **Scope:** SQLite telemetry sidecar (events, failures, lifecycle stats, recall receipts/diagnostics, erasure purge), immutable `MemorySettings` snapshot, and the `feature_flags.py` SSOT registry.
- **Code:** `src/menhir/infrastructure/telemetry/`, `src/menhir/config/settings*.py`, `src/menhir/config/feature_flags.py`, `src/menhir/config/authority_gates.py`
- **Tests:** `tests/test_feature_flag_registry.py`
- **Docs / Plans:** `default-off-features.md`, `plans/menhir-feature-flag-registry.md`

### S7.4 CI, Packaging, Release & Benchmark Alignment
- **Scope:** GitHub Actions CI & PyPI publishing, wheelhouse/Docker release builds, local compose stacks, scripts index, and boundary contracts with `archolith-bench` and private `Archolith/menhir-deploy`.
- **Code:** `.github/workflows/`, `deploy/`, `pyproject.toml`, `docker-compose*.yml`, `scripts/`
- **Tests:** `.github/workflows/tests.yml`, `deploy/lib/verify_wheelhouse.py`, `deploy/release_pipeline.py`
- **Docs / Plans:** `workflows/run_and_test.md`, `scripts-index.md`, `../deploy/README.md`, `../docs/evaluation.md`

---

## S8. Research Frontier & Retired (`atlas: researchAndRetired`)

Keeps non-default, unwired, speculative, and bench-retired work explicitly separated from shipped defaults.

### S8.1 Code-Backed Opt-In & Unwired Slices
- **Scope:** Built and unit-tested code not active by default or awaiting wiring: L4 knowledge artifacts, facet candidate shadow, graph verifier sync, diagnostic/audit shadows, snapshot sync pipeline (`menhir sync`), deferred Beacon memory provider (`#120`), belief/git domain substrates.
- **Code:** `src/menhir/domain/artifacts.py`, `src/menhir/services/artifact_service.py`, `src/menhir/domain/facets*.py`, `src/menhir/services/verifier_sync.py`, `src/menhir/services/assertion_pipeline.py`, `src/menhir/snapshot/`, `src/menhir/services/beacon_*.py`, `src/menhir/domain/belief.py`, `src/menhir/domain/git_staleness.py`
- **Tests:** `tests/test_l4_artifact_loop_integration.py`, `tests/test_verifier_sync.py`, `tests/snapshot/`, `tests/test_beacon_generation.py`
- **Docs / Plans:** `plans/menhir-mcp-snapshot-ingest-2026-09-16.md`, `plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`

### S8.2 Speculative & Parked Research
- **Scope:** Forward designs with no live runtime implementation: L3 semantic overlay, ColdStartBrief & task Oracles, Oracle scheduler/amplification, Intent State View (`Law 3`), cognitive replay, sealed recall & signed admission, adaptive activation topology.
- **Docs / Plans:** `../docs/research/README.md`, `../docs/roadmap/README.md`, `plans/menhir-research-execution-ladder.md`, `plans/menhir-intent-state-view-2026-08-08.md`, `plans/backlog/README.md`

### S8.3 Bench-Tested & Retired Ledger
- **Scope:** Experiments removed from `main` in Oct 2026 (`#399`) after neutral/negative benchmark verdicts (belief/evidence gates, entity-search/pool/diversity flags, event-history lane, scalar shadow/brief experiments).
- **Code / Evidence:** `src/menhir/config/feature_flags.py` (`RETIRED`), `archive/removed-features-2026-10-27.md`, `reference/menhir-recall-experiment-record.md`
