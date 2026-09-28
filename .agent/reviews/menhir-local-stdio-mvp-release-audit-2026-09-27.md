---
artifact_schema: 1
artifact_uuid: 6f52ab20-83ef-44ae-b838-1797c31018d2
artifact_type: review
artifact_status: OPEN
reviews: dbde430c-9be1-4131-aa85-871a8e99e5ef
---

# Menhir local-stdio MVP release audit

**Review baseline:** `c49b6bd9cf32503f4c85fd80c0bf302e7fdc38e4` (merged `main`, 2026-09-27)

**Second-pass baseline:** `14aed115dd0ecb8878aef9b44bfc6a233748cb1a` (merged `main`, 2026-09-28)

**Authority:** [local-stdio MVP release plan](../plans/menhir-local-stdio-mvp-release-2026-09-16.md), Phase B

**State:** OPEN / final candidate evidence in progress. The historical findings below were recorded before PRs #179–#181 merged. Their current dispositions are here; the old pass-by-pass text remains as the review history, not the latest verdict. This is not RC freeze or release approval.

## Current disposition, 2026-09-28

**Reviewed main:** `45b91cf3abef05091270b69ed81f00fa53ee8235` (PRs [#179](https://github.com/Archolith/menhir/pull/179), [#180](https://github.com/Archolith/menhir/pull/180), and [#181](https://github.com/Archolith/menhir/pull/181) merged). [Post-merge CI](https://github.com/Archolith/menhir/actions/runs/36463630273) passed lint, offline tests, disposable-graph online tests, and installed-wheel stdio E2E. Its downloaded stdio evidence records 11 PASS tests and 68/68 implemented criteria PASS; the two release-container criteria correctly remained PENDING in that separate job.

The [no-publish image validation](https://github.com/Archolith/menhir/actions/runs/36464246373) passed on the **same commit**. The downloadable sealed bundle binds image ID `sha256:4ac07f89438d64a4e1485294a1a0c6c17e4c0917cda55d4b1b568a37f66ab21c` to that source commit and its archive/SBOM/vulnerability-report digests. The image's own probe reports `archolith-graphiti-core==0.30.2.post1`, no upstream `graphiti-core` distribution, all native hook parameters present, and Menhir's hooks wired. Syft reported 187 packages; Grype found zero Critical findings and passed the workflow's stated policy. The validation job did not publish an image.

| Finding | Current disposition and direct evidence |
| --- | --- |
| B-01 | **Resolved.** Installed-wheel E2E-6 passed all five fork criteria in the post-merge stdio artifact. |
| B-02 | **Resolved for pre-freeze validation.** The exact-main no-publish image workflow and in-container Graphiti probe passed. The branch adds an optional E2E-6 bundle verifier so the two container criteria can be recorded as PASS in a single strict campaign; that branch run remains to be completed. |
| B-03 | **Resolved.** E2E inventory and execution exclude deferred Beacon cases and include the fork refusal. |
| B-04 | **Fixed on main; platform acceptance being completed.** The scanner raises on traversal, stat, or required-read failure before publication; incomplete caps cannot authorize prune. A disposable-graph injected failure regression and scan-cap E2E passed in post-merge CI. A new Windows ACL denied-directory installed-wheel E2E on this review branch passed: caller received a traversal refusal, and all 35 indexed entities and 44 relationships were unchanged. Ubuntu receipt remains to be checked in branch CI. |
| B-05 | **Fixed on main.** Each scan receives a graph-backed generation before traversal; publication locks its project and checks the latest generation inside one Neo4j transaction. Online regressions cover stale publication, serialization, rollback, restart, and missing token. |
| B-06/B-07 | **Fixed on main.** Artifact transitions compare the observed type/status/namespace while holding the graph lock; supersession creates the replacement edge in the same mutation. General or newly registered edge-less `SUPERSEDED` is refused. Online tests cover competing transitions and replacements, plus rollback. |
| B-08 | **Fixed on main.** TODO file resolution links only a unique permitted candidate; ambiguous/unknown locations stay saved and explicitly unresolved. Online tests cover duplicate paths, namespace boundaries, multiple locations, and restart. |
| B-09/B-10 | **Fixed on main.** Empty structure answers include the measured or unknown coverage qualifier. E2E-8 now separates namespace-delete limits from a real capped structural scan over an indexed file. |

The branch's first real ACL test exposed a caller-quality gap: the graph remained intact, but a traversal error surfaced as an opaque HTTP 500. This change maps that scan failure to the established caller-visible refusal path. The rerun passed on Windows against a disposable Neo4j graph; the changed online regression is being rerun. The branch must still pass its exact-SHA CI, Ubuntu denial test, no-publish bundle verification, and complete strict pre-freeze E2E before this review can become `COMPLETE`. Frozen-RC and final release evidence are later gates.

## Findings

| ID | Disposition | Evidence and required action |
| --- | --- | --- |
| B-01 | **RESOLVED ON AUDIT BRANCH — candidate-wheel evidence** | At the review baseline, E2E-6 tested deferred Beacon behavior. PR #176 replaces that lane with a clean packaged-fork stdio test. [CI run 36365177164](https://github.com/Archolith/menhir/actions/runs/36365177164) passed the test on implementation commit `d7dd3477`; its [evidence artifact](https://github.com/Archolith/menhir/actions/runs/36365177164/artifacts/10947501507) records five of five candidate-wheel criteria PASS. The release-container criteria remain PENDING under B-02. |
| B-02 | **BLOCKER — release-container validation outstanding** | A fresh Windows Python 3.12.10 environment installed a Menhir wheel built from this commit, resolved public `archolith-graphiti-core==0.30.2.post1` with no upstream or VCS distribution, imported the four native hooks, and passed dependency compatibility for 95 packages. This proves the package path only. [No-push image validation run 36362966372](https://github.com/Archolith/menhir/actions/runs/36362966372) built the image but failed when Syft could not read its archive; publication was skipped. Fix and revalidate the container path at the later release gate. No image publication or deployment is authorized by this audit. |
| B-03 | **FIXED ON AUDIT BRANCH — evidence map was stale** | At the review baseline, `tests/e2e/README.md` assigned E2E-6 and an E2E-8 criterion to Beacon. The follow-up updates the lane inventory, moves the Beacon E2E-6 scenario outside test collection, and adds a real missing-fork refusal check for E2E-8. |

No new HIGH-severity implementation defect was confirmed in the first pass. B-01's candidate-wheel gap is resolved on main; B-02 remains a release blocker. The second pass below identifies additional blockers. This does not establish frozen-RC acceptance.

## Follow-up on the audit branch

`tests/e2e/test_e2e_06_graphiti_fork.py` checks the exact public wheel hash against `uv.lock`, compares all installed `graphiti_core` Python files to that wheel, rejects an upstream `graphiti-core` collision, constructs Menhir's installed Graphiti client to inspect its four native hooks, and drives one deterministic ingest/recall through MCP stdio. The isolated Windows package probe matched all 163 installed source files and reported all four hooks wired. CI on implementation commit `d7dd3477` passed lint, offline, online, and stdio E2E jobs; the uploaded report records packaged-fork PASS (5/5), including READY enrichment and recall over stdio. E2E-8 now tests the missing-fork variant in a second installed-wheel environment; its focused Windows run passed after `pip check` and Menhir both refused the broken environment by name. This does not separately exercise an incompatible version. The release-container half of E2E-6 remains PENDING (0/2) under B-02. Deferred Beacon scenarios are not required for MVP.

## Supported-path disposition

| Phase B area | Code/contract evidence inspected | Status before RC freeze |
| --- | --- | --- |
| B1 — stdio/MCP | `mcp/server.py` explicitly binds local stdio trust. Tool registration validates metadata and tenancy scope. E2E-1 declares fixed required tool names and a non-editable installed-wheel setup. | **Verify:** stock-client schema results and graceful lifecycle on the named RC; reconcile the shipped tool list with the deferred Beacon claim. |
| B2 — memory | `add_memory_and_track` distinguishes an accepted write from READY/FAILED/timeout, retains the episode receipt if tracking fails, and tells the agent to observe the same episode. `get_enrichment_status` checks namespace ownership. #88 has a live regression receipt; E2E-2 and E2E-8 pin the lifecycle and isolation paths. | **Verify:** complete frozen-candidate E2E-2/E2E-8, #119 rollout status, default-off scalar/event isolation, and #70 residuals. |
| B3 — local structure | E2E-3 asserts imports, callers, tests, blast radius, restart persistence, and an explicit unknown-coverage caveat. E2E-8 declares capped-scan prune and project isolation checks. | **Verify:** inspect the local scanner's identity/CAS write path and run the complete lanes on the named candidate; project rename remains unsupported. |
| B4 — WorkArtifacts | MCP `supersede_artifact` checks ownership for both IDs; repository supersession uses one Cypher statement for edge and status. E2E-4 declares Git move/reconciliation and unreadable-corpus checks. | **Verify:** lifecycle matrix, schema/backend argument mapping, and E2E-4 on the named candidate. |
| B5 — TODOs | MCP `close_todo` checks ownership; the repository closes only open TODOs in one graph statement. E2E-5 declares location resolution, repeat close, invalid close, and restart checks. | **Verify:** full add/list/get/close contract and E2E-5 on the named candidate. |
| B6 — Graphiti fork | `pyproject.toml` and `uv.lock` pin the public fork. The fresh merged-wheel install passed; `test_graphiti_fork_contract.py` covers immutable dependency and adapter boundaries. The audit branch's installed-wheel native-hook stdio acceptance passed on implementation commit `d7dd3477`; its missing-fork negative check passed locally. | **Blocked:** B-02 release-container validation and the later frozen-RC rerun remain. |
| B7 — persistence/failure | E2E-7 declares interrupted-work recovery and no false READY. E2E-8 declares provider-failure, oversize, isolation, and partial-scan checks. | **Verify:** complete E2E-7/E2E-8 and inspect failure/secret-handling paths on the named candidate. |

## Second independent pass on merged `14aed115` (2026-09-28)

The current commit has a successful [four-job CI run](https://github.com/Archolith/menhir/actions/runs/36379240436) (lint, complete offline, disposable-Neo4j online, and stdio E2E). The stdio job passed, but its evidence artifact could not be downloaded with the available GitHub credential (HTTP 401); a job success is not a criterion-by-criterion receipt. A focused local pytest collection failed before tests ran because this checkout's global Python lacks `graphiti_core`. Neither result is represented as a new local test pass. The prior clean Windows E2E receipt on `4750b1c8` remains 11 PASS / one declared release-container PENDING.

| ID | Area / severity | Supported-path failure and required action |
| --- | --- | --- |
| B-04 | **B3 BLOCKER — incomplete traversal can prune indexed structure** | `project_scanner.py` calls `os.walk(root)` without `onerror` (line 269). By default a `scandir` error is ignored; `partial_index` only detects the file cap (lines 135–143). A temporarily unreadable subtree therefore looks absent in a complete scan, and `structure_queries.py` may delete its existing file/directory entities (lines 179–180, 254–256, 304–310). Make traversal failure explicit and refuse destructive pruning on incomplete coverage; test an injected unreadable-directory scan against an already indexed tree. |
| B-05 | **B3 BLOCKER — concurrent scans can regress structure** | Manual ingest schedules a detached write (`backend_runtime_data_ops.py`, lines 640–748); the watcher writes independently. `structure_write_fence.py` admits multiple writers for the same project (lines 150–177), while `structure_queries.py` writes fingerprint/entities and prunes without a freshness CAS (lines 159–229, 304–310). If older scan A writes after newer scan B, A can restore stale structure and delete B-only paths. Serialize per project or reject stale scans at the mutation boundary; test the A-scan/B-scan/B-write/A-write interleaving. |
| B-06 | **B4 BLOCKER — lifecycle read/write race** | `transition_artifact_status` checks legality from a read at `work_artifact_repository.py:1198–1223`, then writes by UUID/namespace without matching the observed status at lines 1234–1242. Concurrent transitions can both pass from one state and the later one can overwrite a terminal state. Add compare-and-set to the graph mutation and a stale-transition regression. |
| B-07 | **B4 BLOCKER — supersession can lack its replacement edge** | The general `transition_artifact` tool advertises `SUPERSEDED` and the domain permits it, but the general transition only changes status. The dedicated `supersede_artifact` path documents and atomically creates the `new → old` edge. Route `SUPERSEDED` through the dedicated operation or define and enforce a different explicit contract; test that a superseded artifact always records its replacement. |
| B-08 | **B5 BLOCKER — ambiguous TODO file reference links across projects** | `add_todo` permits `code_ref` without `structure_project`. `todo_repository.py:300–319` creates `REFERENCES_FILE` for every matching relative path across projects, then `LIMIT 1` returns only one arbitrary match. A TODO can appear attached to the wrong project. Refuse/leave unlinked when ambiguous, or require a resolved project; test two projects with the same relative path. |
| B-09 | **B3 MEDIUM — incomplete negative-answer caveats** | `query_structure.py` computes a coverage qualifier but empty symbols/dependencies/documents/affected-tests paths omit it (lines 327–331, 383–389, 539–560, 584–589). Extend the qualifier and test legacy/partial coverage. |
| B-10 | **E2E coverage mismatch** | E2E-8's criterion named `capped_scan_does_not_authorize_destructive_prune` actually calls `delete_namespace` with `max_nodes=1` (`tests/e2e/test_e2e_08_isolation_adversarial.py:322–376`); it never caps a structural scan. Rename that criterion for what it proves and add a real structure-cap/traversal-error regression. |

### Area-by-area second-pass verdict

- **B1 stdio/MCP: provisionally passes code review.** `mcp/server.py` binds explicit local operator trust, registration validates declared scopes/metadata, and E2E-1 uses an installed wheel and stock MCP client for initialization, discovery, schema shape, and shutdown. The exact-commit stdio CI job passed. No new HIGH finding was identified here; its criterion evidence still belongs in the final RC pack.
- **B2 memory: provisionally passes code review with #119 held.** `add_memory` says PENDING; `add_memory_and_track` preserves an accepted receipt when observation fails and distinguishes READY/FAILED/timeout. #88 has two real-LLM disposable-graph passes and a deterministic same-session namespace E2E pin. #70's heartbeat/observability sites now use `try/finally` and warning logs. Scalar/event/frontier flags are default-off. #119's historical repair is explicitly deferred to the single final release, so production rollout is not claimed here.
- **B3 structure: blocked by B-04/B-05; B-09 is additional correctness work.** The local path uses identity admission; project rename remains unsupported. A partial-cap guard exists, but it does not cover traversal failure, and concurrent accepted scans lack freshness control.
- **B4 WorkArtifacts: blocked by B-06/B-07.** MCP/backend supersede argument order and the dedicated edge/status transaction are consistent; source reconciliation, read-only audit, and UUID validation have code and E2E coverage, subject to final RC rerun.
- **B5 TODOs: blocked by B-08.** Add/list/get/close and restart are covered in E2E-5; repeat close reports an explicit outcome. Equal-time list ordering lacks a UUID tie-breaker (low severity), but the ambiguous file-link path is the release concern.
- **B6 Graphiti fork: wheel path passes; container path pending (B-02).** `pyproject.toml` and `uv.lock` pin the distinct public fork. Installed-wheel native-hook ingest/recall and missing-fork refusal passed previously. The release-container validation still has no passing receipt; no image was published.
- **B7 persistence/failure/resource: provisional, not a release pass.** E2E-7 exercises kill/restart, graph-backed no-false-READY, and operator lease recovery; E2E-8 exercises provider failure, oversized diff, and namespace isolation. Current CI ran the stdio job successfully. The evidence artifact was unavailable to this audit credential, and the frozen-candidate rerun remains required. B-04/B-05 are also B7 data-integrity risks.

**Gate B verdict: BLOCKED.** Fix B-04 through B-08 on supported paths, add direct regressions (including the mislabeled E2E structural scan), rerun focused tests plus the required exact-SHA CI/E2E lanes, and independently review the changed behavior. Keep this review `OPEN` until those findings are resolved and the release-container evidence is available. This audit did not freeze an RC, deploy, repair the production graph, publish, or release.

## Separation from Gate A and release

Gate A's supported-platform/configuration decision and paid five-item Oracle graph preflight have since passed and are recorded in issue #123. The five-item preflight is a harness smoke, not six-type quality evidence. The public fork's locked wheel SHA-256 is `a748f98e0b09d64ab1eb29bd3449f52b552250a31663e86c4d99dc51ddf991f0`.

This review remains OPEN until every Phase B item is independently checked and every HIGH/critical supported-path finding is fixed or proven unreachable. Do not freeze an RC or release from this report.
