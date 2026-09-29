## 2026-09-28 - Schedule scalar-only personal memory

- `src/menhir/core/runtime.py`, `src/menhir/services/maintenance_scheduler.py`:
  create the chat dependency and register the shared background job when scalar
  state alone is enabled, while keeping counter and event lanes independent.
- `tests/test_settings_event_history_runtime.py`, `tests/test_api_routes.py`:
  cover all eight lane combinations, the unavailable-provider case, and scalar
  parity with API-triggered consolidation.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Ground scalar clock times with dotted meridiems

- `src/menhir/services/typed_scalar_rules.py`: normalize dotted AM/PM source
  times correctly and refuse unsupported suffixes instead of accepting a bare
  time prefix that could overwrite a correct model value.
- `tests/test_typed_scalar_perception.py`: cover source spelling, noon and
  midnight, malformed times, and full-row grounding.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Refuse ambiguous scalar subject fallback

- `src/menhir/services/typed_scalar_rules.py`: distinguish absent, ambiguous,
  invalid, and unique subject matches so a conflicting local owner cannot fall
  through to a looser spelling or namespace lookup.
- `tests/test_typed_scalar_self_binding.py`,
  `tests/test_typed_scalar_bind_persist.py`: cover exact and variant ambiguity,
  blank UUIDs, both namespace forms, persisted advisories, and repair.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Reconcile MVP preflight evidence before RC freeze

- `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`: record the completed issue inventory, five-item Oracle harness preflight, and merged Opus fixes while keeping exact-RC gates open.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Preserve MVP release evidence and clarify stdio setup

- `README.md`, `docs/post-install.md`, `.env.example`: show the running-backend requirement and client environment for the supported local stdio path.
- `tests/e2e/conftest.py`, `tests/e2e/README.md`, `tests/test_e2e_work_root.py`: keep local E2E evidence outside pytest's session scratch directory and document where to inspect it.
- `.github/workflows/release-image.yml`, `tests/test_release_image_workflow.py`: fail validation when the in-image Graphiti fork probe fails before evidence upload.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Keep pytest scratch local and report cleanup failures

- `pytest.ini`, `tests/conftest.py`, `tests/_temp_cleanup.py`: use pytest's public temporary-path fixtures outside the Git checkout, close CLI log handlers in the scratch tree, remove it with Windows read-only-file handling, and surface residue as a test failure.
- `tests/infrastructure/test_git_log.py`, `tests/test_audit_trail.py`, `tests/test_consolidation_audit.py`, `tests/test_consumer_session_e2e.py`, `tests/test_telemetry_stats.py`: move test scratch databases and Git repositories under pytest-owned temporary paths and restore the session test's telemetry setting.
- `tests/test_mcp_server.py`, `tests/test_mcp_telemetry.py`, `tests/test_services_pipeline.py`: report manual scratch cleanup failures instead of silently ignoring them, and close the inspected SQLite connection.
- `tests/test_temp_hygiene.py`: catch direct scratch-directory bypasses and hidden cleanup failures, and keep pytest paths outside the repository for non-repository tests.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Reconcile local-main MVP safety fixes

- `src/menhir/infrastructure/episode_lifecycle.py`, `src/menhir/infrastructure/memory_graph_adapter.py`, `src/menhir/services/enrichment_steps.py`, `src/menhir/services/ingest_worker.py`: fold transient retry refunds into the claim-fenced episode transition so stale workers cannot refund a newer claim.
- `src/menhir/infrastructure/consolidation_queries.py`, `src/menhir/core/backend_client_ops.py`, `src/menhir/core/backend_protocol.py`, `src/menhir/core/backend_runtime_admin_ops.py`, `src/menhir/mcp/tools/conflict/resolve_conflict.py`: keep conflict reads, writes, readback, and edge bridging inside the requested namespace; return conflict member UUIDs in stable order.
- `src/menhir/services/ingest_limits.py`, `src/menhir/services/ingest_intake.py`, `src/menhir/api/routes_support.py`, `src/menhir/services/enrichment_steps.py`, `src/menhir/core/backend_runtime_admin_ops.py`: share memory text and diff limits across local intake, direct temporal writes, and the API.
- `tests/conftest.py`, `tests/services/test_ingest_write_bounds.py`, `tests/test_api_routes.py`, `tests/test_backend_roundtrip.py`, `tests/test_circuit_breaker.py`, `tests/test_conflict_namespace_scope.py`, `tests/test_episode_lifecycle.py`, `tests/test_high_wave1_remediation.py`, `tests/test_mcp_server.py`, `tests/test_memory_graph_adapter_methods.py`, `tests/test_transient_exhausted_recovery_live.py`, `tests/test_transient_refund_online.py`: cover write boundaries, mixed-group namespace isolation, deterministic results, and stale-claim retry accounting.
- `tests/e2e/test_e2e_08_isolation_adversarial.py`: recognize the shared validator's explicit oversized-diff refusal in the stdio acceptance criterion.
- `.agent/plans/menhir-local-main-safety-reconciliation-2026-09-28.md`, `.agent/plans/README.md`, `.agent/architecture.md`, `.agent/data_models.md`, `.agent/endpoints.md`: record the reconciliation scope and updated contracts.
## 2026-09-28 - Harden post-release audit boundaries before MVP RC

- `.github/workflows/tests.yml`, `tests/e2e/conftest.py`, `tests/e2e/_harness/ci_evidence_gate.py`, `tests/test_e2e_ci_gate.py`: fail wheel-build errors and reject missing, skipped, or unproven installed-wheel acceptance in CI.
- `src/menhir/services/beacon_compat.py`, `tests/test_beacon_compat.py`: isolate Beacon subprocess imports from repository-controlled modules while preserving the installed Beacon contract.
- `src/menhir/snapshot/upload_client.py`, `src/menhir/cli/sync.py`, `tests/snapshot/test_upload_client_offline.py`, `tests/snapshot/test_upload_security.py`: refuse unsafe operator-key transports and redirects before disclosure.
- `src/menhir/infrastructure/graphiti_llm_adapter.py`, `tests/test_graphiti_combined_extraction_patch.py`, `tests/test_graphiti_fork_contract.py`, `docs/post-install.md`: restore JSON-object relationship aliases and explain mixed Graphiti installation repair.
- `.agent/plans/menhir-post-v023-audit-remediation-2026-09-28.md`, `.agent/plans/README.md`, `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`: keep the follow-up work and RC gate explicit.

## 2026-09-28 - Close the local-stdio MVP Gate B audit

- `.agent/reviews/menhir-local-stdio-mvp-release-audit-2026-09-27.md`: close Gate B against merged main after exact-commit CI, Ubuntu stdio criteria, Windows strict E2E, and no-publish Graphiti image receipts; keep RC freeze and final release separate.
- `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`: record Gate B and pre-freeze Gate C completion, exact evidence, and the remaining named-RC work.
- `.agent/archive/plans/menhir-gate-b-remediation-2026-09-28.md`, `.agent/plans/README.md`: mark the completed remediation plan IMPLEMENTED, archive it, and remove it from active execution routing.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Prove denied scans and sealed Graphiti container preflight

- `src/menhir/core/backend_runtime_data_ops.py`: report failed project traversal to stdio callers as a clear refusal while preserving the existing index.
- `tests/infrastructure/test_gateb_structure_publication_online.py`: keep the graph-preservation regression aligned with the caller-visible refusal.
- `tests/e2e/test_e2e_08_isolation_adversarial.py`: deny a real directory listing on Windows or POSIX and compare the indexed graph before and after a rejected rescan.
- `tests/e2e/test_e2e_06_graphiti_fork.py`: verify and probe a no-publish release image artifact from the same clean commit; retain explicit PENDING evidence when no bundle is supplied.
- `tests/e2e/README.md`: document both platform and container evidence paths.
- `.agent/reviews/menhir-local-stdio-mvp-release-audit-2026-09-27.md`: reconcile merged correctness fixes and exact-commit evidence without declaring RC release approval.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
