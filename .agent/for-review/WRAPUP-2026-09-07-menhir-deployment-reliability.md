# WRAPUP — Menhir deployment reliability

**Date:** 2026-09-07  
**Agent:** Codex  
**Model:** gpt-5.6-sol  
**Session:** unavailable  
**Status:** PARTIAL  
**Plan / Ticket:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\.agent\plans\menhir-deployment-reliability-2026-09-07.md  
**Worktree:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion  
**Branch:** fix/deployment-reliability-20260907  
**Commits:** 62a536854b69b25611ab70faea8ea080869ee824; 476f5efc944e64f8aa9ac5b9c0adae7f1590810c; 5b8db26d0f7649575b1f28d79425a6de96c5e65c; 06658b0693e4c4c8407bf5de2588894f7d653a31; fb62b5e35b72a3ef9f1f1be83eecff0a7cbf12e8; 409b6096d700ee1e1647c62b689485aedfbeb36d; f7f8475d6b58e4f7717a9fba5009b2890fde329f; d4ec1974fe150f0aa95060e2a5c97edaa678443e; shared operator scripts b2fbde79f0e43de798fd813c26c6e74cb277de7a
**Verification Scope:** Menhir commits 62a5368 through d4ec197, shared operator-scripts commit b2fbde79, the installed scaffold receipt captured 2026-09-08T02:45:30Z, and the unchanged live 0.2.0-13 runtime checks recorded below
**Docs Updated:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\deploy\RELEASE_AUTOMATION.md; C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\deploy\LIVE_VPS_PLAYBOOK.md; C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\deploy\PRODUCTION.md; C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\deploy\release.json.example; C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\.agent\scripts-index.md  
**Changelog Updated:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\.agent\CHANGELOG.md

---

## Summary

The 0.2.0-11 through 0.2.0-13 deployment incident is reconstructed in a durable postmortem. The
normal release path now generates the next label, binds deployment class and both generated
changelog digests into immutable review/release authority, publishes exact prepared fragments with
crash recovery, runs selection plus isolated staging through one resumable `rehearse` command, and
requires a class-specific live preflight in the staging receipt. Current-generation maintenance
rehearsal evidence is accepted by the readiness audit. The live app-only preflight and production
health audit pass.

A single independent acceptance audit was scoped to the implementation commits. Its three concrete
findings were closed without reopening the broader audit: prepare now enforces generated release-label
sequence, the coordinator refuses the incomplete direct VPS staging entry point, and the direct
promotion gate derives mode from immutable authority and recomputes the preflight seal.

Cloudflared is now the sole installed Menhir ingress authority; the duplicate Caddy route and both
reconciliation units are retired. A dedicated ten-minute `security-config` transaction is installed
on the VPS. App-only, security-config, and maintenance promotions now retain a root transaction
receipt bound to the release, root runner, wrapper digests, elapsed time, and ingress/database
identities. OAuth key rotation mechanically escalates to maintenance.

This wrapup remains `PARTIAL` only because the mechanical wrapup validator is unavailable, the
protected publication environment is not configured, and the shared operator-scripts commit cannot
be safely pushed from a master branch that is 13 commits ahead and three behind its remote. The
Menhir branch itself is pushed. The implementation and live control-plane rehearsal are complete;
no new product release was invented merely to exercise a mutating cutover.

## Files Changed

| File | Why |
|------|-----|
| `.agent/CHANGELOG.md` | Record the deployment-reliability change. |
| `.agent/plans/README.md` | Index the reliability plan. |
| `.agent/plans/menhir-deployment-reliability-2026-09-07.md` | Define acceptance, rehearsal evidence, lane readiness, and infrastructure follow-up. |
| `.agent/reviews/menhir-release-0.2.0-13-postmortem.md` | Reconstruct the three-version incident and remediation status. |
| `.agent/scripts-index.md` | Document `next-id`, publication, and personal rehearsal entry points. |
| `deploy/LIVE_VPS_PLAYBOOK.md` | Correct live ingress topology and operator sequence. |
| `deploy/PRODUCTION.md` | Separate product release from personal deployment and record immutable bindings. |
| `deploy/RELEASE_AUTOMATION.md` | Document the future release/publish/rehearse/approve/promote method. |
| `deploy/build_install_bundle.py` | Require release/spec class and changelog digest agreement. |
| `deploy/installed-artifacts.json`, `deploy/release-install.sh`, `deploy/release-run.sh` | Remove Menhir-owned Caddy writers/artifacts and retain Cloudflared during maintenance. |
| `deploy/lib/menhir_schema.py` | Validate current release authority fields while retaining read-only legacy compatibility. |
| `deploy/personal_deploy.py` | Add resumable rehearsal, derived confirmations, strict authority agreement, and preflight validation. |
| `deploy/personal_promote.ps1` | Derive mode from immutable release authority and recompute the production-preflight seal before invoking the operator transaction. |
| `deploy/personal_security_config.ps1` | Upload the bounded security-config authority and retain the root transaction receipt. |
| `deploy/personal_stage_vps.py` | Add class-specific live preflight and bounded staging sidecars. |
| `deploy/release-author.py` | Include class and changelog digests in reviewed immutable release authority. |
| `deploy/release_spec.py`, `deploy/release-inputs.example.json` | Require and author the Cloudflared ingress authority. |
| `deploy/release.json.example` | Document the current authority schema. |
| `deploy/release_flow.py` | Add next-label generation/enforcement and atomic, recoverable fragment publication. |
| `deploy/scaffold/menhir_scaffold.py` | Accept fresh release rehearsal evidence bound to the current backup/release. |
| `deploy/scaffold/menhir_app_only.py` | Bind successful app-only receipts to unchanged Neo4j and Cloudflared container identities. |
| `deploy/scaffold/menhir_security_config.py` | Classify, apply, accept, and roll back the dedicated config/application transaction. |
| `deploy/scaffold/contract.production.json`, `deploy/scaffold/install.sh`, `deploy/scaffold/menhir-scaffold.sudoers` | Install, attest, and narrowly authorize both root runners. |
| `deploy/ansible/**` | Converge bounded host prerequisites and verify them over SSH without owning application releases. |
| `.github/workflows/release-image.yml` | Validate and publish release images with a protected environment and attestations. |
| `tests/test_install_bundle_builder.py` | Cover authority/spec binding refusal. |
| `tests/test_personal_deploy.py` | Cover rehearsal, preflight, legacy refusal, and derived confirmation behavior. |
| `tests/test_personal_promote.py` | Cover PowerShell preflight promotion gates. |
| `tests/test_personal_stage_vps.py` | Cover live preflight classes, host failures, route failures, and sidecar limits. |
| `tests/test_release_author.py` | Cover current authority fields and legacy readability. |
| `tests/test_release_flow.py` | Cover label generation, authority agreement, publication atomicity, drift, and recovery. |
| `tests/test_scaffold_authority.py` | Cover current, stale, mismatched, and tampered rehearsal evidence. |
| `tests/test_ansible_host_state.py`, `tests/test_deployment_contracts.py`, `tests/test_release_image_workflow.py`, `tests/test_release_spec.py` | Cover host convergence, Cloudflared authority, workflow permissions/attestations, and release classification. |

## Verification

- `python -m pytest tests/test_release_flow.py tests/test_release_author.py tests/test_install_bundle_builder.py tests/test_personal_deploy.py tests/test_personal_promote.py tests/test_personal_stage_vps.py tests/test_scaffold_authority.py -q` with third-party plugin autoload disabled — `PASS` — 162 passed, 3 skipped, zero failed in 511.53 seconds.
- `python -m pytest tests/test_personal_deploy.py -q` with third-party plugin autoload disabled after adding legacy-absence coverage — `PASS` — 20 passed, zero failed in 1.31 seconds.
- Independent acceptance audit of commits `62a5368` and `476f5ef` — `COMPLETE` — three actionable gate findings; no broad audit restart.
- `python -m pytest tests/test_release_flow.py tests/test_personal_deploy.py tests/test_personal_promote.py -q` with third-party plugin autoload disabled after closing only those findings — `PASS` — 60 passed, zero failed in 22.88 seconds.
- `ruff check <changed Python files and focused tests>` — `PASS` — all checks passed.
- `python -m py_compile <changed Python deployment files>` — `PASS` — exit code 0.
- Focused deployment/control-plane suite after root-receipt and security-config work — `PASS` — 104 passed in 28.79 seconds; the final OAuth-key escalation delta passed its three targeted tests.
- `ruff check` on the final changed Python deployment surface plus `git diff --check` — `PASS`.
- PowerShell parser check on both repository wrappers and all three shared operator wrappers — `PASS`.
- Final Ansible `--check --diff` against the VPS — `PASS` — changed=0 after correcting the tmpfiles template; SSH pytest-testinfra — `PASS` — 17 passed.
- Live scaffold install/verify — `PASS` — sudoers parsed, static contract verified, audit timer active; installed security runner SHA-256 `516f4e4d63bd3d86f5a5dad5af91b52e0c2413d51bfd396289cf7748d99d7986`.
- Post-install production non-interference — `PASS` — app `2c5d53...` healthy, Neo4j `9b2ac4...` healthy, Cloudflared `23ac64...` running with service label `cloudflared`, OAuth operations active, `/readyz` ready production, unauthenticated `/mcp-http` 401.
- `python deploy/release_notes.py validate deploy/changes/unreleased` — `PASS` — validated 9 release-note fragments.
- `python deploy/release_flow.py next-id --prior-release <copy of live 0.2.0-13 authority>` — `PASS` — generated `menhir-prod-0.2.0-14`.
- `python -m menhir artifacts validate . --repository menhir` — `FAIL` — validated 220 records but reported 22 pre-existing corpus findings outside this change; the new plan and wrapup produced no new reported finding.
- New live app-only `_production_preflight` against unchanged 0.2.0-13 — `PASS` — exact app/database image digests, healthy services, completed journal, expected network roles, 105,022,050,304 free disk bytes, and 7,496,204,288 available memory bytes; receipt digest `be492ac57a33650251586580429992c05fb299eec8f015ea20e5be1928a287d3`.
- Live `verify-artifacts` plus Docker/systemd snapshot — `PASS` — exact app and Neo4j healthy, all installed artifact checks `OK`, retired Caddy writer units absent, audit timer active, scaffold audit result `success`/0.
- Public endpoint snapshot from the VPS — `PASS` — `/readyz` 200, OAuth metadata 200, unauthenticated `/mcp-http` 401.
- Fresh full isolated 0.2.0-13 staging under the new coordinator — `NOT RUN` — 0.2.0-13 is a legacy authority and cannot be relabeled as a new immutable release; its retained exact-image staging receipt already passed all 17 behavioral checks and the new Cloudflared preflight passed separately.
- Mechanical wrapup validator — `NOT RUN` — no `artifact_validate(artifact_type="wrapups")` tool is available in this harness; repository-wide artifact validation was run and is reported above.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `yes`, except GitHub publication-environment activation blocked by desktop authentication
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`

## Assumptions

1. The retained 0.2.0-13 staging receipt remains valid evidence for unchanged OAuth/MCP/restart and
   rollback test behavior; the new host preflight was exercised separately and read-only.
2. A person with unrestricted root/SSH access remains a recovery authority and can bypass local
   coordinators. Normal deployment relies on the documented entry points and fixed sudoers surface.

## Risks / Gaps

1. The next genuine app-only or security-config release is still the first mutating use of the new
   root receipt contract; unit, wrapper, scaffold, staging, rollback simulation, and live
   non-interference checks passed, but no synthetic production release was created.
2. The Menhir branch is remote-active, but PR creation/protected-environment configuration still
   needs explicit external-publication authorization. Shared operator scripts commit `b2fbde79`
   remains local because its master branch is 13 ahead and three behind `origin/master`.
3. The repository artifact corpus has 22 pre-existing validation findings unrelated to this work.
4. Unrestricted root/SSH remains the intentional recovery authority outside normal fail-closed paths.

## Follow-Up Tasks

1. Open the pushed Menhir branch as a PR and configure the protected
   `menhir-release-publication` environment before enabling publication. Reconcile the shared
   scripts repository separately, then publish commit `b2fbde79` without dragging unrelated commits.
2. Use the documented `rehearse -> approve -> promote` path for the next real release and retain both
   receipt files as the first mutating production evidence.
3. Keep OAuth signing-key rotation in maintenance until a separate coordinated private/public-key
   rotation transaction is designed and rehearsed.

## Invariant Verification

```text
Invariant: Every normal personal promotion of a Menhir product release must use the exact reviewed deployment class, generated changelog digests, passing class-specific production preflight, staging receipt, and owner approval bound to that release.
Authority and refusal outcome: release.json plus release-flow/personal-deployment receipts; selection, staging, approval, or promotion exits nonzero before production mutation.
System boundary: Menhir product release coordinator, release author/bundle builder, personal coordinator, desktop staging/promotion wrappers, VPS staging runner, scaffold readiness, app-only/security-config/maintenance root runners, and unrestricted root/SSH recovery administration.
In-repo paths: release_flow prepare/finalize/publish, personal_deploy rehearse/approve/promote, personal_promote.ps1, personal_security_config.ps1, personal_stage_vps.py, menhir_app_only.py, menhir_security_config.py, release-run.sh, and menhir_scaffold.py; census based on source search because the structure index was stale.
External paths: three committed shared desktop wrappers at b2fbde79 and root/SSH recovery administration; controls/evidence: fixed sudoers commands, root runner digests, owner approval, and live scaffold verification.
Enforcement point: release_flow._verify_next_release_id, personal_deploy._release_binding and _validate_staging_receipt, plus personal_promote.ps1; required context: prior release ID, release/state digests, deployment class, changelog digests, preflight, staging receipt, and approval.
Atomicity: release publication uses a nonce-bound staged directory and atomic renames; personal state uses atomic replacement; each production lane uses the host-wide lock, root-owned staged files, durable journals, and class-specific rollback.
Accommodations: legacy release authorities remain readable by schema/audit but are refused for new personal selection; tested yes.
NULL/absent behavior: missing current class, changelog, preflight, receipt, or approval fields fail closed; tested yes.
Staging: exact 0.2.0-13 behavioral rehearsal retained; class-specific preflight and root classifiers are tested; the live scaffold update was rehearsed without replacing the running app.
Recovery: interrupted publication resumes only its nonce-bound archive; app-only and security-config automatically restore their exact prior authority; maintenance retains generation-based recovery.
Deployed versions: live 0.2.0-13 app digest sha256:c228117f6b9a0e65badd4eac3c54072a3d47b6bae0a60ae3a8d41e38749fa115 and Neo4j digest sha256:535fc4b4bc9e079630c9adc6d2accc6b5af0cc14980cf94008119155999e2944.
Live proof: Cloudflared-only ingress retirement is committed on the VPS as cc0510c; scaffold receipt 8f5976... verifies both narrow root runners; public readiness/OAuth/MCP authentication boundary returned ready/active/401 without changing the app, database, or ingress IDs.
Remaining assumptions: unrestricted root remains outside fail-closed application enforcement; the next genuine release supplies the first mutating end-to-end receipt.
```
