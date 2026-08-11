# WRAPUP — Menhir Phase 4/5 artifact reconciliation remediation

**Date:** 2026-08-11  
**Agent:** Codex  
**Model:** GPT-5  
**Status:** PARTIAL  
**Plan / Ticket:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md`  
**Worktree:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation`  
**Branch:** `agent/menhir-phase45-remediation`  
**Commits:** `7a19426297f022ed887d22a255a7d01c31e7b9d2`, `59a45c727244d2de2f44699fc939ac2f1489d831`, `0779c15fb120a0338b40c64af3bf326e9b805a51`, `fe8bfdc78474a89c0e17f753c63c8652097e45a6`, `2e18832811c4fd19f494ddafe858181a908df15b`  
**Verification Scope:** the five work-product commits above plus this wrapup document; production graph checks were read-only  
**Docs Updated:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\docs\hook-center-tool-events.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\scripts\hooks\README.md`  
**Changelog Updated:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\CHANGELOG.md`

---

## Before Writing

The Phase 5 acceptance criteria were traced backwards from zero-repeat reconciliation and a current
persisted cursor. That exposed three prerequisites: acknowledged source-less conflicts must not pin
the cursor forever, Hook Center must carry canonical repository identity across worktrees, and the
source-v2 backfill must install and verify constraints before any corpus repair. Those are now
implemented and tested. The production backup, prepare, digest approval, apply, and zero-repeat pass
remain deliberately unexecuted pending the owner gate.

---

## Summary

Phase 4/5 is code-ready but not operationally complete. Hook events now keep structural project
scope separate from stable artifact repository identity. Apply advances the persisted cursor past
only source-less `UNCLASSIFIED_NEW_SOURCE` conflicts; every identity-bearing conflict, other conflict
class, and skipped write still blocks it. A new graph-wide preparation command is read-only by
default and requires an owner-approved source count before it backfills source-v2 fields, activates
four uniqueness constraints, and verifies their backing indexes `ONLINE`.

The full offline suite and isolated-Neo4j acceptance pass. A read-only production preflight measured
112 sources requiring preparation and zero duplicate blockers. The current Menhir audit remains the
expected 13 relocations, 29 refreshes, 136 registrations, 12 unresolved markings, and 12 source-less
reference conflicts. No production graph writes, service restart, branch push, PR merge, or runtime
configuration change occurred in this remediation.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md` | Correct Phase 4/5 cursor, worktree identity, preparation, backup, and 29/25 acceptance gates. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\CHANGELOG.md` | Record the remediation and verification evidence. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\for-review\WRAPUP-2026-08-11-menhir-phase4-5-remediation.md` | Record the implementation evidence and remaining production gate. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\docs\hook-center-tool-events.md` | Document stable artifact repository identity. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\scripts\hooks\README.md` | Document hook environment/Git configuration. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\scripts\hooks\menhir_file_event.py` | Emit explicit repository identity from environment or repository-local Git config. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\api\routes.py` | Keep structural project and artifact repository routing independent. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\api\routes_support.py` | Add and normalize the optional tool-event repository field. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\cli\artifacts.py` | Add separately gated graph preparation and retire reconcile's unsafe inline preparation. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\infrastructure\schema.py` | Centralize the four reconciliation constraints. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\infrastructure\work_artifact_repository.py` | Add global preflight, idempotent v2 backfill, constraint activation, and ONLINE verification. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\src\menhir\services\artifact_reconciliation_service.py` | Add narrow cursor-conflict policy and count-gated preparation ordering. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_artifact_reconciliation_entrypoints.py` | Cover preparation CLI write gates. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_artifact_reconciliation_live.py` | Verify source-v2 preparation against Neo4j. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_artifact_source_reconciliation_io.py` | Cover conflict classification, preparation ordering, duplicates, constraints, and unresolved-source idempotence. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_hook_center_artifact_reconciliation.py` | Cover worktree identity separation and visible missing identity. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_hook_center_tool_events.py` | Cover environment/Git-config identity resolution and precedence. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\tests\test_main_checks.py` | Replace a checkout-name allowlist with the actual repository-root invariant. |

## Verification

- `python -m compileall -q src` — `PASS` — all source files compiled.
- Focused reconciliation/entrypoint/schema/hook tests — `PASS` — 133 passed.
- `python -m pytest -p no:cacheprovider -q` — `PASS` — 5,947 passed, 197 skipped.
- `python -m pytest -p no:cacheprovider -q --run-online tests/test_phase_one_bootstrap_live.py tests/test_artifact_reconciliation_live.py` — `PASS` — 21 passed against the isolated port-7688 Neo4j container; the container and network were removed afterward.
- Production `source_preflight()` — `PASS` — read-only result: 112 sources, 112 missing source UUIDs, 112 missing current locator keys, and zero duplicate artifact UUID, source UUID, raw locator, materialized locator, or cursor-repository groups.
- `menhir artifacts audit --repo . --repository menhir --from-commit f441a23~1` against production — `PASS` — read-only digest `a8e75e6253bb39034771be72d0161587c7069f845fb28c1317a33adba9342a08`; 190 corpus entries, 54 Menhir graph sources, 13 relocations, 29 refreshes, 136 registrations, 12 unresolved markings, and 12 `UNCLASSIFIED_NEW_SOURCE` conflicts.
- Independent read-only cursor/hook review — `PASS` — no P0-P3 findings.
- Independent read-only preparation review — `PASS WITH TEST GAP REMEDIATED` — reviewer questioned unresolved-source re-keying; the intended null key was confirmed and a direct unresolved-row regression test was added and passed.
- Hosted CI — `NOT RUN` — branch has not been pushed.
- Production backup/prepare/apply/zero-repeat — `NOT RUN` — requires owner approval and a verified restorable backup path.
- `artifact_validate(artifact_type="wrapups", ...)` — `NOT RUN` — that validator tool is unavailable in this harness; status remains `PARTIAL`.

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

1. The 12 current reference conflicts remain source-less `UNCLASSIFIED_NEW_SOURCE` entries until a separately approved metadata pass.
2. The owner-approved preparation count remains 112 at execution time; the command refuses if it changes.
3. Phase 5 will run only from a merged commit with a newly generated digest.

## Risks / Gaps

1. `scripts/export_graph_backup.py` produces a self-consistent logical export, but this repository has no tested importer. Before production mutation, use a verified `neo4j-admin database dump` on the remote host or demonstrate restoration of the logical export into an isolated database.
2. The running Menhir server predates this branch and startup reconciliation is not configured with a repository path/identity. Phase 4 is not operationally active until merge, configuration, and restart.
3. The 12 reference conflicts will remain visible after repair. They are allowed to coexist with cursor advancement only because they carry no source or artifact identity.
4. Hosted CI and PR review have not run.

## Follow-Up Tasks

1. Push the branch, open a PR, run hosted CI, and obtain approval before merging to `main`.
2. Stop Menhir and the watchdog; create and restore-test a complete backup, including embeddings or an explicitly documented re-embedding route; record before counts.
3. Run `menhir artifacts prepare --apply --expected-source-count 112`, then verify all four constraint indexes are `ONLINE`.
4. Audit the merged commit with `--from-commit f441a23~1`; obtain owner approval of the new digest and exact action counts; apply once.
5. Re-audit: no safe mutation action may repeat, the 12 reviewed source-less conflicts may remain, and the Menhir cursor must equal the observed commit.
6. Configure repository-local `menhir.artifactRepository=menhir` and audit-only startup repository/path settings, restart Menhir, and verify startup output.

## Notes

- No production graph mutation occurred during this remediation.
- The temporary isolated-Neo4j container and network used for live tests were removed after the tests passed.
- The commit list names the work-product commits. This wrapup's own commit is omitted because a file
  cannot truthfully contain the hash of the commit that first adds it.
