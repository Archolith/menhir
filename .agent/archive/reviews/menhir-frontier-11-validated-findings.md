# Menhir Frontier Phase 11 - Validated Findings

## Validation Anchor

- Worktree: `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier`
- Branch: `claude/menhir-chain-handoff-doc-7iuat2`
- HEAD: `e42e6053efd0466fa094ecea0f01f96ff1d843ce`
- Final filesystem freeze: `2026-07-04T02:08:32.6920396Z`
- Relevant files: 492; aggregate SHA-256 `d0bb2884d94d0a5c4be16712e9057dad7c030f302e81a7f01bd9d1227f605688`
- Dirty state: four tracked perception/fold files modified and seven relevant design/plan/evaluation files untracked. Two fingerprint checks more than 15 seconds apart matched.
- Environment: Python 3.12.10; project `.venv`; `pip check` clean; online Neo4j, Graphiti, model-provider, and Langfuse execution not used.

The verifier rejected the earlier `45dcf988...` freeze after source changed without a HEAD change. It re-opened the affected code, reran the perception/fold tests, and updated affected phases. A new untracked retrieval design reference appeared during the final check; its material claims were independently checked against current scoring/recall code before accepting the final filesystem state below.

## Confirmed Defect Ledger

| Candidate | Final classification / severity | Exact evidence and reachable behavior | Rebuttal, tests, environment, duplicates | Disposition |
|---|---|---|---|---|
| AR-01 | Confirmed Defect / High | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\core\runtime.py:115-146,376-427,468-480`. Menhir computes `uses_scheduler` from model endpoint ownership and starts `MaintenanceScheduler` only when that value is true. A supported direct OpenAI Graphiti configuration can be fully enrichment-ready while skipping periodic stale-lease recovery, failed retry, conflict work, structural refresh, and counter sync. | No alternate maintenance owner is selected or documented for direct providers. Scheduler unit tests construct the scheduler directly; no runtime provider-combination test contradicts the trace. Supported non-local provider mode; live provider start was NOT RUN. | CONFIRMED. Decouple model-process management from Menhir maintenance ownership. |
| AR-02 | Confirmed Defect / High | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\maintenance_scheduler.py:73-75,221-269,275-323`; `...\services\scheduler_lease.py:60-126`. The 90-second lease is renewed only before a sequential batch of unbounded awaited jobs. If A runs longer than 90 seconds, B can atomically acquire the expired row while A continues mutating shared state. | Fast/idempotent jobs reduce likelihood, but provider/Graphiti calls longer than 90 seconds are supported and not all multi-step state changes are fenced. Existing tests do not run a job past lease expiry. Multi-process stress was NOT RUN; the interleaving is complete from current code. | CONFIRMED. Heartbeat during work and fence writes by lease generation. |
| FC-02 / AR-04 | Confirmed Defect / Medium | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\maintenance_scheduler.py:110-166,221-240`; `...\services\scheduler_lease.py:159-205`. Force takeover overwrites an active owner and immediately starts B; A is neither signalled nor fenced and notices loss only after its current due-job batch. | Operator-only use and partial idempotence reduce likelihood, not the overlap. Transfer/status tests do not prove old in-flight work stopped. Duplicate candidates merged. | CONFIRMED once. |
| SEC-01 / EC-01 / DOC-10 | Security/Privacy Defect / High | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\api\auth.py:52-60,135-153`; `...\api\routes.py:297-358,387-470`; contrast `...\mcp\contracts.py:190-213`. A holder of a configured readonly key is authenticated and its tier is bound, but REST delete/write handlers and hidden backend RPC never authorize the operation. Requests such as `DELETE /api/memory/{uuid}` or backend `delete_namespace`, scheduler control, conflict resolution, TODO deletion, and candidate mutation reach the backend. | Preconditions: tiered auth configured and main API reachable. Hiding the backend route is not authorization. Tests prove tiers authenticate but contain no negative readonly REST/backend test. The claim that REST bearer holders are all trusted contradicts the documented readonly dashboard/integration role. EC-01 and DOC-10 are duplicates/affected documentation. | CONFIRMED. Attacker: readonly credential holder. Assets: memory integrity/availability, namespaces, scheduler/control state. Enforce an operation-to-tier policy at REST/backend dispatch. |
| SEC-02 | Security/Privacy Defect / Medium | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\mcp\tools\ingest\ingest_document.py:41-65`; `...\core\backend_impl.py:351-389,391-465`. Agent-tier ingest accepts caller paths, reads a file verbatim or scans a directory with no configured root/symlink containment, persists excerpts/narrative, and makes them recallable. | Preconditions: remote MCP exposed, valid agent credential, service account can read target. OS permissions and authentication constrain impact; the tool is intended for project ingestion. That does not make an agent-tier client equivalent to the host filesystem principal. No arbitrary-path denial test was found. | CONFIRMED. Attacker: compromised/scoped agent client. Asset: host-readable source/configuration/secrets. Resolve paths under allowlisted roots and reserve arbitrary paths for operator. |

## Documentation Defect Ledger

| Candidate | Final classification / severity | Contradiction and evidence | Rebuttal / disposition |
|---|---|---|---|
| DOC-01 | Documentation Defect / Medium | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\README.md:119-125` points to `projects/archolith/menhir` and `.[dev]`; the frozen root is `menhir-frontier`, and `pyproject.toml:1-33` has a dependency group but no `dev` extra. | Copy/paste onboarding fails. CONFIRMED. |
| DOC-02 | Documentation Defect / Medium | `README.md:157-176` says `python -m menhir` runs stdio MCP; `src\menhir\__main__.py:1-3` invokes Typer, while stdio MCP is `src\menhir\mcp\server.py:53-61`. | The CLI may be useful, but it is not the documented server. CONFIRMED. |
| DOC-03 | Documentation Defect / Low | `README.md:178-182` says `yawn-memory-explorer`; `pyproject.toml:25-27` registers `menhir-explorer`. | No compatibility script was found. CONFIRMED. |
| DOC-04 | Documentation Defect / Medium | `README.md:80-83`, `.agent\architecture.md:118-149`, `.agent\endpoints.md:80-399`, and `.agent\plans\chain-handoff.md:139-143` state or enumerate 23 tools. Runtime registration produced 37 tools and 9 resources; the endpoint document omits live write/destructive tools. | Historical counts do not describe the current attack/contract surface. CONFIRMED. |
| DOC-05 / EC-02 / MA-04 | Documentation Defect / Medium | `.env.example:50-59` and `src\menhir\config\settings.py:163-169,271` say frontier defaults are off; executable defaults at `settings.py:108-127` enable oracle ranking, intent lens, evidence anchor, and shadow. | Code is authoritative; adjacent comments do not override values. Duplicates merged. CONFIRMED. |
| DOC-06 | Documentation Defect / Medium | `.agent\plans\chain-handoff.md:456-462,521-531,584,689-700` calls IntentOracle design-only/not wired while current code and the same document also call integration complete. Lines 5-7 claim all work committed/pushed, but the final snapshot has four tracked modifications and six relevant untracked files. Lines 72-80 say tests cannot run despite a working `.venv`. | Some passages are historical, but the handoff does not reliably distinguish historical from current state. CONFIRMED. |
| DOC-07 | Documentation Defect / Medium | `.agent\workflows\run_and_test.md:7-37` uses the wrong root, nonexistent `requirements.txt`, and nonexistent `scripts/menhir.ps1`; `tests\conftest.py:19-24` uses a different temp location. | The marker commands themselves exist, but the setup/isolation path fails. CONFIRMED. |
| DOC-08 | Documentation Defect / Low | `.agent\data_models.md:19-90` omits active namespace, belief/temporal, artifact/evidence, candidate, view/counter/timeline, fold/perception, and retrieval-trace contracts present in current domain/repository/schema modules. | It documents the core, not a safe canonical full contract. CONFIRMED as incomplete canonical documentation. |
| DOC-09 | Documentation Defect / Low | `.env.example:22-29` defaults OpenAI chat to `gpt-4.1-nano`; `src\menhir\config\settings.py:201-204` inherits dataclass default `gpt-4.1-mini` when unset. | An explicit environment value avoids drift; absent-variable behavior still contradicts the template. CONFIRMED. |
| C36 plan status | Documentation Defect / Low; former FC-01 stale | `.agent\plans\perception-dedup-signature-and-veto-receipts.md:7-38` remains `PLANNED`, but final dirty source implements identity-first dedup, ambiguous candidate routing, tri-state coreference, and unresolved veto at `src\menhir\domain\fold_algebra.py:84-160` and `src\menhir\services\perception.py:381-433,656-679`. | 63 focused tests pass. Plan status stale; former product defect is not current. |

## Validated Non-Defect Risks and Gaps

| Candidate | Final classification / severity | Evidence, scenario, rebuttal, test status | Disposition |
|---|---|---|---|
| FC-R01 / C37 | Design Risk / Medium | `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\src\menhir\services\perception.py:506-541,681-714`. Law-3 can treat every post-anchor mention as additive; its anchor marks triangulation satisfied and suppresses the holistic check in that path. Optional final verification may reject it, but is default-off and representative live-model behavior was NOT RUN. | VALIDATED RISK, not a defect. Add causally independent corroboration/evaluation. |
| AR-03 | Design Risk / Medium | `...\src\menhir\core\runtime.py:272-343` closes ingest/recall/Graphiti/Neo4j before `_stop_scheduler`; `maintenance_scheduler.py:168-184` waits for active work. A shutdown during a graph job can fail, delay, or partially complete work. No injected reproduction; event-loop serialization is counter-evidence. | VALIDATED RISK. Drain scheduler first. |
| AR-R01 | Design Risk / Low | Neo4j state changes and SQLite audit/suppression writes are separate calls/transactions; a SQLite failure may leave graph state without audit/suppression. Best-effort telemetry may be intended; live rediscovery NOT RUN. | VALIDATED RISK. |
| SEC-R01 / prior C | Privacy/Operational Design Risk / Medium | `...\src\menhir\infrastructure\telemetry\store.py:38-42,277-341,425-450,740-765` stores bounded payload previews and caller-selected read limits; revision pruning exists, but no general retention for all append-only event tables was found. Bounded previews and local SQLite reduce impact; no production data inspected. | VALIDATED RISK. |
| AI-R01 | Design Risk / Medium | `...\src\menhir\core\backend_impl.py:351-389` sends document narrative into memory flows; recall/context preserves retrieved content but provides no machine-enforced data-versus-instruction boundary for downstream agents. Menhir itself does not execute retrieved instructions, and no poison-to-tool chain was reproduced. | VALIDATED RISK, not prompt-injection defect. |
| AI-R02 | Duplicate risk | Least-agency concern is fully represented by SEC-01 and SEC-02. | MERGED; no separate finding. |
| AI-R03 | Hardening Opportunity / Low | Ordinary memory ingestion accepts caller source/source-confidence with less explicit promotion control than candidate/artifact flows. Lifecycle/truth layers are counter-evidence; no forged-confidence promotion was reproduced. | VALIDATED HARDENING. |
| AI-R04 / C39 | Design Risk / Low | `...\src\menhir\services\recall_service.py:484-514,1168-1173` touches returned nodes and increments traversed edges; future recency/prominence can therefore reinforce past results. `update_access=False` supports pure probes, and no harmful ranking drift was measured. | VALIDATED RISK. |
| AI-G01 / CG-02 | Governance Evidence Gap | Environment-driven model/provider configuration lacks a consolidated deployed model/version, purpose, evaluation, approval, rollback, incident, and data-handling record. NIST AI RMF is voluntary; this is not a legal violation. | VALIDATED GOVERNANCE GAP. |
| EC-R01 | Design Risk / Medium | `...\src\menhir\core\backend_impl.py:436-470` acknowledges scan metadata after `asyncio.create_task`; a later failure is only logged/queued for a future request. Async acknowledgement is intentional and documented in code. | VALIDATED RISK. |
| EC-R02 | Design Risk / Low | Public REST paths are unversioned and no explicit compatibility/deprecation policy or old-data fixture suite was found. Package version 0.2.0 reduces stability expectation. | VALIDATED RISK. |
| MA-01 | Maintainability Design Risk / Medium | `...\src\menhir\services\recall_service.py:752-1260`: `RecallService.recall` is 509 lines and owns candidate retrieval, metadata/evidence, scoring, frontier toggles, traces, packing, and access mutation. Explicit sequencing and tests are counter-evidence. | VALIDATED RISK. |
| MA-02 | Maintainability Design Risk / Medium | `...\src\menhir\core\backend_protocol.py` and 1,611-line `core\backend_impl.py` mirror broad protocol/client/provider signatures. Current stale test doubles for `include_superseded` and `occurred_at` demonstrate synchronization cost. | VALIDATED RISK. |
| MA-03 | Dark Code (production-unwired) / Medium | `...\src\menhir\services\perception.py:1-879` and fold modules have no core/API/MCP/CLI registration or call path; current calls are tests/internal modules. Registration, reflection, plugin, and dispatch searches found none. It is an intentional bench-first prototype, so it is not called dead/deletable. | VALIDATED DARK CODE. |
| PE-R01 | Performance Design Risk / Low | Three synthetic full scans of this repository took 9.842s, 10.082s, 9.876s and 14,582,753 bytes traced peak for 500 files, 4,374 symbols, 770 imports, and 2,120 call edges. `backend_impl.py:391-423` scans before comparing stored fingerprint. Background cost may be acceptable; no SLO/saturation evidence. | VALIDATED MEASURED RISK, not defect. |
| PE-R02 | Open Question | Recall/Neo4j round-trip and structure-query performance lack production telemetry or `EXPLAIN`. | OPEN; no performance defect. |
| PE-R03 / C38 | Design/Performance Risk / Medium | `...\src\menhir\services\scoring_service.py:47-63,81-95` applies a 0.15 floor to Graphiti RRF scores and says the rank cut is coupled to `rank_const=1`; `domain\retrieval_tuning.py:50-68` still describes priors in cosine terms. A score-scale change can silently change admission. Current historical calibration was not rerun and no regression was reproduced. | VALIDATED RISK, not defect. |
| TQ-01 / FC-T01 | Test Gap / Medium | Final targeted run reproduces six failures: `tests\test_api_routes.py:129,134,199`, `tests\test_cypher.py:1103`, `tests\test_mcp_server.py:640,745`. Production added `include_superseded`, `occurred_at`, and `FACT_TEMPORAL_FIELDS`; stale mocks/expectations fail. Concrete backends accept the parameters. | VALIDATED test-suite regression, not product defect. |
| TQ-02 | Test/Process Gap / Medium | No `.github/workflows` exists; no current CI evidence enforces the 1,871 collected tests. Another external CI system is possible but not documented. | VALIDATED GAP. |
| TQ-03 | Test Gap | Coverage.py/pytest-cov was unavailable and no current coverage artifact/config establishes line/branch coverage. Specific missing behavior: direct-provider scheduler startup, long-job lease expiry, readonly REST/backend denial, and ingest allowed-root denial. | VALIDATED GAP. |
| TQ-L01 | Low assertion-quality issue | `tests\test_scaffold.py:13` is `assert True`. It does not materially change risk alone. | VALIDATED LOW. |
| SEC-H01 / prior B | Hardening Opportunity / Medium | `...\src\menhir\explorer\app.py:618-629` has no auth and defaults to `127.0.0.1`. Default loopback prevents classification as a remote defect; rebinding/proxying exposes graph/telemetry reads and candidate mutations. | VALIDATED HARDENING. |
| SEC-H02 | Hardening Opportunity / Low | `...\src\menhir\api\auth.py:52-60` uses ordinary string equality. High-entropy tokens and network jitter make practical timing extraction unproven. | VALIDATED HARDENING. |
| Prior A | Changed Hardening Opportunity / Low | `...\src\menhir\config\settings.py:45-48` now defaults Neo4j password to empty, not `password`, so the prior credential-default claim is fixed. URI remains loopback `bolt://` and driver creation does not force encrypted transport. | CHANGED: password portion fixed; deployment/TLS hardening remains. |
| Prior D | Design Risk / Low | `...\src\menhir\infrastructure\episode_lifecycle.py:20-36,318-323` classifies context-window retry by free-text markers. This is brittle but no current misclassification reproduction was run. | VALIDATED RISK. |
| CG-01 | Privacy/Governance Gap | Graph delete paths exist at `...\src\menhir\api\routes.py:330-344`, but coordinated deletion/export/correction does not cover SQLite previews, provider/Langfuse copies, caches, logs, or backups. Local/private use and bounded previews affect applicability. | VALIDATED GOVERNANCE GAP; legal significance open. |
| License/SBOM | Policy Evidence Gap | Repository has no root LICENSE, NOTICE, SBOM, or automated license policy; README says private. Full transitive license/SBOM scan was NOT RUN because tooling was unavailable. | VALIDATED evidence gap; no license violation claimed. |
| Explorer WCAG | Open Question | Developer explorer only. Axe/Lighthouse/WAVE, keyboard, and screen-reader checks were NOT RUN. | OPEN; no WCAG failure claimed. |

## Rejected, Fixed, or Unsupported Candidates

| Candidate | Former claim | Decisive evidence | Final disposition |
|---|---|---|---|
| FC-01 | Category-first event signature silently undercounts two distinct same-day/same-value/category purchases. | Final dirty source uses identity/wording at `fold_algebra.py:84-97`, routes ambiguous same-category clusters at `fold_algebra.py:125-160`, and vetoes unresolved coreference at `perception.py:656-679`. Focused run: 63 passed. | FIXED in audited filesystem; do not report as current defect. |
| Six offline failures as product crashes | REST/MCP production paths are broken. | Failures are stale mock/expected-call/export contracts; current concrete backends support new arguments. | DOWNGRADED to TQ-01 test gap. |
| Gitleaks generic-key hits | Repository contains production secrets. | Three hits were documentation/throwaway examples on current review; values were not reproduced. | REJECTED as false positives. |
| CVE claims | Installed dependencies are vulnerable. | `pip-audit` unavailable; no resolved affected-range/advisory proof. | UNSUPPORTED; no CVE reported. |
| Prompt injection exploit | Retrieved content causes tool execution or privileged memory mutation. | Menhir returns content but does not execute it; no synthetic downstream tool chain reproduced. | DOWNGRADED to AI-R01 design risk. |
| Performance defect | Static Neo4j/recall concerns prove unacceptable latency. | No representative query plans, SLO, or load/production telemetry. | OPEN only. |

## Prior Finding Revalidation

Every item in `C:\Users\you\IdeaProjects\projects\archolith\menhir-frontier\.agent\verified-current-findings.md` was re-opened:

1. Forced scheduler takeover overlap: **confirmed**, merged as FC-02/AR-04.
2. Neo4j unsafe defaults: **changed**. The former default password is fixed; loopback unencrypted transport remains deployment hardening.
3. Explorer lacks auth: **confirmed as hardening**, not a default-remote defect because bind defaults to loopback.
4. Telemetry growth/unbounded caller limits: **confirmed as design risk**, with bounded previews and revision-only pruning as current nuance.
5. Context-window free-text retry matching: **confirmed as design risk**, not a demonstrated failure.

The two untracked perception plans were also treated as prior leads: C36's product issue is fixed in the final filesystem; C37 remains a Law-3 design/evaluation risk. The late retrieval design reference produced two validated risks (score-scale coupling and self-reinforcement) and one matched design statement (View reachability is measured, not privileged).

## Verified Measurements and Test Evidence

- Runtime registration: 37 MCP tools and 9 resources/templates.
- Generated OpenAPI: 3.1.0, 10 public paths, 14 schemas; hidden backend route excluded.
- Pytest collection: 1,871 tests, exit 0.
- Full offline disposable-copy run on the pre-drift source: 1,834 passed, 7 failed, 30 deselected, 3 warnings in 589.02s. One failure was rejected as copy-path-specific.
- Final-snapshot direct failure rerun: 6 failed, 3 warnings in 3.25s.
- Final-snapshot perception/fold rerun: 63 passed, 1 warning in 0.21s.
- Scanner measurement: three runs near 10 seconds; 14,582,753-byte traced peak; counts reproduced identically across runs.
- Dependency health: `pip check` exit 0. `pip-audit`, coverage.py/pytest-cov, and pip-licenses unavailable.
- Gitleaks full-history artifact: `C:\tmp\menhir-frontier-audit\phase3\gitleaks.json`; values intentionally not reproduced.

## Phase 11 Conclusion

Five unique current defects survive validation: three scheduler/runtime correctness defects and two authorization/filesystem security defects. Nine material documentation groups are current. FC-01 was invalidated by a stable concurrent fix before validation and is explicitly excluded. Remaining items are classified as risks, hardening, test/dark-code/performance/governance gaps, or open questions without strengthening them into defects.

## Remediation Status (2026-07-06)

All five confirmed defects have been remediated on branch `claude/menhir-chain-handoff-doc-7iuat2` (menhir-frontier repo). Verified against current code, which was ~40 commits ahead of the audit freeze `e42e6053`.

| Finding | Disposition | Commit |
|---|---|---|
| SEC-01 / EC-01 (readonly -> destructive REST + backend RPC) | Already fixed post-audit: `routes.py` operator-gated deletes + total per-op backend dispatch policy; `contracts.py` per-tool `required_tier`; constant-time compares (SEC-H02) | `4130e95` |
| AR-01 (maintenance coupled to model-endpoint ownership) | Fixed: in-process `MaintenanceScheduler` now starts on `capabilities.enrichment_ready`, not `uses_scheduler` | `1858e9c` |
| AR-02 (lease not heartbeated during long jobs) | Fixed: background heartbeat renews lease at <= lease/3 independent of job execution | `1858e9c` |
| FC-02 / AR-04 (forced-takeover overlap) | Fixed: heartbeat marks lease lost + job batch fences remaining jobs | `1858e9c` |
| SEC-02 (unbounded ingest paths) | Fixed: `core/ingest_guard.ensure_ingest_path_allowed` at the backend RPC choke point; secure-by-default allowlist + operator bypass (`MENHIR_INGEST_ALLOWED_ROOTS`) | `7bceb9a` |

Regression tests added (two scheduler tests, repurposed AR-01 startup test, `test_ingest_guard.py`); touched suites green. **Still open** (not addressed this pass): documentation defects DOC-01..10, hardening B/C/D (explorer auth, telemetry retention, free-text retry, Neo4j TLS), TQ-01 stale mocks, MA-01/02 maintainability risks, MA-03 perception dark code, and governance/coverage gaps. These are tracked in `menhir-frontier/.agent/verified-current-findings.md`.
