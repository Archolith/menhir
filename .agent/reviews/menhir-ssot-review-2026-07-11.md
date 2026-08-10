# Menhir deep SSOT review

## Review anchor and coverage

- Repository: `C:\Users\you\IdeaProjects\projects\archolith\menhir`
- Branch: `main`
- HEAD: `81c36c01661d183621d0330f6270759c5ba25bd5` (`fix: also drop None episodes on degenerate EntityEdge`)
- Review date: 2026-07-11
- Review type: read-only, module-by-module single-source-of-truth and ownership audit
- Source coverage: **239/239 handwritten files** under `src/menhir` (**221 Python files, 51,401 lines; 18 Explorer CSS/JS/template files, 1,311 lines**)
- Additional coverage: canonical `.agent` contracts, `README.md`, `.env.example`, package metadata, and risk-selected tests
- Excluded: two vendored Cytoscape bundles, bytecode/cache files, generated artifacts, and dependency code
- Evidence ledger: `menhir-ssot-review-2026-07-11-coverage.csv` contains every reviewed source path, line count, SHA-256, and review method

Every Python file was parsed successfully and included in deterministic scans for definitions, imports, literals, signatures, registries, environment reads, raw Cypher writes, and duplicate function bodies. Every handwritten Explorer asset was included in endpoint/contract and hard-coded-value scans. High-risk hits were then traced manually across their callers, implementations, tests, and canonical documents. This is exhaustive file coverage, not a claim that every line received equal manual scrutiny.

## Executive summary

The deeper pass found **13 actionable SSOT issues: two high, ten medium, and one low**. The first pass materially understated the backend contract, namespace, registry, lifecycle-state, and projection problems.

The most immediate defect is a broken backend contract: the MCP recall tool always passes `include_invalidated`, but `BackendClient.recall` does not accept it. Stdio/backend-client recall therefore raises `TypeError` before making an HTTP request. The second high-severity issue is a namespace isolation failure: `add_memory(namespace=..., memory_type="TEMPORAL")` discards the namespace and persists the temporal record into the shared/default group.

The largest architectural pattern is that several nominal SSOTs exist but are bypassed: `CorrelationService`, `CandidateService`, `TruthAttestation`, `MemorySettings`, `NodeScope.PROMOTED`, repository namespace parameters, feature/concept registries, and shared projections. Menhir has good central abstractions; the risk comes from parallel paths that do not consume them.

## Findings

### SSOT-01 — High — Backend protocol and client signatures disagree; stdio recall is broken

**Modules:** `core`, `mcp`, `api`

- `MemoryBackend.recall` declares `include_invalidated` in `core/backend_protocol.py:90-103`.
- `RuntimeProvider.recall` implements it in `core/backend_impl.py:214-253`.
- `BackendClient.recall` at `core/backend_impl.py:1150` omits it.
- `RecallMemoriesTool.endpoint` always supplies it at `mcp/tools/recall/recall_memories.py:97`, including when the value is the default `False`.
- A direct focused reproduction, `await BackendClient(...).recall("x", include_invalidated=False)`, raises `TypeError: BackendClient.recall() got an unexpected keyword argument 'include_invalidated'` before network I/O.
- All three surfaces expose the same 67 method names, so the existing method-set checks look healthy while signature parity is not enforced.

**Impact:** MCP stdio/backend-client recall cannot execute. Future parameter additions can silently break only one deployment mode.

**Recommendation:** generate or validate provider/client signatures from `MemoryBackend`; add an introspection parity test covering parameter names, keyword-only status, and defaults for all 67 methods. Add `include_invalidated` to `BackendClient.recall` and its request payload.

### SSOT-02 — High — TEMPORAL ingestion drops the requested namespace

**Modules:** `mcp.ingest`, `core`, `infrastructure`, `domain.namespace`

- `add_memory` advertises `namespace` as the operation scope, but its TEMPORAL branch calls `backend.create_temporal` without it at `mcp/tools/ingest/add_memory.py:70`.
- `MemoryBackend.create_temporal`, `RuntimeProvider`, `BackendClient`, `MemoryGraphAdapter`, and `TemporalRepository` have no namespace parameter.
- `TemporalRepository.create_temporal` writes `group_id: ''` at `infrastructure/temporal_repository.py:61`.
- A fake-backend reproduction with `namespace="private-ns"` captured no namespace in the call.

**Impact:** a private temporal memory is stored in the shared/default group. It may disappear from namespace-scoped recall while remaining visible to unscoped/default consumers. This is an isolation and ownership defect, not merely metadata drift.

**Recommendation:** thread namespace through the complete backend contract and persist the canonical `stamped_namespace`/group identifier. Add round-trip tests proving TEMPORAL, TODO, and semantic ingestion have identical namespace behavior.

### SSOT-03 — Medium — Identity-merge policy is duplicated and lifecycle omits a veto

**Modules:** `services`, `infrastructure`

- `CorrelationService._handle_merge_proposal` owns routing, deterministic vetoes, LLM voting, auditing, and merge execution (`services/correlation_service.py:379-557`).
- It checks `ineligible_node_veto`, `co_mention_veto`, and `anchor_project_veto` at lines 395-397.
- `LifecycleService` reimplements the workflow at `services/lifecycle_service.py:338-584`, but checks only co-mention and anchor/project vetoes at lines 435-436.

**Impact:** lifecycle consolidation can merge structural/path-shaped nodes that the canonical correlation path refuses. The copied vote, telemetry, and threshold logic can diverge further.

**Recommendation:** make `CorrelationService` the only owner of pair classification and judgment. Lifecycle should consume a structured result and retain only lifecycle bookkeeping. Test veto parity across both entry paths.

### SSOT-04 — Medium — Recall loses the repository's namespace constraint during adjacency scoring

**Modules:** `services`, `infrastructure`

- `MemoryQueryRepository.fetch_adjacency_pairs(..., namespace=None)` explicitly constrains both endpoints to the namespace at `infrastructure/memory_queries.py:226`.
- `MemoryGraphAdapter.fetch_adjacency_pairs` drops that parameter at `infrastructure/memory_graph_adapter.py:414-422`.
- `RecallService._compute_adjacency` also has no namespace parameter and calls the adapter without one at `services/recall_service.py:455-467,1083`.

**Impact:** initial candidates are namespace-filtered, but context edges/nodes used for structural scoring are not protected by the repository's namespace predicate. Cross-namespace structure can influence ranking, and the intended defense-in-depth capability is unreachable.

**Recommendation:** carry namespace through recall adjacency computation and require it whenever recall is scoped. Add a two-namespace graph test that fails if one namespace affects the other's score.

### SSOT-05 — Medium — Explorer bypasses canonical candidate approval/rejection

**Modules:** `explorer`, `services`, `infrastructure`

- `CandidateService` declares itself the contradiction-checked approval owner (`services/candidate_service.py:6-20`).
- Its approval path promotes then runs contradiction detection (`candidate_service.py:48-80`).
- Explorer performs independent approval/rejection Cypher (`explorer/app.py:229-252`) and exposes it at lines 672-675.

**Impact:** Explorer approval has weaker consistency than backend/MCP approval and creates a second authoritative mutation path.

**Recommendation:** route Explorer through `CandidateService.approve/reject`; remove the local writers; assert Explorer and MCP behavior against one shared service contract.

### SSOT-06 — Medium — Conflict scanning has four competing defaults

**Modules:** `core`, `mcp`, `services`

- Protocol and `LifecycleService` use `150`.
- `RuntimeProvider` and `BackendClient` use `100` (`core/backend_impl.py:615,1341`).
- The MCP module wrapper uses `150` (`mcp/tools/conflict/scan_conflicts.py:8`).
- The registered endpoint uses `500`, while the wrapper documentation also says `500`.

**Impact:** the same operation scans 100, 150, or 500 records depending on entry path; load, latency, and observed conflict counts vary without an explicit caller choice.

**Recommendation:** define one named default in the backend contract/settings and make every wrapper inherit it. Add wrapper-to-endpoint and protocol-to-implementation default-parity tests.

### SSOT-07 — Medium — Runtime configuration is split across incompatible loaders and stale names

**Modules:** `config`, `api`, `mcp`, `infrastructure`, documentation

- `MemorySettings` calls `LOCAL_LLM_*` canonical, but active docs still lead with legacy `LLAMA_*` names.
- Architecture documentation advertises `GRAPHITI_LLM_BASE_URL`, `GRAPHITI_LLM_API_KEY`, `GRAPHITI_LLM_CHAT_MODEL`, `GRAPHITI_EMBED_BASE_URL`, `GRAPHITI_EMBED_API_KEY`, `GRAPHITI_EMBED_MODEL`, and `OPENAI_BASE_URL`; production reads none of them.
- `.env.example` still documents dead `YAWN_MEMORY_MCP_*` and Explorer host/port variables while production uses `MENHIR_*`.
- OAuth and client-token modules reread environment variables instead of consuming the settings snapshot.
- Boolean parsers disagree: with `MENHIR_CLIENT_TOKENS_ENABLED=on`, `MemorySettings.client_tokens_enabled` is `False` while `api.client_token_store.client_tokens_enabled()` is `True`.
- Scheduler metadata reports the raw URL while operational code uses a separate normalizer.

**Impact:** a documented setting can do nothing, and two parts of one process can disagree about whether authentication is enabled.

**Recommendation:** make `MemorySettings` the immutable runtime snapshot for all runtime-affecting options, expose one boolean parser and one endpoint normalizer, and generate/test the env reference from the settings aliases.

### SSOT-08 — Medium — Protected durable state has two representations, one unreachable

**Modules:** `domain`, `services`, `infrastructure`, documentation

- `NodeScope.PROMOTED` is documented as protected durable memory (`domain/models.py:25`, `.agent/data_models.md:30`).
- Cleanup/decay code contains special protection for `scope='PROMOTED'` (`consolidation_queries.py:254-261,768-772`).
- No production path writes or transitions a node to `PROMOTED`.
- The active flag flow writes `user_flagged=true` (`memory_queries.py:321`) and lifecycle moves flagged SESSION nodes to `PERSISTENT`, not `PROMOTED` (`lifecycle_service.py:221` and its promotion branch).

**Impact:** retention policy is encoded both as an effectively dead enum state and as `PERSISTENT + user_flagged`. Queries, metrics, docs, and future migrations can protect/count different sets.

**Recommendation:** choose one representation. Prefer an explicit protection flag/status if protection is orthogonal to lifecycle scope; otherwise make PROMOTED a real transition and derive `user_flagged` behavior from it.

### SSOT-09 — Medium — Truth confidence and envelope ownership are fragmented

**Modules:** `domain`, `domain.truth`, `services`, `infrastructure`, `mcp`

- `domain/truth/kinds.py:91` defines `SOURCE_CONFIDENCE_USER = 1.0`.
- Active `domain/utils.py:58-70` returns `0.9` for `source="user"`; `tests/test_utils.py:19` pins that value.
- `domain/artifacts.py` independently defines trusted confidence as `0.9`, and the utility has source-specific values not named in truth constants.
- `TruthAttestation` and `TruthClaim` call themselves the canonical truth envelope/consumer interface, but production outside `domain` does not consume them. Recall, assertion, and MCP formatting carry their fields independently.

**Impact:** stored trust depends on the path, and the nominal canonical envelope cannot prevent drift because it is not wired into runtime data flow.

**Recommendation:** centralize the complete source-to-confidence mapping in `domain.truth`. Either complete the attestation migration through recall/assertion/formatting or narrow its documented status to an experimental model.

### SSOT-10 — Medium — Tool, feature, endpoint, and concept registries disagree

**Modules:** `mcp`, `explorer`, canonical documentation

- Runtime registers **42 tools and 9 resources**; README and architecture still claim 23 tools.
- Explorer taxonomy maps 36 registered tools, omits 8 (`add_candidate`, `get_provenance`, `list_clients`, `mint_client`, `pause_scheduler`, `resume_scheduler`, `revoke_client`, `view_entropy`), and includes 2 nonexistent tools (`memory_gateway`, `recover_memory`). Unmapped usage is aggregated as `other`.
- `.agent/endpoints.md` has named sections for only 31 of 42 tools; all 9 resources are represented.
- `.agent/concept-ids.yaml` omits multiple active tool concept IDs and `model.todo`; declarations and registry are not completeness-tested.

**Impact:** observability attribution, authorization/review inventories, and canonical documentation describe different callable surfaces.

**Recommendation:** derive taxonomy, counts, endpoint inventory, and concept completeness checks from the runtime registry. Keep human descriptions keyed by generated stable IDs rather than maintaining independent name lists.

### SSOT-11 — Medium — The shared “full memory” projection omits active canonical fields

**Modules:** `infrastructure`, `mcp`, documentation

- `MEMORY_RETURN_FIELDS` is described as the full memory projection and is reused by UUID/recent/flagged/scope/type reads (`infrastructure/cypher.py:232`).
- It omits `processing_substage`, `processing_substage_started_at`, and active LLM task/kind/model/endpoint fields.
- `EPISODE_PROCESSING_FIELDS` includes them at `cypher.py:323-329`; `.agent/data_models.md:66-67` documents the substages.
- Tests check minimum projection sizes and the processing projection independently, but do not assert shared-field parity.

**Impact:** different APIs return inconsistent representations of the same episode, despite both relying on allegedly canonical projections.

**Recommendation:** define one base episode projection and compose view-specific additions. Add an invariant that all canonical processing fields appear in any API advertised as a full memory view.

### SSOT-12 — Medium — Persisted symbol-path identity is implemented twice

**Modules:** `infrastructure.project_scanner`, `infrastructure.structure_queries`

- `_sym_path` in `project_scanner.py:851` and `_symbol_path` in `structure_queries.py:1426` have equivalent bodies; one comment explicitly says it mirrors the other.
- One creates persisted structure identifiers while the other resolves/query-matches them.

**Impact:** a future path-format change can make newly indexed symbols unresolvable by queries without a schema or type error.

**Recommendation:** move symbol path construction/parsing to one domain utility and test scanner/query round trips from the same corpus.

### SSOT-13 — Low — Application version metadata is duplicated and drifting

**Modules:** package, API, Explorer

- `pyproject.toml`, `menhir.__init__`, and the API report `0.2.0`.
- Explorer reports `0.1.0` and retains the old `cth.mcp.memory explorer` title (`explorer/app.py:555`).

**Impact:** operational surfaces report different builds.

**Recommendation:** resolve the installed package version once via `importlib.metadata.version("menhir")`, with a development fallback, and inject it into both apps.

## Lower-risk consolidation opportunities

The duplicate-body scan also found exact or near-exact helpers that are not currently behavioral defects but are useful early warnings:

- ISO timestamp `_parse` in `domain/git_staleness.py`, `domain/structure_temporal.py`, and `domain/temporal.py`.
- `_counter_value_unchanged` in the failure- and instability-counter bridges.
- `_overlap_coefficient` in memory-oracle and retrieval-oracle code.
- `_parse_json_array` in two consolidators.
- `_json_default` in MCP contracts and telemetry storage.
- `_TIER_RANK` in API routes and MCP contracts.
- CLI and MCP temporal-line formatters, with different invalid-timestamp behavior.
- `query_structure` uses a branch chain as its real kind registry; its wrapper and endpoint documentation omit `documents` even though runtime accepts it.

These should be centralized when their modules are next changed, but they do not outrank the contract and isolation defects above.

## Module-by-module disposition

| Module | Assessment | Findings |
|---|---|---|
| Package/root entry points | Thin entry points; copied version values drift. | SSOT-13 |
| `config` | Strong central settings object, but incomplete adoption and competing env parsers. | SSOT-07 |
| `core` | Runtime lifecycle ownership is sound. Backend method names align, but signatures/defaults do not. | SSOT-01, SSOT-02, SSOT-06 |
| `domain` | Useful enums/models; confidence, protection, paths, and nominal truth models have parallel authorities. | SSOT-08, SSOT-09, SSOT-12 |
| `domain.truth` | Good vocabulary, not yet the effective runtime owner. | SSOT-09 |
| `infrastructure` | Repository decomposition and shared Cypher fragments are valuable; adapters drop a namespace capability and projections/path helpers drift. | SSOT-02, SSOT-04, SSOT-08, SSOT-11, SSOT-12 |
| `services` | Most orchestration owners are clear; correlation is copied and candidate approval is bypassed. | SSOT-03, SSOT-05, SSOT-08, SSOT-09 |
| `api` | Auth-mode precedence and operation-tier coverage are strengths; config parsing and tier-rank constants are duplicated. | SSOT-07 |
| `mcp` | Registration is centralized, but backend signature/default, namespace, and external registries are not synchronized. | SSOT-01, SSOT-02, SSOT-06, SSOT-10 |
| `cli` | Mostly consumes shared settings; bootstrap/context still constructs a documented direct Neo4j path and should remain a monitored exception. | Watchlist only |
| `explorer` | Read UI is local by design; mutations, taxonomy, and version metadata are independent authorities. | SSOT-05, SSOT-10, SSOT-13 |
| `pipeline` | Empty compatibility package; no authority detected. | None |
| Templates/static | Presentation-only except feature-name mapping and endpoint coupling already captured above. | SSOT-10 |

## Positive SSOTs to preserve

- `core.runtime` is the effective process lifecycle owner; MCP lifecycle aliases it instead of copying state.
- Auth-mode precedence is centralized in `api.auth_mode`.
- `_BACKEND_METHODS` and both implementations currently contain the same 67 operation names.
- MCP tool/resource registration is code-centralized.
- Embedding-dimension compatibility has an explicit startup owner.
- `ViewRepository`/`ViewKind` provide a clear shared-versus-kind-specific write boundary.
- Shared Cypher projection fragments are the right architecture; they need composition/invariant tightening, not removal.

## Remediation order

1. Fix `BackendClient.recall` and add complete signature/default parity tests.
2. Thread namespace through TEMPORAL creation and recall adjacency; add cross-namespace integration tests.
3. Route lifecycle identity decisions through `CorrelationService`.
4. Route Explorer candidate mutations through `CandidateService`.
5. Establish one conflict-scan default and one settings/env parser.
6. Resolve `PROMOTED` versus `user_flagged`, and truth confidence/envelope ownership.
7. Generate registry inventories and compose canonical projections.
8. Centralize symbol paths and version metadata.

## Verification

Focused existing tests:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_utils.py tests\domain\test_truth.py tests\test_correlation_service.py tests\test_candidate_service.py tests\test_explorer_candidates.py tests\test_settings.py -q
```

Result: **131 passed** in 1.66 seconds, with one third-party Pydantic deprecation warning.

Focused reproductions additionally confirmed:

- `BackendClient.recall(..., include_invalidated=False)` raises the reported `TypeError`.
- TEMPORAL `add_memory(namespace="private-ns", ...)` does not pass namespace to its backend.
- `MENHIR_CLIENT_TOKENS_ENABLED=on` produces conflicting results between `MemorySettings` and the client-token helper.
- Runtime/provider/client method-name sets match at 67, while signature/default comparison exposes the recall and scan-limit drift.
- Runtime registry/taxonomy/endpoint/concept set differences match the counts reported above.

Full unit run status: **not completed**. `pytest -m unit tests -q` was attempted twice and made no visible progress after 68 completed tests before timeouts at 120 and 300 seconds. The apparent next test passes alone in 0.37 seconds, and a verbose rerun also stalled without emitted failure, so this is recorded as an unresolved suite interaction/teardown hang rather than attributed to a specific test.

## Limitations

- No live Neo4j, Graphiti, OAuth provider, or scheduler integration was exercised.
- Vendored/minified JavaScript and generated/cache artifacts were excluded from handwritten-source review.
- The worktree already contained the untracked `.agent/reviews/` directory created for this report; no application source or tests were changed.
