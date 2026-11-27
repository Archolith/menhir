# Changelog

Older entries: [`CHANGELOG-archive.md`](CHANGELOG-archive.md).

The legacy `user` extraction prompt work in the archive is marked "Superseded by `automatic-memory-v1` on 2026-09-06". `tests/test_canonical_self_contract_docs.py` checks for that phrase in this file.

## 2026-11-27 - Address the extension design review (#725)

- Give extension Views kind-scoped keys and keep deployed built-in keys compatible.
- Run each installed projection definition and version under its own lease and job loop. A stalled materializer or a backend holding the general maintenance lock can leave its own work pending while other projection lanes keep running.
- Bind one registry for the life of the process. Validate synchronous materializers and required View read fields during registration, and document the host and author responsibilities for scope.

## 2026-11-26 - Share extension registration across graph surfaces (#725)

- Use one graph adapter factory and one View repository factory across Menhir's product paths. HTTP, hook and Explorer hosts can pass the same validated registry.
- Hook and Explorer inherit a starting or running backend's registry in the same process. Conflicting registries are refused before those surfaces open graph resources. Stdio continues to use its HTTP backend.

## 2026-11-26 - Add a decision timeline usefulness test (#725)

- Project the sourced D5 decision history through the example extension and provide a timed comparison with finding the approval in the issue thread.

## 2026-11-26 - Register a project decision timeline through HTTP startup (#725)

- Validate host-supplied projection extensions before constructing the server. The HTTP runtime shares the registered View kinds with its scheduler materializers.
- Add a synthetic decision timeline whose source changes and projection work are atomic. Erasing a source removes its historical Views before the scheduler rebuilds the surviving timeline.
- Let the shared View writer run inside an existing Neo4j transaction while preserving statement timeouts for standalone writes.

## 2026-11-26 - Resolve retained projection work explicitly (#725)

- Let an extension requeue an exact retained generation under the current version or queue its removal. The worker still materializes and certifies the new generation.
- Reject stale, quarantined, and already-resolved generations. Concurrent decisions on one generation can advance it once.

## 2026-11-26 - Preserve pending projection work across version changes (#725)

- Keep the exact pending generations from the outgoing definition version available for review. Requeue settled targets under the new version.
- Refuse ordinary target updates that would replace retained work. Report retained targets as stale and keep them out of worker queues.

## 2026-11-26 - Schedule registered projection workers (#725)

- Give each explicitly registered projection definition a separate maintenance job with a 100-item batch. An empty registration set adds no jobs.
- Use the scheduler lease and the Neo4j revocation check to stop dispatch and retry after ownership loss. Already-started transactions still use the work-generation fence.

## 2026-11-26 - Internal projection execution lane (#725)

- Bind each reconciliation lane to a host-supplied materializer for one definition and version. The scalar reconciliation prototype uses the same lane.
- Filter worker queue reads by the installed and shared-current version before the batch limit. Mismatched work remains pending for review.

## 2026-11-26 - Extension roadmap Phase 0 inventory (#725, #727)

- Recorded the runtime adapter constructors and the distinct projection queue and View repair paths.
- Updated the roadmap for approved extension and remote SDK decisions and merged scoped queue fixes.

## 2026-11-25 - Record Graphiti prompt_name in LLM usage telemetry (#91)

- `src/menhir/infrastructure/observability.py`: add request-scoped `set_llm_operation_label` /
  `reset_llm_operation_label` ContextVar helpers and use the active label on `_InstrumentedEndpoint`
  chat completions (falling back to `"chat.completions.create"` when unset; never applied to
  embedding calls).
- `src/menhir/infrastructure/graphiti_llm_adapter.py`: bind `prompt_name` in
  `MenhirOpenAIGenericClient.generate_response` across attempts and retries, resetting in `finally`,
  so `llm_usage_events.operation` attributes per-prompt tokens and cache hits directly.
- Tests: `tests/test_graphiti_prompt_name_telemetry.py`.

## 2026-11-22 - Agent Smith OAuth clients come from the client policy

- `src/menhir/api/oauth_as_metadata.py`: the hardcoded Agent Smith table (`_AGENT_SMITH_CLIENTS`,
  `_OPERATOR_AGENT_SMITH_CLIENTS`) is removed. `/oauth/client-metadata/agent-smith.json?client=<key>`
  and the local CIMD resolution in authorize now publish a document only when the loaded client
  policy lists that exact client_id with a `registration` (client name, loopback redirect URIs).
  Scope comes from the policy. No policy, no registration or a client removed from the policy:
  404 / no local document. URLs, client_ids and ports are unchanged.
- **Deploy precondition:** the production `client-policy.json` must give every Agent Smith client a
  `registration` matching its stored `oauth_clients` row (same name, `http://127.0.0.1:<port>` then
  `http://localhost:<port>` callback), or startup refuses on drift. Adding or retiring a harness
  (e.g. `agy` 43694, later `gemini`/`gemini-config`) is then a policy change, not a release.
- `tests/fixtures/client-policy*.synthetic.json`: Agent Smith entries carry registrations and both
  admit `?client=agy`; digests recomputed.
- Tests: `test_oauth_as_metadata.py` (documents from registrations, 404 without policy, removed or
  unregistered client, foreign issuer), `test_oauth_cimd.py` (local resolution needs the policy;
  registrations reconcile with rows stored by the old table; drift refuses startup),
  `test_client_policy_v3.py` (agy agent tier on v2 and v3; no table fallback).

## 2026-11-15 - Hardening: loopback request checks, ingest path containment, structure signatures

- `src/menhir/api/auth.py`, `api/server_support.py`: credential-free loopback grants also check the
  request's browser origin; `MENHIR_CORS_ORIGINS` is honoured.
- `src/menhir/core/ingest_guard.py`, `core/backend_runtime_data_ops.py`: host system directories are
  refused for every tier; `ingest_document` refuses an existing path that is not a regular file.
- `src/menhir/infrastructure/path_containment.py`, `project_scanner.py`, `artifact_corpus_scanner.py`:
  project scans and the corpus audit read only regular files inside the scanned root; symlinks out
  of it (and Windows junctions), FIFOs, devices and dangling symlinks are skipped, for every tier.
  A file that cannot be examined still refuses the scan. `domain/project_id_file.py`: the legacy
  identity file must be a regular file inside the project.
- `src/menhir/core/backend_runtime_admin_ops.py`, `api/routes_support.py`: the corpus audit uses the
  ingest path containment and is agent tier in the internal dispatch.
- `src/menhir/infrastructure/project_scanner.py`, `infrastructure/structure_queries.py`: text literals
  are elided from stored symbol signatures. The project node records `scanner_schema`; the first
  ingest after an upgrade rewrites every file's symbols once, not only files whose mtime changed.
- `docs/security-posture.md`: keyless loopback request checks and `MENHIR_CORS_ORIGINS`.
- Tests: `test_loopback_cross_origin.py`, `test_ingest_guard_system_paths.py`,
  `core/test_corpus_audit_path_containment.py`, `test_signature_literal_elision.py`,
  `infrastructure/test_scanner_schema_rewrite_online.py`.

## 2026-11-09 - Vector index plan (#574)

- `plans/menhir-574-vector-indexes-2026-11-09.md`: new plan for default vector indexes and
  index-backed similarity search; listed in `plans/README.md`.

## 2026-11-07 - Design records moved in from the maintainer workspace

- Added 16 plans and reviews, trimmed of local paths, host details and live configuration: three current plans, four backlog plans, five archived plans plus the bucket-1 removal plan, the #168 off-flag triage, the agent prompt review and the combined PR #274 reviews.
- `plans/README.md`, `plans/backlog/README.md`: routed the new plans.
- `archive/removed-features-2026-10-27.md`: its evidence and plan links now resolve here.
- `reviews/`: added the anchored-time attach gate review and the #168 freeze flag review, trimmed the same way.
- `CHANGELOG-archive.md`: generalized private host, provider and repository names.
- `anchored_time.py`, `tests/fixtures/anchored_time/README.md`: the P1 plan citation now points at the archive.
- Tracked docs: generalized home paths, LAN addresses, the production hostname, the owner's name, snapshot volume names, and the provider and operations repository names. The archived production migration plan moved to the private deployment repository.
- `archive/plans/`: added the proposer-reviewer vocabulary experiment plan; its five code and test citations now resolve.

## 2026-11-07 - Recall test records added to reference

- `reference/`: added the real-similarity, no-match and graph-value test records, trimmed of local paths and host names.
- `reference/menhir-projection-coverage-audit.md`, `reference/menhir-realization-coverage.md`: retyped as `investigation`.
- `reference/README.md`: the evidence-admission PDF is suggestions only, and its two validator findings are accepted.

## 2026-11-06 - Doc review: removed features marked, reference docs typed

- `architecture.md`, `data_models.md`, `endpoints.md`, `memory-backlog.md`: marked the event-history lane and deterministic shadow as removed.
- `artifacts-provenance-governance-status.md`, `scripts-index.md`, `workflows/scalar_state_measurement.md`, `audit/MODULE-MAP.md`: corrected claims about retired flags and removed scripts.
- `README.md`, `reference/README.md`, `maintenance.md`: fixed routing, the reference count and a duplicate heading.
- `reference/*.md`: declared an artifact type on 13 records, with no IDs or statuses added.
- Four plans: corrected invalid statuses to `IMPLEMENTED`.
- `../CHANGELOG.md`, `../CHANGELOG-archive.md`: moved the oldest 8 entries to meet the 10-entry limit.

## 2026-11-06 - Research routing and changelog trim

- `workflows/artifact_authoring.md`: added where research results go.
- `README.md`: routes recall questions to the experiment record.
- `maintenance.md`: older changelog entries now move to `CHANGELOG-archive.md`.
- `CHANGELOG.md`, `CHANGELOG-archive.md`: moved 114 older entries to the archive. One Windows home path became a repo-style path; the rest is unchanged.
- `../docs/research/README.md` and the cluster READMEs: added "Start here" routing.

## 2026-10-27 - Bucket 1 default-off features removed; kept on an archive branch

- Removed nine default-off features that the #168 freeze did not ship (`be6e3658`..`155d34be`):
  - the oracle subset;
  - the similarity scale;
  - the fact-edge, content-vector and BM25 recall lanes;
  - scalar collapse;
  - scalar canonical self;
  - the deterministic scalar extractor and router;
  - shadow context composition.
- The code is kept on `archive/bucket1-features-2026-10-27` (base `d1105b25`).
  [`archive/removed-features-2026-10-27.md`](archive/removed-features-2026-10-27.md) maps each commit
  to its files and gives restore steps.
- Their settings are in `RETIRED`. A stale env export is ignored, and the bench launchers refuse it.
- Recall Lab arms a, b and c are gone. A request that sets a removed knob gets a 422.
- Equivalence check: for each unit, an empty env resolves to the same settings, recall tuning, hooks
  and constructor values as the base, with no differences beyond the removed keys.

## 2026-10-27 - #168 freeze: tested behavior becomes the default; 23 flags retired

- **Always on, flags retired:**
  - Warden gate, contradiction interrupt, fact said-time, source memories, source-memory spread,
    count anchor hint and stale-lead demotion (`370fd94e`);
  - edge-invalidation statement order (`8ec03d26`);
  - scalar reconcile attribute, scope and subject, scalar View authority, the View freshness gate
    and sum grounding (`d022a7ed`);
  - counting recall authority-only, counting wording, counting extraction v2 and related-count
    matching (`9ec4d08c`, fix `7b26834f`);
  - assistant turns stored only (`bdad118c`).
- **Defaults on, switches kept** (`cc931186`). These three cost LLM calls and remain the way to turn
  the work off:
  - `MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED` and `MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED`: the
    scheduler runs the scalar and counting lanes.
  - `MENHIR_ANCHORED_TIME_RESOLVER`: now also drives the blind attach check and world-end expiry.
    `MENHIR_ANCHORED_TIME_ATTACH` and `_EXPIRY` are retired.
- `MENHIR_FRONTIER_SOURCE_MEMORY_MAX_CHARS` defaults to 2000.
- **Retired keys** are recorded in `RETIRED` (`config/feature_flags.py`). Menhir ignores a retired env
  var, so a stale export cannot turn a feature off.
- **Component knobs** (`RetrievalTuningConfig`, service, adapter and scheduler fields) default on and
  stay as per-request or test seams. No alternate code path was deleted.
- **Scalar View authority** now follows the scalar lane alone (`config/authority_gates.py`).
  Features that required it now require scalar state.
- **Counting stamp:** writers and the API dirty check stamp `<configured>+x2+m`. Namespaces counted
  without v2 or matching are re-counted once.
- **Equivalence check:** an empty env on the new code resolves to the same settings, recall tuning,
  Graphiti hooks and production-constructor values as the old code (`466a8d39`) with the approved
  values set. The same holds with each switch off.
- Plan: workspace `menhir-freeze-retire-always-on-flags-plan-2026-10-27.md`.

## 2026-10-25 - Record statement-order keeps as edge conflicts (#168 item 9, deferred link)

- Each pair the statement-order veto keeps (NEWER_CANDIDATE and CONTRADICTION) is now written as an
  `EdgeInvalidationConflict` record with `kept_by = 'statement_order'`, the fork reason, and the
  cause's source (stored, or this call's). Authority keeps are written exactly as before; a record
  without `kept_by` is an authority keep. One record per ordered (kept, cause) pair, as before.
- Nothing reads these records yet. Linking them to `list_conflicts`, recall and resolution needs
  edge-level conflict code: Menhir's conflict groups live on `:Entity` nodes, so linking through a
  fact's endpoints would flag the user node on every recall. That link is deferred to post-MVP.
- Written only when `MENHIR_EDGE_INVALIDATION_STATEMENT_ORDER` is on.
- `tests/test_edge_invalidation_statement_order_records.py`, `tests/test_edge_invalidation_statement_order_records_online.py`.
