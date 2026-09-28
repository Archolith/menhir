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

## 2026-09-27 - Record local-stdio MVP platform and configuration decision

- `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`: approve Windows 11
  AMD64 and Ubuntu Linux AMD64 as the primary MVP platforms; record the stdio,
  backend, Neo4j, provider, benchmark-model, default-off feature, and public
  Graphiti fork contract. Keep Oracle preflight, container validation, RC freeze,
  and frozen-RC evidence open.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-27 - Add packaged Graphiti fork stdio acceptance path

- `tests/e2e/test_e2e_06_graphiti_fork.py`: verify the public fork wheel hash,
  installed source bytes, absence of upstream Graphiti, native hook wiring, and
  deterministic ingest/recall through the installed Menhir stdio path.
- `tests/e2e/beacon_post_mvp_scenarios.py`,
  `tests/e2e/test_e2e_08_isolation_adversarial.py`, `.github/workflows/tests.yml`:
  remove deferred Beacon from MVP execution and verify a missing fork makes both
  package integrity and Menhir's runtime check fail explicitly in a disposable install.
- `tests/e2e/README.md`, `.agent/plans/README.md`, the approved MVP plan, and the
  MVP release audit: distinguish the candidate-wheel result from the later
  release-container gate and keep that gate explicitly PENDING in E2E evidence.

## 2026-09-27 - Start independent local-stdio MVP release audit

- `.agent/reviews/menhir-local-stdio-mvp-release-audit-2026-09-27.md`: record the
  supported-path evidence map and the outstanding packaged-Graphiti E2E and
  release-container validation blockers without claiming Gate B closure.

## 2026-09-27 - Prepare public Graphiti fork package for Menhir MVP

- Require `archolith-graphiti-core==0.30.2.post1` so normal installs can use a
  public wheel rather than a VCS checkout while retaining the native fork hooks.
- Lock the published PyPI wheel and source hashes from fork tag `v0.30.2.post1`;
  verify a fresh Menhir wheel install resolves the public fork without upstream Graphiti.
- Simplify the release image to consume the locked, hashed fork wheel directly.
- Make the fork package and its cold-install compatibility an explicit MVP gate;
  defer Beacon generation and consumption from this release.
- `graphiti_client.py`, `graphiti_llm_adapter.py`: retain DeepSeek's JSON-object
  fallback with its schema prompt, and accept the fork's typed-attribute extraction
  flag while preserving its preamble across retries.
- `tests/test_graphiti_client.py`: cover provider requests and the fork's typed
  node-attribute path through Menhir's adapter.
