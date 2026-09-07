# WRAPUP — Menhir deployment reliability

**Date:** 2026-09-07  
**Agent:** Codex  
**Model:** gpt-5.6-sol  
**Session:** unavailable  
**Status:** PARTIAL  
**Plan / Ticket:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion\.agent\plans\menhir-deployment-reliability-2026-09-07.md  
**Worktree:** C:\Users\thron\Documents\Codex\2026-09-06\inve\work\menhir-doc-staged-promotion  
**Branch:** fix/deployment-reliability-20260907  
**Commits:** 62a536854b69b25611ab70faea8ea080869ee824; 476f5efc944e64f8aa9ac5b9c0adae7f1590810c; 5b8db26d0f7649575b1f28d79425a6de96c5e65c
**Verification Scope:** commits 62a536854b69b25611ab70faea8ea080869ee824, 476f5efc944e64f8aa9ac5b9c0adae7f1590810c, and 5b8db26d0f7649575b1f28d79425a6de96c5e65c, plus the live 0.2.0-13 read-only rehearsal and health audit recorded below
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

This wrapup is `PARTIAL` because two intentionally disclosed system-level items remain: a dedicated
security-config transaction/receipt is not implemented, and maintenance is fail-closed until the
Cloudflared-versus-Caddy ingress authority is resolved. Promotion receipts also do not yet bind the
underlying transaction receipt and fixed operator-wrapper digest.

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
| `deploy/lib/menhir_schema.py` | Validate current release authority fields while retaining read-only legacy compatibility. |
| `deploy/personal_deploy.py` | Add resumable rehearsal, derived confirmations, strict authority agreement, and preflight validation. |
| `deploy/personal_promote.ps1` | Derive mode from immutable release authority and recompute the production-preflight seal before invoking the operator transaction. |
| `deploy/personal_stage_vps.py` | Add class-specific live preflight and bounded staging sidecars. |
| `deploy/release-author.py` | Include class and changelog digests in reviewed immutable release authority. |
| `deploy/release.json.example` | Document the current authority schema. |
| `deploy/release_flow.py` | Add next-label generation/enforcement and atomic, recoverable fragment publication. |
| `deploy/scaffold/menhir_scaffold.py` | Accept fresh release rehearsal evidence bound to the current backup/release. |
| `tests/test_install_bundle_builder.py` | Cover authority/spec binding refusal. |
| `tests/test_personal_deploy.py` | Cover rehearsal, preflight, legacy refusal, and derived confirmation behavior. |
| `tests/test_personal_promote.py` | Cover PowerShell preflight promotion gates. |
| `tests/test_personal_stage_vps.py` | Cover live preflight classes, host failures, route failures, and sidecar limits. |
| `tests/test_release_author.py` | Cover current authority fields and legacy readability. |
| `tests/test_release_flow.py` | Cover label generation, authority agreement, publication atomicity, drift, and recovery. |
| `tests/test_scaffold_authority.py` | Cover current, stale, mismatched, and tampered rehearsal evidence. |

## Verification

- `python -m pytest tests/test_release_flow.py tests/test_release_author.py tests/test_install_bundle_builder.py tests/test_personal_deploy.py tests/test_personal_promote.py tests/test_personal_stage_vps.py tests/test_scaffold_authority.py -q` with third-party plugin autoload disabled — `PASS` — 162 passed, 3 skipped, zero failed in 511.53 seconds.
- `python -m pytest tests/test_personal_deploy.py -q` with third-party plugin autoload disabled after adding legacy-absence coverage — `PASS` — 20 passed, zero failed in 1.31 seconds.
- Independent acceptance audit of commits `62a5368` and `476f5ef` — `COMPLETE` — three actionable gate findings; no broad audit restart.
- `python -m pytest tests/test_release_flow.py tests/test_personal_deploy.py tests/test_personal_promote.py -q` with third-party plugin autoload disabled after closing only those findings — `PASS` — 60 passed, zero failed in 22.88 seconds.
- `ruff check <changed Python files and focused tests>` — `PASS` — all checks passed.
- `python -m py_compile <changed Python deployment files>` — `PASS` — exit code 0.
- `python deploy/release_notes.py validate deploy/changes/unreleased` — `PASS` — validated 9 release-note fragments.
- `python deploy/release_flow.py next-id --prior-release <copy of live 0.2.0-13 authority>` — `PASS` — generated `menhir-prod-0.2.0-14`.
- `python -m menhir artifacts validate . --repository menhir` — `FAIL` — validated 220 records but reported 22 pre-existing corpus findings outside this change; the new plan and wrapup produced no new reported finding.
- New live app-only `_production_preflight` against unchanged 0.2.0-13 — `PASS` — exact app/database image digests, healthy services, completed journal, expected network roles, 105,022,050,304 free disk bytes, and 7,496,204,288 available memory bytes; receipt digest `be492ac57a33650251586580429992c05fb299eec8f015ea20e5be1928a287d3`.
- Live `verify-artifacts` plus Docker/systemd snapshot — `PASS` — exact app and Neo4j healthy, all installed artifact checks `OK`, route watcher and audit timer active, scaffold audit result `success`/0.
- Public endpoint snapshot from the VPS — `PASS` — `/readyz` 200, OAuth metadata 200, unauthenticated `/mcp-http` 401.
- Fresh full isolated 0.2.0-13 staging under the new coordinator — `NOT RUN` — 0.2.0-13 is a legacy maintenance authority and maintenance now correctly refuses the unresolved ingress mismatch; its retained exact-image staging receipt already passed all 17 behavioral checks.
- Mechanical wrapup validator — `NOT RUN` — no `artifact_validate(artifact_type="wrapups")` tool is available in this harness; repository-wide artifact validation was run and is reported above.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `partial`
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`

## Assumptions

1. The retained 0.2.0-13 staging receipt remains valid evidence for unchanged OAuth/MCP/restart and
   rollback test behavior; the new host preflight was exercised separately and read-only.
2. A person with unrestricted root/SSH access remains a recovery authority and can bypass local
   coordinators. Normal deployment relies on the documented entry points and fixed sudoers surface.

## Risks / Gaps

1. `security-config` still maps to the maintenance transaction and has no ten-minute readiness claim.
2. Maintenance remains blocked because Cloudflared owns the live `.2` ingress role while reviewed
   Caddy assets assume that address; TLS entries also include directories and missing AOP files.
3. The personal promotion receipt is still synthesized from the wrapper exit result. It does not bind
   a fixed operator-wrapper digest, underlying root transaction receipt, elapsed time, or explicit
   unchanged Neo4j/ingress identities.
4. The repository artifact corpus has 22 pre-existing validation findings unrelated to this commit.

## Follow-Up Tasks

1. Choose Cloudflared or Caddy as the single Menhir ingress authority, encode that mode in release
   authority, remove the inactive duplicate route, and rehearse mode-specific rollback.
2. Implement and rehearse the dedicated security-config transaction and ten-minute end-to-end receipt.
3. Bind the fixed operator wrapper and root transaction result into the personal promotion receipt.
4. Express persistent host state in Ansible check/diff mode and verify it with pytest-testinfra over SSH;
   later move image publication to a reusable GitHub workflow with provenance attestations and a
   protected environment.

## Invariant Verification

```text
Invariant: Every normal personal promotion of a Menhir product release must use the exact reviewed deployment class, generated changelog digests, passing class-specific production preflight, staging receipt, and owner approval bound to that release.
Authority and refusal outcome: release.json plus release-flow/personal-deployment receipts; selection, staging, approval, or promotion exits nonzero before production mutation.
System boundary: Menhir product release coordinator, release author/bundle builder, personal coordinator, desktop staging/promotion wrappers, VPS staging runner, scaffold readiness, app-only/maintenance root runners, and unrestricted root/SSH recovery administration.
In-repo paths: release_flow prepare/finalize/publish/deploy, release-author, build_install_bundle, personal_deploy select/rehearse/stage/approve/promote, personal_stage.ps1, personal_promote.ps1, personal_stage_vps.py, menhir_app_only.py, release-run.sh, and menhir_scaffold.py; census based on source search because the structure index was stale.
External paths: desktop operator wrapper and root/SSH administration; controls/evidence: fixed sudoers commands, handbook recovery-only designation, owner approval, and live installed-artifact verification.
Enforcement point: release_flow._verify_next_release_id, personal_deploy._release_binding and _validate_staging_receipt, plus personal_promote.ps1; required context: prior release ID, release/state digests, deployment class, changelog digests, preflight, staging receipt, and approval.
Atomicity: release publication uses a nonce-bound staged directory and atomic renames; personal state files use atomic replacement; production atomicity remains in the existing app-only/maintenance transaction and lock.
Accommodations: legacy release authorities remain readable by schema/audit but are refused for new personal selection; tested yes.
NULL/absent behavior: missing current class, changelog, preflight, receipt, or approval fields fail closed; tested yes.
Staging: exact 0.2.0-13 behavioral rehearsal retained; new app-only host preflight passed live; maintenance refused before mutation on known ingress drift.
Recovery: interrupted publication resumes only its nonce-bound archive; app-only and maintenance retain their existing rollback/recovery contracts.
Deployed versions: live 0.2.0-13 app digest sha256:c228117f6b9a0e65badd4eac3c54072a3d47b6bae0a60ae3a8d41e38749fa115 and Neo4j digest sha256:535fc4b4bc9e079630c9adc6d2accc6b5af0cc14980cf94008119155999e2944.
Live proof: read-only production preflight passed and public readiness/OAuth/MCP authentication boundary returned 200/200/401; new release code itself is not deployed.
Remaining assumptions: unrestricted root remains outside fail-closed application enforcement; dedicated security-config and ingress-mode authority are follow-up work.
```
