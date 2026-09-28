---
artifact_schema: 1
artifact_uuid: 6f52ab20-83ef-44ae-b838-1797c31018d2
artifact_type: review
artifact_status: OPEN
reviews: dbde430c-9be1-4131-aa85-871a8e99e5ef
---

# Menhir local-stdio MVP release audit

**Review baseline:** `c49b6bd9cf32503f4c85fd80c0bf302e7fdc38e4` (merged `main`, 2026-09-27)

**Authority:** [local-stdio MVP release plan](../plans/menhir-local-stdio-mvp-release-2026-09-16.md), Phase B

**State:** OPEN. This is the first independent code-and-contract pass, not Gate B closure or release approval.

## Findings

| ID | Disposition | Evidence and required action |
| --- | --- | --- |
| B-01 | **RESOLVED ON AUDIT BRANCH � candidate-wheel evidence** | At the review baseline, E2E-6 tested deferred Beacon behavior. PR #176 replaces that lane with a clean packaged-fork stdio test. [CI run 36365177164](https://github.com/Archolith/menhir/actions/runs/36365177164) passed the test on implementation commit `d7dd3477`; its [evidence artifact](https://github.com/Archolith/menhir/actions/runs/36365177164/artifacts/10947501507) records five of five candidate-wheel criteria PASS. The release-container criteria remain PENDING under B-02. |
| B-02 | **BLOCKER � release-container validation outstanding** | A fresh Windows Python 3.12.10 environment installed a Menhir wheel built from this commit, resolved public `archolith-graphiti-core==0.30.2.post1` with no upstream or VCS distribution, imported the four native hooks, and passed dependency compatibility for 95 packages. This proves the package path only. [No-push image validation run 36362966372](https://github.com/Archolith/menhir/actions/runs/36362966372) built the image but failed when Syft could not read its archive; publication was skipped. Fix and revalidate the container path at the later release gate. No image publication or deployment is authorized by this audit. |
| B-03 | **FIXED ON AUDIT BRANCH � evidence map was stale** | At the review baseline, `tests/e2e/README.md` assigned E2E-6 and an E2E-8 criterion to Beacon. The follow-up updates the lane inventory, moves the Beacon E2E-6 scenario outside test collection, and adds a real missing-fork refusal check for E2E-8. |

No new HIGH-severity implementation defect was confirmed in this pass. B-01's candidate-wheel gap is resolved on the audit branch; B-02 remains a release blocker. This does not establish frozen-RC acceptance.

## Follow-up on the audit branch

`tests/e2e/test_e2e_06_graphiti_fork.py` checks the exact public wheel hash against `uv.lock`, compares all installed `graphiti_core` Python files to that wheel, rejects an upstream `graphiti-core` collision, constructs Menhir's installed Graphiti client to inspect its four native hooks, and drives one deterministic ingest/recall through MCP stdio. The isolated Windows package probe matched all 163 installed source files and reported all four hooks wired. CI on implementation commit `d7dd3477` passed lint, offline, online, and stdio E2E jobs; the uploaded report records packaged-fork PASS (5/5), including READY enrichment and recall over stdio. E2E-8 now tests the missing-fork variant in a second installed-wheel environment; its focused Windows run passed after `pip check` and Menhir both refused the broken environment by name. This does not separately exercise an incompatible version. The release-container half of E2E-6 remains PENDING (0/2) under B-02. Deferred Beacon scenarios are not required for MVP.

## Supported-path disposition

| Phase B area | Code/contract evidence inspected | Status before RC freeze |
| --- | --- | --- |
| B1 � stdio/MCP | `mcp/server.py` explicitly binds local stdio trust. Tool registration validates metadata and tenancy scope. E2E-1 declares fixed required tool names and a non-editable installed-wheel setup. | **Verify:** stock-client schema results and graceful lifecycle on the named RC; reconcile the shipped tool list with the deferred Beacon claim. |
| B2 � memory | `add_memory_and_track` distinguishes an accepted write from READY/FAILED/timeout, retains the episode receipt if tracking fails, and tells the agent to observe the same episode. `get_enrichment_status` checks namespace ownership. #88 has a live regression receipt; E2E-2 and E2E-8 pin the lifecycle and isolation paths. | **Verify:** complete frozen-candidate E2E-2/E2E-8, #119 rollout status, default-off scalar/event isolation, and #70 residuals. |
| B3 � local structure | E2E-3 asserts imports, callers, tests, blast radius, restart persistence, and an explicit unknown-coverage caveat. E2E-8 declares capped-scan prune and project isolation checks. | **Verify:** inspect the local scanner's identity/CAS write path and run the complete lanes on the named candidate; project rename remains unsupported. |
| B4 � WorkArtifacts | MCP `supersede_artifact` checks ownership for both IDs; repository supersession uses one Cypher statement for edge and status. E2E-4 declares Git move/reconciliation and unreadable-corpus checks. | **Verify:** lifecycle matrix, schema/backend argument mapping, and E2E-4 on the named candidate. |
| B5 � TODOs | MCP `close_todo` checks ownership; the repository closes only open TODOs in one graph statement. E2E-5 declares location resolution, repeat close, invalid close, and restart checks. | **Verify:** full add/list/get/close contract and E2E-5 on the named candidate. |
| B6 � Graphiti fork | `pyproject.toml` and `uv.lock` pin the public fork. The fresh merged-wheel install passed; `test_graphiti_fork_contract.py` covers immutable dependency and adapter boundaries. The audit branch's installed-wheel native-hook stdio acceptance passed on implementation commit `d7dd3477`; its missing-fork negative check passed locally. | **Blocked:** B-02 release-container validation and the later frozen-RC rerun remain. |
| B7 � persistence/failure | E2E-7 declares interrupted-work recovery and no false READY. E2E-8 declares provider-failure, oversize, isolation, and partial-scan checks. | **Verify:** complete E2E-7/E2E-8 and inspect failure/secret-handling paths on the named candidate. |

## Separation from Gate A and release

Gate A still needs an explicit supported-platform/configuration decision and a live five-item Oracle preflight. The no-cost benchmark preflight passed 85 validator/provenance/ingest-fixture tests and its calls match current Menhir API fields, but no live graph or paid model call was run. A bare `--limit 5` samples only temporal-reasoning; it is a harness smoke, not six-type quality evidence. The public fork's locked wheel SHA-256 is `a748f98e0b09d64ab1eb29bd3449f52b552250a31663e86c4d99dc51ddf991f0`.

This review remains OPEN until every Phase B item is independently checked and every HIGH/critical supported-path finding is fixed or proven unreachable. Do not freeze an RC or release from this report.
