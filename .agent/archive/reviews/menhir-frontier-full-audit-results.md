# Menhir Frontier Full Audit

## Audit Anchor

- Worktree: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier`
- Branch: `claude/menhir-chain-handoff-doc-7iuat2`
- HEAD: `e42e6053efd0466fa094ecea0f01f96ff1d843ce`
- Final filesystem freeze: `2026-07-04T02:08:32.6920396Z`
- Fingerprint: 492 relevant files; SHA-256 `d0bb2884d94d0a5c4be16712e9057dad7c030f302e81a7f01bd9d1227f605688`
- Dirty state: modified `src/menhir/domain/fold_algebra.py`, `src/menhir/services/perception.py`, `tests/test_fold_algebra.py`, and `tests/test_perception_generalization.py`; three untracked design/plan documents and four untracked evaluation scripts. All are included in scope. The audit did not modify source or tests.
- Environment: Windows/PowerShell, Python 3.12.10, project `.venv`, clean `pip check`. Neo4j/Graphiti/model-provider/Langfuse online execution was not used.

The source changed during the audit without a HEAD change. Phase 11 invalidated the affected evidence, re-froze the filesystem, reran the affected tests, and removed a now-fixed candidate. A retrieval design reference appeared during the final check and was also validated. The final target is this dirty filesystem state, stable across two checks more than 15 seconds apart, not HEAD alone.

## Executive Summary

Five unique current defects survived independent validation:

1. Direct/non-local model-provider deployments can be fully operational yet never start Menhir's maintenance scheduler.
2. A maintenance job longer than the 90-second lease can run concurrently with a second leader.
3. Forced scheduler takeover can overlap the prior owner's active work.
4. A readonly bearer credential can invoke destructive REST and hidden backend operations because tiers are authenticated but not authorized there.
5. An agent-tier remote MCP client can make the service read and persist arbitrary host-readable files/directories because ingestion has no allowed-root boundary.

The first, second, and fourth have the highest demonstrated impact. The offline suite is also red from six stale mocks/expectations, although those failures do not show a production crash. Operational documentation materially disagrees with current commands, registered surfaces, default feature flags, models, data contracts, and worktree status.

No production performance defect, dependency CVE, prompt-injection exploit, secret leak, WCAG violation, or legal violation was proven. The strongest unconfirmed risks are Law-3 aggregation independence, shutdown ordering, retrieved-content trust boundaries, asynchronous project-write acknowledgement, telemetry retention, and cross-store consistency.

Overall confidence: **84/100** for the validated code/document findings; lower for live dependency, performance, accessibility, supply-chain, and legal conclusions.

## Documentation-to-Code Conformance

The 40-row claim matrix found the architecture narrative broadly aligned with the runtime split, but the operational contract is not reliable in several material areas:

- `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\README.md:119-125` uses the wrong repository path and an undeclared `.[dev]` extra.
- `README.md:157-182` gives the wrong stdio MCP invocation and explorer command.
- `README.md:80-83`, `.agent\architecture.md:118-149`, `.agent\endpoints.md:80-399`, and the handoff describe 23 tools; runtime registers 37 tools and 9 resources. The endpoint document omits live write/destructive tools.
- `.env.example:50-59` and comments in `src\menhir\config\settings.py:163-169,271` say frontier features default off; executable defaults at `settings.py:108-127` enable oracle ranking, intent lens, evidence anchor, and shadow.
- `.agent\plans\chain-handoff.md` contradicts itself on IntentOracle integration, claims a clean/pushed state despite current dirty files, and says tests cannot run despite the working `.venv`.
- `.agent\workflows\run_and_test.md:7-37` uses the wrong root and nonexistent dependency/wrapper files and misstates temporary-directory behavior.
- `.agent\data_models.md:19-90` is incomplete as a canonical model contract for current namespace, belief/temporal, artifact/evidence, candidate, view/counter/timeline, fold/perception, and retrieval-trace surfaces.
- `.env.example:22-29` and `settings.py:201-204` disagree on the absent-variable OpenAI chat model default.
- The untracked perception remediation plan still says `PLANNED`, although the final dirty source implements it and 63 focused tests pass.

These are Documentation Defects, not merely editorial drift, because they affect startup, attack-surface inventory, default behavior, testing, or integration.

## Confirmed Defects

### High - Maintenance is coupled to local model endpoint ownership

- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\core\runtime.py:115-146,376-427,468-480`.
- Behavior: `uses_scheduler` describes whether Graphiti uses the external local model scheduler, but it also gates Menhir's internal `MaintenanceScheduler`. A supported direct OpenAI configuration can be enrichment-ready with `uses_scheduler=False`, so periodic stale-lease recovery, failed retry, queue observation, conflict work, structure refresh, and counter sync never start.
- Reachability: supported non-local provider configuration; no alternative maintenance owner is selected.
- Impact: stalled recovery and permanently skipped maintenance in an otherwise healthy deployment.
- Fix direction: separate Menhir maintenance ownership from model-process acquisition/watchdog logic.

### High - A long job outlives the scheduler lease

- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\maintenance_scheduler.py:73-75,221-269,275-323`; `...\services\scheduler_lease.py:60-126`.
- Behavior: A renews its 90-second lease, awaits sequential jobs without a heartbeat/timeout, B acquires the expired row, and A continues mutating until the job returns.
- Reachability: provider and Graphiti work longer than 90 seconds is supported normal behavior.
- Impact: duplicate/conflicting queue, lifecycle, conflict, structure, or counter work; single-leader invariant fails.
- Fix direction: heartbeat while jobs run, fence writes by lease generation, and bound/cancel jobs.

### Medium - Forced takeover overlaps active scheduler work

- Evidence: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\maintenance_scheduler.py:110-166,221-240`; `...\services\scheduler_lease.py:159-205`.
- Behavior: B overwrites A's lease and starts; A is not signalled or fenced and notices only after its current batch.
- Impact: concurrent mutation during an operator-triggered recovery action.
- Fix direction: cooperative handoff plus fencing/termination confirmation before B executes.

## Security and Privacy Defects

### High - Readonly tokens can perform destructive REST/backend operations

- Attacker: holder of a configured readonly bearer credential.
- Preconditions: tiered auth enabled and main API reachable.
- Path: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\api\auth.py:52-60,135-153` authenticates and binds a tier; `...\api\routes.py:297-358,387-470` invokes write/delete routes and a broad hidden backend allowlist without tier authorization. MCP-only enforcement at `...\mcp\contracts.py:190-213` does not protect these paths.
- Reproduction trace: `DELETE /api/memory/{uuid}` or `POST /api/internal/backend/delete_namespace` with a valid readonly token reaches the backend. Scheduler control, conflict resolution, TODO deletion, and candidate mutation are also allowlisted.
- Assets/impact: memory and namespace integrity/availability, scheduler/control-plane state.
- Strongest rebuttal: REST bearer holders may all be trusted. That conflicts with the documented readonly dashboard/integration tier.
- Remediation: central operation-to-tier authorization for public REST and backend RPC; default destructive operations to operator.

### Medium - Agent ingestion crosses the host filesystem trust boundary

- Attacker: compromised or scoped remote MCP client with an agent credential.
- Preconditions: remote MCP exposed and service account can read the target.
- Path: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\mcp\tools\ingest\ingest_document.py:41-65` accepts caller paths; `...\core\backend_impl.py:351-389,391-465` reads/scans them without allowed-root or symlink containment and persists excerpts/narrative.
- Assets/impact: host-readable source, configuration, `.env`-style files, and other text can be persisted and recalled or sent to providers/traces.
- Strongest rebuttal: agent clients may be as trusted as the host user. The documented scoped tier and remote transport do not make that assumption safe.
- Remediation: configured allowed roots, canonical/symlink validation, operator-only arbitrary paths, and default secret/config exclusions.

## Design Risks

- **Law-3 aggregation independence (Medium):** `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\perception.py:506-541,681-714` can treat post-anchor mentions as additive while treating the anchor as successful triangulation. Optional verification may reject errors but representative model behavior was not run.
- **Shutdown ordering (Medium):** `src\menhir\core\runtime.py:272-343` closes graph-facing dependencies before draining the scheduler. A running job can fail, delay shutdown, or partially complete.
- **Telemetry retention/privacy (Medium):** `src\menhir\infrastructure\telemetry\store.py:38-42,277-341,425-450,740-765` stores bounded payload previews; revision pruning does not establish retention for all append-only telemetry tables.
- **Retrieved-content instruction boundary (Medium):** document/memory content is returned to downstream agents without a machine-enforced data-versus-instruction boundary. Menhir does not itself execute it, so this is not a reproduced prompt-injection exploit.
- **Asynchronous scan acknowledgement (Medium):** `src\menhir\core\backend_impl.py:436-470` returns before the Neo4j structure write completes; failure reaches only logs and a later session warning.
- **Cross-store consistency (Low):** graph mutations and SQLite audit/suppression writes are not one transaction.
- **Unversioned compatibility contract (Low):** public paths and serialized contracts have aliases but no explicit version/deprecation or old-data compatibility policy.
- **Context-window retry classification (Low):** `src\menhir\infrastructure\episode_lifecycle.py:20-36,318-323` relies on free-text markers.
- **Retrieval score-scale coupling (Medium):** `src\menhir\services\scoring_service.py:47-63,81-95` applies a 0.15 floor to Graphiti RRF scores and explicitly depends on the current `rank_const=1` scale; `src\menhir\domain\retrieval_tuning.py:50-68` still describes priors in cosine terms. No current ranking regression was reproduced.
- **Retrieval self-reinforcement (Low):** `src\menhir\services\recall_service.py:484-514,1168-1173` touches returned nodes and increments traversed edges, feeding future recency/prominence. `update_access=False` protects probes; harmful amplification was not measured.

## Hardening Opportunities

- **Explorer authentication:** `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\explorer\app.py:618-629` has no auth. Default loopback binding prevents classification as a default-remote defect; rebinding/proxying exposes graph/telemetry reads and candidate mutations.
- **Constant-time token comparison:** `src\menhir\api\auth.py:52-60` uses ordinary equality. Practical remote extraction was not demonstrated.
- **Neo4j transport posture:** the former default password has been fixed (`src\menhir\config\settings.py:45-48` now uses empty), but `bolt://` remains the default and driver creation does not force encryption.
- **Ordinary-memory promotion:** candidate/artifact paths have more explicit review gates than caller-provided ordinary memory source/confidence. No forged-confidence promotion was reproduced.

## Test Gaps

- The release gate is red: six final-snapshot tests fail because mocks/expected exports were not updated for `include_superseded`, `occurred_at`, and `FACT_TEMPORAL_FIELDS`. Production backends accept the new contract, so this is a test regression, not proof of product failure.
- No `.github/workflows` directory or documented external CI currently enforces the 1,871-test suite.
- Coverage.py/pytest-cov is unavailable and no current line/branch coverage artifact exists.
- Specific high-value missing tests: direct-provider runtime starts maintenance; a job exceeding lease duration cannot create two writers; readonly REST/backend writes are rejected; ingest paths outside allowed roots are rejected; Law-3 evidence is causally independent.
- `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\tests\test_scaffold.py:13` is a trivial `assert True`.
- Online Neo4j/Graphiti/provider behavior, chaos/failure injection, mutation testing, randomized ordering, and multi-process lease stress were not run.

## Dark Code and Line-Count Findings

- **Production-unwired dark code (Medium):** `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\perception.py:1-879` and the fold subsystem have tests/internal calls but no core/API/MCP/CLI registration, dynamic dispatch, reflection, or plugin path. It is an intentional bench-first prototype, not dead/deletable code.
- **509-line orchestration hotspot (Medium):** `src\menhir\services\recall_service.py:752-1260` combines retrieval, metadata/evidence, scoring, feature flags, traces, context packing, and access mutation. Its explicit ordering and tests are counter-evidence; the risk is change collision and combination complexity.
- **Protocol duplication (Medium):** `src\menhir\core\backend_protocol.py` and 1,611-line `src\menhir\core\backend_impl.py` mirror broad provider/client signatures. The six stale tests demonstrate propagation debt.
- Other measured production hotspots include `structure_queries.py` 1,430 lines, `recall_service.py` 1,260, telemetry store 1,163, project scanner 1,143, Graphiti client 1,128, enrichment steps 1,122, memory graph adapter 949, lifecycle 933, and ingest 929. Counts alone are not findings.

## Performance Evidence

- Three synthetic full scans of this repository measured 9.842s, 10.082s, and 9.876s; each produced 500 files, 4,374 symbols, 770 imports, and 2,120 call edges. Python `tracemalloc` peak was 14,582,753 bytes.
- `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\core\backend_impl.py:391-423` performs the scan before checking the stored fingerprint, so an unchanged full scan still pays this cost. This is a measured background-cost risk, not a demonstrated SLO violation.
- The offline suite took 589.02s in the disposable copy; this is developer feedback cost, not production latency.
- Neo4j `EXPLAIN`, recall/ingest profiling, provider token/cost telemetry, startup/load testing, remote MCP payload profiling, and large-repository stress were **NOT RUN**. No production performance defect is reported.

## Compliance and Governance Gaps

### Code/repository evidence

- Graph deletion exists, but end-to-end deletion/export/correction does not coordinate SQLite payload previews, provider/Langfuse copies, caches, logs, or backups. Legal significance depends on deployment and data roles.
- No root LICENSE, NOTICE, SBOM, or automated license policy is present. README says private. Full transitive license scanning was unavailable; no license violation is claimed.
- Model/provider configuration, benchmark notes, truth policies, and telemetry exist, but there is no consolidated deployed model/version inventory tied to purpose, limitations, evaluation, approval, rollback, incident handling, and provider data handling.

### Standards and legal scope

- The governance comparison used [NIST AI RMF 1.0](https://www.nist.gov/itl/ai-risk-management-framework), released 2023-01-26; it is voluntary and gaps are not violations.
- LLM/agentic threat mapping used [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/) and [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/).
- The [EUR-Lex summary for Regulation (EU) 2024/1689](https://eur-lex.europa.eu/legal-content/en/LSU/?uri=oj:L_202401689) states general application from 2026-08-02 with staged exceptions. Menhir's role, market placement, and risk classification are not established by repository evidence; counsel/owner review is required before any applicability conclusion.
- Explorer accessibility scope is developer UI only. Automated axe/Lighthouse/WAVE and manual keyboard/screen-reader checks were **NOT RUN**; no WCAG failure is claimed.

## Open Questions

- Which component is intended to own maintenance in direct OpenAI/Gemini deployments?
- Are agent-tier remote clients explicitly trusted as the host filesystem principal, and what roots are legitimate?
- What is the deployment boundary for the explorer and main API (loopback, reverse proxy, or remote network)?
- What retention/deletion/export obligations apply to graph, telemetry, provider, Langfuse, cache, log, and backup copies?
- What performance targets apply to recall, ingestion, project scans, and maintenance jobs? Query plans and representative telemetry are absent.
- Is the perception/fold subsystem intended for production wiring, experimental removal, or separate packaging?
- Should aggregate-intent queries deterministically inject current Views, or is trace-only reachability the intended contract?
- Which external CI, if any, enforces the suite?

## Rejected or Downgraded Candidates

- **FC-01 fixed:** the former category-first dedup undercount is not current. Final dirty source uses identity/wording (`src\menhir\domain\fold_algebra.py:84-97`), routes ambiguous clusters (`fold_algebra.py:125-160`), and vetoes unresolved coreference (`src\menhir\services\perception.py:656-679`). Focused result: 63 passed.
- **Six failures downgraded:** stale test doubles/expected exports do not prove REST/MCP production crashes.
- **Secret findings rejected:** three Gitleaks generic-key detections were documentation/throwaway examples, not verified production secrets. Values are not reproduced.
- **CVE claims unsupported:** `pip-audit` was unavailable; no resolved affected version/advisory proof exists.
- **Prompt-injection defect downgraded:** retrieved content creates a downstream trust risk, but no poison-to-tool/memory-write exploit was reproduced.
- **Performance defects unsupported:** static round-trip/query concerns lack query plans, representative load, SLOs, or production telemetry.

## Prior Finding Revalidation

Every item in `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\.agent\verified-current-findings.md` was revalidated:

- Forced takeover overlap: **confirmed**, merged into the single Medium scheduler defect.
- Neo4j unsafe defaults: **changed**. Default password issue is fixed; unencrypted deployment posture remains hardening.
- Explorer no auth: **confirmed as hardening** because default bind is loopback.
- Telemetry growth/caller limits: **confirmed as design risk**, with bounded previews and revision-only pruning noted.
- Context-window free-text retry matching: **confirmed as design risk**, not a reproduced failure.

The untracked perception dedup/veto plan's product concern is fixed in the final filesystem. The Law-3 plan remains a design/evaluation risk.

## Coverage Summary

- Reviewed: 325 Python files by AST inventory; all major entry points, core runtime, backend protocol/provider/client, REST/auth, MCP tool/resource contracts, explorer, scheduler/lease, ingest/enrichment/recall/lifecycle/conflict/candidate/artifact/namespace/TODO/view/fold/perception flows, Neo4j/SQLite boundaries, settings/package/runbooks, and relevant tests.
- Generated/introspected: OpenAPI 3.1.0 with 10 public paths and 14 schemas; 37 MCP tools; 9 resources/templates; 1,871 collected tests.
- Structural graph supporting evidence: files, endpoints, dependencies, imports, tests, blast radius, affected tests, 4,041 symbols, 719 import edges, and 1,908 call edges in the initial project-native index. Current-file verification overrode stale index details.
- Executed: full offline suite in a disposable copy, direct rerun of six substantive failures, focused perception/fold regression suite, scanner timing/allocation, package health, registration/OpenAPI introspection, and full-history secret scan.
- Partial/skipped: online Neo4j/Graphiti/model provider, Langfuse, live REST deployment matrix, dependency advisory scan, coverage/mutation, SBOM/license scan, chaos/concurrency stress, query plans, WCAG tooling, production telemetry/data, and legal applicability.

## Verification Commands and Evidence

Commands were run from `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier` unless noted.

```powershell
.\.venv\Scripts\python.exe --version
# exit 0: Python 3.12.10

.\.venv\Scripts\python.exe -m pip check
# exit 0: No broken requirements found.

.\.venv\Scripts\python.exe -m pytest --collect-only -q
# exit 0: 1,871 tests collected

# Disposable copy under C:\tmp\menhir-frontier-audit\run-e42e605
.\.venv\Scripts\python.exe -m pytest -m "not online" -q -p no:cacheprovider
# exit 1: 1,834 passed, 7 failed, 30 deselected, 3 warnings in 589.02s
# one failure was copy-path-specific; six reproduced in the source worktree

.\.venv\Scripts\python.exe -m pytest tests/test_api_routes.py::TestRecall::test_recall_basic tests/test_api_routes.py::TestRecall::test_recall_with_preset tests/test_api_routes.py::TestIngest::test_ingest_with_explicit_session tests/test_cypher.py::TestExports::test_all_exports tests/test_mcp_server.py::test_resource_templates_return_filtered_data tests/test_mcp_server.py::test_read_flagged_then_recall_context_flow -q -p no:cacheprovider
# exit 1: 6 failed, 3 warnings in 3.25s

.\.venv\Scripts\python.exe -m pytest tests/test_fold_algebra.py tests/test_perception.py tests/test_perception_generalization.py -q
# exit 0: 63 passed, 1 warning in 0.21s

.\.venv\Scripts\python.exe -c "from menhir.mcp.tools import ALL_TOOLS; from menhir.mcp.resources import RESOURCE_TYPES; from menhir.api.server import create_app; s=create_app().app.app.openapi(); print('tools={} resources={} openapi={} paths={} schemas={}'.format(len(ALL_TOOLS),len(RESOURCE_TYPES),s.get('openapi'),len(s.get('paths',{})),len(s.get('components',{}).get('schemas',{}))))"
# exit 0: tools=37 resources=9 openapi=3.1.0 paths=10 schemas=14
```

Fingerprint method: sorted tracked plus untracked relevant files, each represented as `path<TAB>lowercase SHA-256`, LF-joined, then SHA-256 of the UTF-8 manifest. Two final checks more than 15 seconds apart matched. Phase 3 secret-scan artifact: `C:\tmp\menhir-frontier-audit\phase3\gitleaks.json` (sensitive values not reproduced).

**NOT RUN:** online tests, live Neo4j/Graphiti/provider/Langfuse operations, `pip-audit`, coverage.py/pytest-cov, pip-licenses/SBOM generation, Semgrep/CodeQL, mutation tests, multi-process scheduler stress, Neo4j `EXPLAIN`, production profiling/load, and accessibility automation.

## Audit Reliability

Confidence is **84/100** for the validated repository findings. Authorization, path ingestion, scheduler ownership, lease/takeover traces, documentation contradictions, test failures, registration counts, and the FC-01 fix are directly supported by current code and reproducible local commands.

Limits: live dependencies and deployment topology were unavailable; full coverage/advisory/license/accessibility tooling was not installed; no production data or production memory was used; performance evidence is limited to synthetic local scanning; AI/legal conclusions are intentionally conservative.

Snapshot drift is explicitly resolved. The final fingerprint is stable, affected phases were rerun, stale line/count references were updated, and the former FC-01 defect was removed. The source worktree remains dirty because those changes belong to another actor; this audit performed no source/test/git mutation.

## Remediation Status (2026-07-06)

The five confirmed defects have been fixed on branch `claude/menhir-chain-handoff-doc-7iuat2` (menhir-frontier repo):

1. Maintenance coupled to local model-endpoint ownership (AR-01) -> `1858e9c` (gate on `enrichment_ready`).
2. Job longer than the 90s lease runs concurrently with a second leader (AR-02) -> `1858e9c` (background heartbeat).
3. Forced-takeover overlap (FC-02/AR-04) -> `1858e9c` (heartbeat lease-lost + batch fence).
4. Readonly credential reaches destructive REST/backend (SEC-01) -> already fixed post-audit in `4130e95` (tier authorization at REST routes + backend dispatch + MCP tools).
5. Unbounded agent-tier ingest paths (SEC-02) -> `7bceb9a` (allowlisted roots + operator bypass).

The documentation defects, the six stale-mock test failures (TQ-01), and the unconfirmed risks (Law-3 aggregation, shutdown ordering, telemetry retention, cross-store consistency, perception dark code, etc.) remain open and are tracked in `menhir-frontier/.agent/verified-current-findings.md`. The raw per-lane audit files (`menhir-frontier-00` through `-10`) have been archived as superseded by this consolidated report and the validated-findings ledger.
