---
artifact_schema: 1
artifact_uuid: f5c42b7a-396a-407f-ab2d-3059e808faaa
artifact_type: implementation_report
artifact_status: DRAFT
implements: 35d57efd-8fd5-4b9a-9fd5-582ebfb134f7
---

# WRAPUP — Menhir release staging and deployment automation

**Date:** 2026-09-06  
**Agent:** Codex  
**Model:** gpt-5.6-sol  
**Status:** PARTIAL  
**Plan / Ticket:** `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\archive\plans\menhir-release-automation-2026-09-06.md`  
**Worktree:** `C:\Users\thron\IdeaProjects\projects\archolith\menhir`  
**Branch:** `feat/release-automation`  
**Commits:** `e9dde81d6b181e7abd9d2960e96cb262d2832b43`, `0e6628be5203401ef7895a1a3cf28bd5776a33e4`, `6e913691efe91706da4d7fe6db075198ec9e8614`, `3f5eebe99689e0c1cae066d01edce6a057372672`, `5c1d12bb36c514eed4889eaf6a6a9a8a16ffb4ca`, `963f1abe2a9761cae620295301fd60b3ebdabdc7`, `8eaa706548a5cfde4ac0764d4418b07dd88ce959`, `8e427f107268aaf76064a9563593972a88f7b8a8`, `bf646676abdeb5d3c4b28e3af2f888fe5c5f3e90`, `362eb37742768adf649a30799d874827a7e06535`; companion workspace-meta commit `2d9c028003b1e23aab922962315b176f1e5aa5d4`  
**Verification Scope:** Menhir `abb1f10ada2eabc0ed63f8c085b7357b37019342..362eb37742768adf649a30799d874827a7e06535`; workspace-meta `fed867d5..2d9c028003b1e23aab922962315b176f1e5aa5d4`; plus this closeout artifact  
**Docs Updated:** `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\RELEASE_AUTOMATION.md`, `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\LIVE_VPS_PLAYBOOK.md`, `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\README.md`, `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\changes\README.md`, `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\scripts-index.md`, `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\archive\plans\menhir-release-automation-2026-09-06.md`, `C:\Users\thron\IdeaProjects\scripts\README.md`  
**Changelog Updated:** `C:\Users\thron\IdeaProjects\projects\archolith\menhir\CHANGELOG.md`

---

## Before Writing

The plan was checked backwards from the desired end state. A production deployment must enter the
existing server transaction through the canonical desktop wrapper, using one immutable reviewed
bundle and an exact release-id confirmation. That requires a fully bound bundle handoff, a review
request derived from strict inputs, deterministic staged notes, conservative deployment
classification, and retry-safe publication. The initial implementation missed the production
wrapper's required `install.sh` name and bound only the bundle manifest; independent review found
that critical gap. The final implementation names and binds the complete bundle tree through the
coordinator, local desktop wrapper, and remote verifier, removes the CLI wrapper override, derives
a conservative minimum deployment class, and reloads or restores active systemd services around
maintenance installation.

The workflow begins after immutable images and evidence exist. Building/publishing those inputs,
cryptographically authenticating the independent reviewer, and proving the workflow on Linux or a
live host remain outside the completed implementation and are recorded below.

---

## Summary

Menhir now has a maintained, resumable release workflow instead of one-off release workspace
scripts. Committed JSON fragments produce deterministic Markdown and JSON notes. A strict
four-repository preparer validates clean remote-tip checkouts, evidence, policies, rendered files,
secret version identifiers, and the exact 67-path installed-artifact census before invoking the
existing release author. Finalization requires the existing authority-bound independent review and
publishes `release.json` plus a deterministic `install-bundle` only after complete validation.

Deployment remains a preview unless the operator supplies both the exact release ID and
`--execute`. The CLI can invoke only the canonical desktop wrapper. App-only classification is
conservative and cannot be downgraded by a fragment; maintenance installation supplies the exact
`install.sh` expected by the wrapper and passes a portable complete-tree digest. The workspace
wrapper requires and verifies that digest before upload and again on the VPS before bundle-backed
mutation. Installation reloads systemd definitions and restores prior files and active services on
failure. The changes are isolated in Menhir PR #59 and workspace-meta PR #1. No production
deployment, image push, or VPS mutation was performed.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\archive\plans\menhir-release-automation-2026-09-06.md` | Record the implemented design, invariants, validation scope, and completed disposition. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\scripts-index.md` | Index the maintained release instruments. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-09-06-menhir-release-automation.md` | Record implementation evidence and unresolved release-readiness gaps. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\CHANGELOG.md` | Add the release-automation entry and retain only the ten most recent entries. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\LIVE_VPS_PLAYBOOK.md` | Route routine release preparation through the maintained coordinator while retaining low-level recovery commands. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\README.md` | Link the release automation runbook from the deployment entry point. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\RELEASE_AUTOMATION.md` | Document inputs, prepare, review, finalize, preview, execution, retries, classification, and systemd behavior. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\build_install_bundle.py` | Build and revalidate the deterministic exact-census install bundle from reviewed authority and committed blobs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\changes\README.md` | Define the strict staged release-note fragment contract. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\changes\unreleased\chatgpt-stable-cimd.json` | Stage the already-committed ChatGPT stable-CIMD production change for the next release. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\changes\unreleased\release-automation.json` | Stage this release automation and its exact implementation commits. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\release-inputs.example.json` | Provide the strict no-secret release-input template. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\release-install.sh` | Validate and transactionally install the reviewed bundle, with file and active-service rollback. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\release_flow.py` | Coordinate digest-bound prepare, finalize, status, preview, and explicit deployment phases. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\release_notes.py` | Strictly validate and deterministically render release-note fragments. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\deploy\release_spec.py` | Generate the maintained release-author specification from validated four-repository inputs. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\tests\test_install_bundle_builder.py` | Cover bundle census, modes, tampering, installer policy, deterministic output, and cleanup. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\tests\test_release_flow.py` | Cover commit coverage, classification, phase gates, exact confirmation, bundle drift, and retry behavior. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\tests\test_release_notes.py` | Cover fragment schema, bounds, deterministic rendering, paths, and atomic writes. |
| `C:\Users\thron\IdeaProjects\projects\archolith\menhir\tests\test_release_spec.py` | Cover repository authority, operations policy shape, installed mappings, evidence, secrets, and cleanup. |
| `C:\Users\thron\IdeaProjects\scripts\deploy-menhir.ps1` | Require the staged complete-tree digest for maintenance, verify it before upload, and repeat verification remotely before bundle-backed mutation. |
| `C:\Users\thron\IdeaProjects\scripts\README.md` | Document the app-only/maintenance dispatcher and digest-bound coordinator path. |

## Verification

- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp C:\Users\thron\Documents\Codex\2026-09-06\inve\work\pytest-full-rebased-12` — `PASS` — 9,020 passed, 361 skipped, 9 warnings in 883.97 seconds against refreshed `origin/main` through commit `5c1d12bb`.
- `.\.venv\Scripts\python.exe -m pytest tests\test_release_notes.py tests\test_release_spec.py tests\test_install_bundle_builder.py tests\test_release_flow.py tests\test_release_author.py tests\test_live_vps_playbook.py tests\test_deployment_contracts.py tests\test_production_runtime_surface.py -q -p no:cacheprovider --basetemp C:\Users\thron\Documents\Codex\2026-09-06\inve\work\pytest-release-rebased-11` — `PASS` — 240 passed, 7 skipped, 2 warnings in 448.20 seconds.
- Focused final `tests/test_release_flow.py` run with an isolated temporary root — `PASS` — 18 passed, 2 warnings in 21.04 seconds after making cross-repository Git fixtures unique at Menhir `8e427f10`.
- Final broad release/deployment selection — `PASS WITH ENVIRONMENT RERUN` — 240 passed and 7 skipped; its only initial failure was WSL `CreateInstance/E_ACCESSDENIED` under the sandbox, and the exact failed shell-contract test passed when rerun with local WSL permission.
- Windows PowerShell 5.1 parser plus local PowerShell/embedded remote-Python portable bundle-digest parity — `PASS` — both implementations returned the same SHA-256 for the same tree.
- Direct maintenance-wrapper call without `-ExpectedBundleSha256` against a local non-bundle fixture — `PASS` — refused before any network or deployment operation.
- Independent final read-only audit through Menhir `362eb377` and the companion wrapper — `PASS` — every staged full commit hash resolved inside the candidate range; approved for merge with no actionable findings and explicitly not approval to deploy.
- `.\.venv\Scripts\python.exe deploy\release_notes.py validate deploy\changes\unreleased` — `PASS` — validated 2 release-note fragments.
- `C:\Program Files\Git\bin\bash.exe -n deploy/release-install.sh` — `PASS` — exit code 0 with no shell syntax findings.
- Release artifact Git-object census over `release_spec.ARTIFACT_SOURCES` — `PASS` — 67 mappings checked; missing list empty.
- `git diff --check origin/main...HEAD` — `PASS` — exit code 0 with no whitespace errors.
- `.\.venv\Scripts\menhir.exe artifacts validate . --repository menhir` — `FAIL` — validated 216 records and reported 22 inherited corpus findings; none names a path added or changed by this branch.
- Hosted CI — `PASS` — Menhir PR #59 at `362eb377`: lint passed, graph-backed online tests passed, and the offline suite passed with 9,030 passed, 352 skipped, and 8 warnings in 261.16 seconds. The first offline run exposed identical test Git commit hashes on fast Linux; fixture content is now repository-unique. The local GitHub CLI check query returned 401, so status was verified through GitHub's public Checks API and authenticated job UI.
- Workspace-meta PR #1 checks — `INHERITED FAILURES` — both mechanical checks pass; eight dependency-audit jobs fail before auditing because the workspace-meta repository intentionally excludes the configured child-project working directories. The PR changes only the Menhir wrapper and script index and has no merge conflict.
- Linux installer execution and live deployment — `NOT RUN` — no Linux host fixture was available and production deployment was explicitly deferred.
- `artifact_validate(artifact_type="wrapups", ...)` — `NOT RUN` — that harness tool is unavailable; the repository-wide artifact validator was run instead, so this wrapup remains below `READY FOR REVIEW`.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `partial` — repository implementation and hosted CI are complete; disposable-Linux installer execution and live proof remain outstanding.
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes` — implementation anchors are committed; this document is the separate closeout artifact.

## Assumptions

1. The canonical desktop wrapper is `C:\Users\thron\IdeaProjects\scripts\deploy-menhir.ps1`; its companion change is isolated in workspace-meta PR #1 rather than mixed with unrelated local workspace commits.
2. The existing procedural control around independent reviewer identity remains the release authority until a separately designed cryptographic attestation mechanism replaces it.
3. Immutable image creation and evidence generation continue to happen before `release_flow.py prepare`.

## Risks / Gaps

1. Independent-review identity is still a self-declared string in the pre-existing release schema; authority-digest binding prevents review reuse after drift but does not cryptographically authenticate the reviewer.
2. Image build/publish, SBOM and scan production, wheelhouse preparation, and current-host evidence collection are still prerequisites rather than automated steps. The implemented workflow automates release assembly from those inputs through the existing deployment transaction.
3. The installer passed syntax, deterministic construction, and tamper/rollback unit tests on Windows, but was not executed on Linux or a disposable host with systemd.
4. Repository artifact validation still has 22 inherited corpus findings outside this branch.
5. Production behavior remains unproven. This wrapup must not be read as production deployment approval.
6. Remote digest verification and bundle execution are sequential. The deployment account and its private upload directory remain trusted against concurrent mutation during that interval.

## Follow-Up Tasks

1. Merge Menhir PR #59 and companion workspace-meta PR #1 after review; workspace-meta's unrelated child-repository audit jobs require repair or an explicit baseline waiver before merge.
2. Exercise `release-install.sh` against a disposable Linux/systemd host before using the maintenance path in production.
3. Design and separately review authenticated or signed independent-review attestations.
4. Add a maintained CI or desktop evidence-acquisition stage for image publication, SBOM/scan output, wheelhouse creation, and current-host evidence.
5. Obtain explicit production approval before running `release_flow.py deploy --execute`; retain the complete release workspace and deployment evidence.
