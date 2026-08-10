# Menhir Frontier Dark Code Audit

## Audit Anchor

- Worktree: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier`
- Branch: `claude/menhir-chain-handoff-doc-7iuat2`
- HEAD: `c9dd15555e91a6a02e40a649b953221c86c3dcb0`
- Frozen source/test state: `2026-07-04T16:48:38.3079583Z`
- Scope: 317 current Python source/test files, including two untracked tests.
- Aggregate source/test SHA-256: `7532955dcf71a667bcfc3ac4453e2760a8496b3a50c60020e71da1e3f18f209c`
- Dirty source/test state: 19 tracked modifications plus untracked `tests/test_api_tier_enforcement.py` and `tests/test_destructive_audit.py`.
- Governing method: dark-code/dead-code evidence contract in `C:\Users\you\IdeaProjects\.agent\audit\maintainability-audit.md`.

The audit target is the current filesystem, not HEAD alone. It did not modify source, tests, configuration, or git state.

## Executive Summary

No module is labeled absolutely dead merely because grep found no caller. The audit traced package entry points, CLI/runtime bootstrap, API router mounting, MCP registration, scheduler construction, internal imports, dynamic dispatch, `importlib` use, test imports, and current design documents.

Six dark-code findings survive validation:

1. The 1,494-line perception/fold/windowed aggregation capability is extensively tested but has no production invocation path.
2. The older QuantState consolidation pass is orphaned even from tests; its planned scheduler call was explicitly dropped.
3. `ArtifactService` and `MemoryOracleService` are test/library-only; runtime constructs neither service.
4. Four research-domain modules totaling 459 lines are test-only and absent from production candidate/ranking flows.
5. Revision retention is a false operational control: configuration and pruning code exist, but production never schedules or calls the cleanup.
6. Three `RetrievalTuningConfig` knobs are inert; callers can set them without changing behavior.

The first finding is high-severity dark code because a safety-sensitive capability can be mistaken for shipped behavior. Findings 5 and 6 are more direct: public/configured controls silently do nothing. The remaining modules are best described as production-unwired or test-only libraries, not broken algorithms.

## Method and Reachability Model

Runtime roots were derived from `pyproject.toml` and current server construction:

- `menhir.main` / `menhir.__main__`
- `menhir.api.server`
- `menhir.mcp.server`
- `menhir.explorer.app`

The internal AST import graph found 186 `menhir` modules, 167 statically reachable from those roots, and 19 outside them. Five were package markers and one was the alternate `menhir.cli.__main__` launcher. Thirteen modules remained as dark-code leads. One of those, `QuantStateRepository`, is an explicit external compatibility alias and was rejected as dead, leaving twelve production-unwired modules.

Dynamic-path checks found:

- 37 MCP tools registered through `ALL_TOOLS`.
- 9 MCP resources registered through `RESOURCE_TYPES`.
- API routes mounted through `api.server`; internal backend methods use an explicit operation allowlist plus `getattr`.
- No internal plugin loader or entry-point discovery that can activate the candidate modules.
- `importlib` use is confined to Graphiti compatibility patches and dependency probing; it does not load Menhir feature modules.
- No reflection/configuration string references to the candidate module or callable names.

The project-native structural graph was supporting evidence only. It is stale for some recently modified files—for example, it reported no imports inside current `perception.py`—so local source/import analysis is authoritative.

## Dark Code Register

### DC-01 — Perception/fold/windowed aggregation is production-unwired

- Severity: **High**
- Category: production-unwired, hard-to-reach, tested dark code.
- Locations:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\domain\fold_algebra.py:1-268`
  - `...\src\menhir\services\perception.py:1-931`
  - `...\src\menhir\services\event_fold.py:1-105`
  - `...\src\menhir\services\windowed_fold.py:1-102`
  - `...\src\menhir\services\windowed_recall.py:1-88`
- Wiring trace: `perceive_and_fold()` is defined at `perception.py:817-930` and called only from tests. It lazily imports `event_fold`, which reaches `fold_algebra` and `ViewRepository`, but no runtime root reaches `perception`. `answer_windowed_count()` at `windowed_recall.py:62-88` is likewise test-only. None appears in `BuildArtifacts` (`core/bootstrap.py:106-121`), the 37 MCP registrations, nine resources, REST/CLI entry points, scheduler jobs, or backend operation allowlist.
- Test evidence: 84 focused tests across fold algebra, perception, windowed fold, and windowed recall passed in 0.28s.
- Documentation evidence: `windowed_recall.py:9-12` explicitly leaves recall injection to a future product decision. `.agent/memory-review-tracker.md:51-57` marks perception production wiring as designed/planned rather than shipped.
- Impact: approximately 1,494 lines of safety-critical aggregation behavior look production-ready and are documented as architecture, but users cannot reach the capability through supported runtime surfaces. Bug fixes and tests can create false confidence about shipped memory behavior.
- Strongest rebuttal: this is deliberate bench-first development, and the documentation now records the missing production seam. Keeping the deterministic core in the production package reduces later integration cost.
- Disposition: **CONFIRMED dark code, not dead code**. Either wire one explicit, feature-gated product path with end-to-end tests/telemetry, or move the subsystem to an experimental package whose import path and docs cannot be mistaken for active runtime behavior.

### DC-02 — The QuantState consolidator is an orphaned predecessor

- Severity: **Medium**
- Category: orphaned production module; no repository caller.
- Location: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\quantstate_consolidator.py:1-139`.
- Wiring trace: the module is unreachable from every runtime root. `consolidate()` at lines 107-139 has no source or test caller; its internal `extract_events()` and `fold()` are referenced only within the same module. It is not a scheduler task, backend operation, tool, resource, route, bootstrap collaborator, or script entry point.
- Documentation evidence: `.agent/plans/productionize-view-primitives.md:9` explicitly says the consolidator's scheduled call site was dropped and `consolidate()` has no production call site.
- Impact: it overlaps the newer perception/fold design while carrying an older event representation and write path. Maintainers can update the wrong aggregation implementation or assume scheduled counters exist.
- Strongest rebuttal: the module is a precedent/reference implementation and can be invoked manually by an external benchmark caller.
- Disposition: **CONFIRMED repository-orphaned code**. Archive/delete it if the perception pipeline supersedes it, or expose a named experimental command and tests if it remains a supported library. No literal external-use claim is made.

### DC-03 — Artifact application services are test/library-only

- Severity: **Medium**
- Category: production-unwired service layer.
- Locations:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\artifact_service.py:1-117`
  - `...\src\menhir\services\memory_oracle_service.py:1-78`
- Wiring trace: production imports neither service. `BuildArtifacts` constructs ingest, recall, scoring, lifecycle, context, and candidate services (`core/bootstrap.py:106-121,192-221`) but not artifact services. No MCP/API/CLI/scheduler registration constructs them. Current imports are limited to `tests/test_artifact_service.py` and `tests/test_l4_artifact_loop_integration.py`.
- Active alternative: `ArtifactRepository` is composed directly into `MemoryGraphAdapter`; repository/adapter methods remain reachable. The dark code is specifically the application-level single-writer facade and read-only artifact oracle.
- Test evidence: 12 focused artifact-service/integration tests passed in 0.41s.
- Impact: `ArtifactService` documents itself as “the one place application/agent code goes through,” but current application code goes through no such service. Its trust/promotion orchestration therefore does not protect a supported runtime entry point, and `MemoryOracleService` does not influence recall.
- Strongest rebuttal: archived verification documents explicitly describe these as libraries; no artifact MCP product surface was promised.
- Disposition: **CONFIRMED production-unwired library code**. Wire it through a deliberate artifact product boundary or relocate it to an experimental/library namespace and correct the “single writer” claim.

### DC-04 — Four research-domain modules are test-only

- Severity: **Medium**
- Category: test-only research implementation.
- Locations:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\domain\repo_snapshot.py:1-95`
  - `...\src\menhir\domain\structural_expansion.py:1-126`
  - `...\src\menhir\domain\structure_temporal.py:1-174`
  - `...\src\menhir\domain\temporal_intent.py:1-64`
- Wiring trace: no runtime root imports these modules. `structure_temporal` imports `structural_expansion`, but its only external importer is `tests/domain/test_structure_temporal.py`; the other three likewise have test/document consumers only. None is exported through a package facade, used by active `RecallService`, or registered dynamically.
- Test evidence: 30 focused domain tests (the non-artifact portion of the 42-test research/artifact run) passed.
- Impact: 459 lines described as durable identity, structural expansion, time-aware blast radius, and temporal intent are easy to interpret as active retrieval features. Active recall instead uses other paths such as `query_intent.py`, `intent_affinity.py`, file-context injection, and current retrieval oracles.
- Strongest rebuttal: these are falsifiable pure-domain rungs intentionally landed ahead of production integration; test-only status is part of a research ladder.
- Disposition: **CONFIRMED test-only dark code**. Label/package them as experimental or add explicit integration ownership and acceptance gates. Do not delete merely because runtime wiring is absent.

### DC-05 — Revision-retention configuration and cleanup are inert

- Severity: **Medium**
- Category: dark configuration plus test-only maintenance method.
- Evidence:
  - `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\config\settings.py:82-84,244-245` defines and parses `MENHIR_REVISION_RETENTION_DAYS`.
  - `...\src\menhir\infrastructure\telemetry\store.py:740-753` implements `prune_old_revisions()`.
  - The only callers are tests at `tests/test_sidecar_expansion.py:285-340`; no runtime/scheduler/CLI/backend path reads `settings.revision_retention_days` or invokes the pruning method.
- Impact: operators can configure a retention duration that has no effect, while `memory_revisions` continues accumulating. The existence of a tested cleanup method masks missing lifecycle wiring.
- Test limitation: the focused `test_sidecar_expansion.py -k prune_old_revisions` run did not complete within 60.3s in this audit and produced no failing assertion. Current test source was inspected; method behavior itself is not the disputed issue.
- Strongest rebuttal: deployments may run cleanup externally against the local SQLite sidecar. No supported command or documented external scheduler was found.
- Disposition: **CONFIRMED inert control**. Schedule pruning under an owned maintenance job or remove the environment setting until a caller exists. Add an integration test proving configured retention reaches the store.

### DC-06 — Three retrieval-tuning knobs are accepted but ignored

- Severity: **Medium**
- Category: inert feature knobs / dark configuration API.
- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\domain\retrieval_tuning.py:97-117` declares `enable_cross_encoder_rerank`, `rerank_top_n`, and `embedding_dimensions`. Whole-source AST attribute analysis found no consumer outside the dataclass; exact-name searches found no test or runtime read.
- Reachability: callers can construct `RetrievalTuningConfig(enable_cross_encoder_rerank=True, rerank_top_n=..., embedding_dimensions=...)` and pass it to active recall, but `RecallService` never branches on or reads those values.
- Impact: the tuning object advertises behavior that silently does nothing. Experiments can record a configuration as enabled while measuring the unchanged path.
- Strongest rebuttal: these may be reserved schema fields intended to stabilize future benchmark/config serialization. Unlike `CandidateSource.FACET/STRUCTURE`, they are not documented inline as reserved or ungenerated.
- Disposition: **CONFIRMED inert API surface**. Remove/rename them as reserved metadata or implement the behavior with tests that fail when the knob is ignored.

## Accepted Dormant and Compatibility Code

These candidates were checked and are not findings:

- `infrastructure/quantstate_repository.py:1-24`: zero internal importers, but explicitly retained as a `ViewRepository` compatibility subclass for external imports. External usage cannot be disproven from this repository.
- `CandidateSource.FACET` and `CandidateSource.STRUCTURE`: not generated, but `retrieval_tuning.py:31-32` and current plans explicitly label them reserved post-graduation seams. They are dormant vocabulary, not falsely active features.
- `menhir.cli.__main__`: outside the primary static root set but is a valid direct `python -m menhir.cli` launcher.
- Package `__init__.py` modules and empty `pipeline/__init__.py`: package markers, not executable dark code.
- `UnimplementedProviderChatBackend`: actively returned for configured Anthropic use and fails explicitly; it is an unsupported-provider sentinel, not unreachable code.
- Large adapter/store methods with no internal direct caller were not called dead because backend dynamic dispatch, decorators, package APIs, and external callers can reach them.

## Endpoint and Registration Review

- All 37 MCP tool classes are present in `ALL_TOOLS` and registered by both stdio and remote MCP constructors.
- All nine resources are present in `RESOURCE_TYPES` and registered.
- REST and explorer routes are decorator-registered/mounted.
- No access logs were available, so “registered but never used by real clients” was **NOT VERIFIED**. No endpoint is reported dark based solely on absent repository callers.
- The structural scanner's 66-endpoint count contains a known scanner artifact (`...`) and was not used as authoritative registration evidence.

## Verification Commands and Results

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_fold_algebra.py tests/test_perception.py tests/test_perception_generalization.py tests/test_windowed_fold.py tests/test_windowed_recall.py -q -p no:cacheprovider
# exit 0: 84 passed, 2 warnings in 0.28s

.\.venv\Scripts\python.exe -m pytest tests/test_artifact_service.py tests/test_l4_artifact_loop_integration.py tests/domain/test_repo_snapshot.py tests/domain/test_structural_expansion.py tests/domain/test_structure_temporal.py tests/domain/test_temporal_intent.py -q -p no:cacheprovider
# exit 0: 42 passed, 2 warnings in 0.26s

.\.venv\Scripts\python.exe -m pytest tests/test_artifact_service.py tests/test_l4_artifact_loop_integration.py -q -p no:cacheprovider
# exit 0: 12 passed, 2 warnings in 0.41s

.\.venv\Scripts\python.exe -m pytest tests/test_sidecar_expansion.py -q -p no:cacheprovider -k "prune_old_revisions"
# NOT COMPLETED: timed out after 60.3s with no reported failing assertion

.\.venv\Scripts\python.exe -c "from menhir.mcp.tools import ALL_TOOLS; from menhir.mcp.resources import RESOURCE_TYPES; print(len(ALL_TOOLS), len(RESOURCE_TYPES))"
# exit 0: 37 tools, 9 resources
```

Additional evidence: local AST module/import graph, exact-name and dynamic-loading searches, current `BuildArtifacts`, MCP/API/CLI/scheduler registration paths, project-native structural graph, and focused test imports. Coverage.py, vulture, pylint/ruff unused-code rules, production access logs, and external consumer telemetry were **NOT RUN/UNAVAILABLE**.

## Open Questions

- Are artifact services and research-domain modules intentionally supported as importable libraries for external Archolith consumers? If yes, their public support/version contract is undocumented.
- Is QuantState consolidation still a supported manual benchmark path, or has perception fully superseded it?
- Which production surface should own perception and windowed-count invocation when those designs graduate?
- Is revision pruning expected to be performed by an external operator job? No repository contract identifies one.

## Confidence

**91/100** for repository-internal reachability and inert configuration claims. Confidence is lower for absolute deletion decisions because installed Python modules may have external consumers not visible in this repository. Accordingly, the report distinguishes production-unwired/test-only code from literal dead code and preserves explicit compatibility/research seams.
