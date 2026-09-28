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

## 2026-09-26 - Reconcile native Graphiti fork with current Menhir

- `pyproject.toml`, `uv.lock`: pin the public Archolith Graphiti 0.30.2 maintenance
  commit `6b907b93fed32cb979093327608a4fd897b39751` and preserve locked registry dependencies.
- Recover the unfinished Phase F migration onto published main: replace all 17 runtime
  patch installers with native fork hooks and Menhir-owned extraction, resolution and
  LLM policy adapters. Keep subsequent ingestion, retention and recall fixes.
- `graphiti_client.py`, `graphiti_resolution_policy.py`: flush telemetry inside its
  request task on success, failure and cancellation; count empty candidate searches.
- `graphiti_llm_adapter.py`: preserve namespace and operation metadata across retries.
- Release wheelhouse: derive the immutable fork requirement from package metadata,
  pin/hash the additional build backend closure, and build without isolated dependency
  resolution. Docker remains an offline wheel consumer.
- Migrated contract tests and added dependency, request-context, task-boundary and
  immutable-build-pin regressions. Feature defaults and deployed configuration stay as-is.
- Fork baseline is now 0.30.2. Default-on readiness (#169 and siblings) still requires
  outstanding source-grounding fixes and current graph/model quality evidence; this
  integration does not itself qualify a feature for default enablement.

## 2026-09-26 - remaining MVP audit fixes for recall and decay (#154, #144)

- `recall_pipeline.py`: acquire applicable independent sources before deciding recall is empty;
  assemble standalone edge candidates before fallback, retain pending results and search-failure attribution,
  and avoid metadata/adjacency round trips for an empty node pool.
- `consolidation_queries.py`, `memory_graph_adapter.py`, `lifecycle_decay.py`: rotate bounded decay
  batches by persistent least-recent selection, mark selected rows before processing, and log selection
  separately from successful work. Preserve access/age/retention/policy gates and deletion disarm.
- `test_recall_service.py`: empty/filtered/failed/pending semantic pools, file visibility/session guards,
  enabled observation-only and standalone edge lanes, plus actual scoped file-to-recall Neo4j acquisition.
- `test_lifecycle_service.py`: actual 501-record skipped-batch/restart regression, marker order for both
  phases, unchanged access/freshness, direct/source retention, and marker-failure refusal.
- `.agent/memory-policy.md`, `.agent/data_models.md`: define independent acquisition and the scheduling-only
  selection marker, finite-set fairness and restart/rollout limits. No production writes or migration.
- `CHANGELOG-archive.md`: move the oldest entry to keep ten.

## 2026-09-26 - chronological memory reads tolerate legacy timestamp storage (#145)

- Normalize native dates and valid legacy ISO text before database ordering and limits in recent,
  flagged, scope, and type reads; invalid access falls back to creation, unknown dates sort last,
  and equal instants use a stable UUID tie-break without losing fractional precision.
- Stamp native memory timestamps in TEMPORAL, candidate, L4, TODO-reminder mirrors, and View refreshes;
  retain candidate/artifact creation receipts when storage types change.
- Use guarded timestamp conversion for decay and session age predicates; unknown ages cannot
  justify destructive decay. Document writer coverage, manual backfill precautions, and scoring limits.
- Disposable Neo4j regressions cover actual reads/touches, bounded startup selection, writer receipts,
  lifecycle protection, offsets, invalid calendar values, native/local dates, and nanosecond ties.
- Archive the oldest changelog entry to keep ten. No automatic migration or production data changes.
