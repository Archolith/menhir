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

## 2026-09-28 - Let isolated scanners read the release image archive

- `deploy/build_release_image.py`: make the saved public-source image archive readable to non-root scanners and give Syft enough temporary space to unpack its layers during no-publish validation.
- `.github/workflows/release-image.yml`: surface a failed builder's final diagnostic as a job annotation and verify the fork version, upstream absence, and native hook wiring inside the no-publish candidate image.
- `tests/test_build_release_image.py`: reproduce Docker's restrictive archive mode, verify the scanner-readable mode on POSIX, and pin Syft's temporary-space budget.
- `tests/test_release_image_workflow.py`: keep the offline container fork probe in the validation job before artifact upload.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Close Gate B structure, artifact, and TODO correctness gaps

- `src/menhir/infrastructure/project_scanner.py`: refuse unreadable traversal, stat, and source reads before publication; bump the scanner fingerprint schema.
- `src/menhir/infrastructure/structure_write_fence.py`: issue graph-backed scan generations and lock each project during transactional publication.
- `src/menhir/infrastructure/structure_queries.py`: publish scan metadata last, preserve unseen legacy symbols on capped scans, and scope fingerprint reads and binding refreshes to project identity.
- `src/menhir/infrastructure/memory_graph_adapter.py`: validate the latest scan generation and publish structure, binding refreshes, and document writes under the project transaction lock.
- `src/menhir/core/backend_runtime_data_ops.py`: mint scan generations before manual and detached symbol scans and carry them into skipped binding refreshes.
- `src/menhir/core/backend_shared.py`: preserve scan generations through serialized payloads and reject missing or inconsistent coverage counts before publication.
- `src/menhir/services/scheduler_tasks.py`: settle identity and mint the generation before watcher traversal; report scan and binding failures.
- `src/menhir/services/scheduler_protocols.py`: describe the watcher scan-generation and binding-refresh methods.
- `src/menhir/domain/work_artifact.py`: require a replacement for `SUPERSEDED` and validate known lifecycle states.
- `src/menhir/infrastructure/work_artifact_repository.py`: lock artifact updates and compare observed lifecycle state before applying transitions or supersession, including legacy null namespaces.
- `src/menhir/services/artifact_reconciliation_service.py`: leave unresolved source declarations in place when a superseded registration has no replacement.
- `src/menhir/mcp/tools/ops/transition_artifact.py`: explain stale transitions and the dedicated supersession operation.
- `src/menhir/infrastructure/todo_repository.py`: link unique visible files, persist each chosen project and canonical path, and return linked and unresolved locations.
- `src/menhir/mcp/tools/ops/add_todo.py`: report linked files and unresolved location reasons.
- `src/menhir/mcp/tools/ops/get_todo.py`: display every linked file on a TODO read.
- `src/menhir/mcp/tools/recall/query_structure.py`: qualify empty symbols, dependencies, documents, and affected-test answers by index coverage.
- `tests/test_project_scanner.py`: traversal, stat, and required-read refusal regressions.
- `tests/test_query_structure_tool.py`: complete, partial, and legacy-unknown negative-answer regressions.
- `tests/test_structure_watcher.py`, `tests/test_beacon_provider.py`, and `tests/infrastructure/test_cf257_detached_write_identity.py`: cover the new watcher and detached-write ordering contract.
- `tests/infrastructure/test_cf257_stale_claim.py`, `tests/test_high_wave1_remediation.py`, and `tests/test_high_wave5_explorer_domain.py`: keep the existing identity, rescan, and unscoped-transition regressions aligned with the new token and lifecycle compare-and-set boundaries.
- `tests/infrastructure/test_gateb_structure_publication_online.py`: real-Neo4j stale-scan, rollback, same-project serialization, capped legacy-symbol, cross-identity refresh, and injected-traversal regressions.
- `tests/test_work_artifact.py`, `tests/test_artifact_tools.py`, `tests/test_artifact_source_reconciliation_io.py`, and `tests/test_cf48_domain_owns_artifact_predicates.py`: artifact lifecycle and unresolved-import regressions.
- `tests/test_work_artifact_online.py`: real-Neo4j artifact transition, supersession, and rollback races.
- `tests/test_todo.py` and `tests/test_todo_file_link_online.py`: unique, ambiguous, multi-location, namespace, and restart link regressions.
- `tests/e2e/test_e2e_08_isolation_adversarial.py`: distinguish namespace-delete caps from a real structural scan cap and check preservation of an indexed file.
- `tests/e2e/test_e2e_04_workartifacts.py` and `tests/e2e/_harness/artifact_corpus.py`: verify a legacy superseded source without a replacement stays unregistered and byte-identical while valid artifacts reconcile.
- `tests/e2e/README.md`: explain the two separate cap criteria.
- `.agent/data_models.md`, `.agent/endpoints.md`, and `.agent/workflows/artifact_authoring.md`: document scan ordering, TODO resolution, and replacement-backed supersession.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Plan Gate B correctness remediation

- `.agent/plans/menhir-gate-b-remediation-2026-09-28.md`: define bounded fixes and
  regression evidence for supported-path structure, WorkArtifact, and TODO audit
  findings, including concurrency, legacy-state, and rollback decisions.
- `.agent/plans/README.md`: route the proposed work package under the approved
  local-stdio MVP release plan.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-27 - Separate operator deployment from public Menhir

- Move the operator host configuration, production runbooks, release receipts,
  staging/promotion scripts, migration pipeline, and offline tests into the
  private `Archolith/menhir-deploy` repository. Preserve their relative paths
  for offline verification with a pinned public Menhir checkout.
- Keep the public package, local self-host instructions, sealed image builder,
  and disposable Docker checks. Correct the Docker guide so it no longer
  claims a plain clone can build the release image.
- Replace production-policy fixtures in public security tests with a synthetic
  policy. Keep runtime checks public and add a boundary check against re-adding
  operator files. Earlier public Git history still contains operator details;
  no history rewrite, host rotation, deployment, or release occurred.
