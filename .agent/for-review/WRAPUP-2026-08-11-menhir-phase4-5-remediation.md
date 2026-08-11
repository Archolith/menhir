# WRAPUP REVIEWED WITH FINDINGS - Menhir Phase 4/5 artifact reconciliation remediation

**Date:** 2026-08-11
**Agent:** Codex
**Model:** GPT-5
**Status:** REVIEWED WITH FINDINGS
**Plan / Ticket:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md`
**Worktree:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation`
**Branch:** `agent/menhir-phase5-closeout`
**Commits:** `7a19426297f022ed887d22a255a7d01c31e7b9d2`, `59a45c727244d2de2f44699fc939ac2f1489d831`, `0779c15fb120a0338b40c64af3bf326e9b805a51`, `fe8bfdc78474a89c0e17f753c63c8652097e45a6`, `2e18832811c4fd19f494ddafe858181a908df15b`, `1fcd050848da1e78ef9768993b080ce6ff199786`, `338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd`
**PR:** [#8](https://github.com/Archolith/menhir/pull/8)
**Merge Commit / Closeout Base:** `338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd`
**Verification Scope:** verifier-supplied hosted CI, production backup and index repair, source-v2 preparation, reconciliation apply, repeat audit, and direct acceptance evidence
**Docs Updated:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\for-review\WRAPUP-2026-08-11-menhir-phase4-5-remediation.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\CHANGELOG.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\docs\hook-center-tool-events.md`, `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\scripts\hooks\README.md`, `C:\Users\thron\IdeaProjects\.agent\handoffs\HANDOFF-2026-08-11-menhir-artifact-reconciliation-phase5.md`, `C:\Users\thron\IdeaProjects\.agent\handoffs\MENHIR-PHASE5-LIFECYCLE-CONTRADICTIONS-2026-08-11.md`
**Changelog Updated:** `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\CHANGELOG.md`

---

## Before Writing

The Phase 5 acceptance criteria were traced backwards from zero-repeat reconciliation and a current
persisted cursor. That exposed three prerequisites: acknowledged source-less conflicts must not pin
the cursor forever, Hook Center must carry canonical repository identity across worktrees, and the
source-v2 backfill must install and verify constraints before corpus repair. Those prerequisites
were merged in PR #8. The owner then approved the backup, index consistency repair, graph-wide
preparation, reconciliation digest, first apply, and zero-mutation second apply. This closeout records
that completed Phase 5 operation; it does not close or authorize Phase 6.

---

## Summary

Phases 4 and 5 are complete. PR #8 merged at
`338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd`, and both hosted CI jobs passed. Hook events now keep
structural project scope separate from stable artifact repository identity. The production source-v2
preparation stamped 112 source UUIDs and locator keys, left every duplicate category at zero, and
placed all four uniqueness constraints and backing indexes `ONLINE` at 100 percent.

The approved reconciliation applied 191 safe actions with no skips: 13 relocations, 29 refreshes,
137 registrations, and 12 unresolved-source markings. The cursor advanced despite 12 reviewed
source-less `UNCLASSIFIED_NEW_SOURCE` conflicts. Repeat audit reported 179 `NOOP` actions, the same
12 conflicts, zero safe mutations, and the cursor exactly at the `338b1cb` production baseline. A
second apply wrote nothing. All 29 current plans now have exactly one artifact and one resolvable source. The resulting
106 lane/lifecycle contradictions are classification debt for owner disposition; no lifecycle was
inferred or mutated. Phase 6 remains separately owner-gated, and this wrapup remains `PARTIAL`
because `artifact_validate` is unavailable and closeout publication has not yet been reconciled.

**Closeout publication caveat:** This plan, wrapup, and changelog are themselves in Menhir's scanned
corpus. Once their eventual docs commit merges, the persisted cursor at `338b1cb` will be behind the
new observed commit. A fresh read-only audit, owner approval of its exact digest, one apply, and a
zero-repeat re-audit are required after the docs merge. Phase 5's production repair at `338b1cb` is
complete; the post-doc cursor must not be described as current until that sequence completes.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\plans\menhir-work-artifact-reconciliation-2026-08-11.md` | Correct Phase 4/5 cursor, worktree identity, preparation, backup, and 29/25 acceptance gates. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\CHANGELOG.md` | Record the remediation and verification evidence. |
| `C:\Users\thron\Documents\Codex\2026-08-10\mes-3\menhir-phase45-remediation\.agent\for-review\WRAPUP-2026-08-11-menhir-phase4-5-remediation.md` | Record completed Phase 5 evidence and the remaining Phase 6 gate. |
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
| `C:\Users\thron\IdeaProjects\.agent\handoffs\HANDOFF-2026-08-11-menhir-artifact-reconciliation-phase5.md` | Record the workspace-level Phase 5 operator procedure and evidence. |
| `C:\Users\thron\IdeaProjects\.agent\handoffs\MENHIR-PHASE5-LIFECYCLE-CONTRADICTIONS-2026-08-11.md` | Record the 106-item lifecycle contradiction queue for owner disposition. |

## Verification

The following results were supplied by the central verifier; no command in this section was run by
the closeout author during this documentation edit.

- Earlier local focused verification: `PASS`, 133 passed.
- Earlier full offline suite: `PASS`, 5,947 passed and 197 skipped.
- Earlier isolated Neo4j acceptance: `PASS`, 21 passed.
- Hosted CI for PR #8: `PASS`; `offline-tests` completed in 2m00s and `online-tests` in 2m14s.
- Canonical checkout: `PASS`; `main` was fast-forwarded without a branch switch to
  `338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd`, the unrelated untracked
  `.agent/plans/menhir-post-install-and-agent-defaults-plan.md` was preserved, and repository-local
  Git config is `menhir.artifactRepository=menhir`.
- Pre-repair dump: `/home/ctharvey/menhir-neo4j/backups/phase5-20260811T211356Z`.
  `neo4j.dump` SHA-256 is
  `44f135b570aa27ca70a9617387bc1af022a0995e651e52822ccb7fb33d339df8`; `system.dump`
  SHA-256 is `a7edf7885688b785d85ab5dfdc9d9110ff39ef851c4e453ca07c138ecbcb756a`.
- Restore/consistency validation found five stale standalone Entity RANGE-index entries across
  `name_entity_index`, `created_at_entity_index`, `entity_last_accessed_idx`, `entity_uuid`, and
  `entity_freshness_idx`. No constraints backed them. With owner approval and Menhir/watchdog
  paused, all five were recreated from `SHOW INDEXES` `createStatement` and reached `ONLINE` at
  100 percent.
- Post-repair dump:
  `/home/ctharvey/menhir-neo4j/backups/phase5-post-index-repair-20260811T212419Z`.
  `neo4j.dump` SHA-256 is
  `f93e6b1c6afa626d8b7eb7ebcbaa2b71c01ecb0c80b7bebb1db1e582abefd2c7`; `system.dump`
  SHA-256 is `49ceacafc2d6debab2e629e4bb061f39081670610989fa9f14de1984fc2017d4`.
  Both post-repair consistency checks passed.
- Service restoration: `PASS`; server HTTP ready, Neo4j up, and watchdog task enabled/Ready on
  Neo4j Community 5.26.26 at `ubuntu-server`.
- Preparation preflight: 112 sources, 112 missing source UUIDs, 112 missing locator keys, and zero
  duplicates in all five categories. Owner-approved apply stamped all 112 UUIDs and keys. Postflight
  missing and duplicate counts were all zero.
- Constraint acceptance: `artifact_reconcile_cursor_repository_unique`,
  `artifact_source_locator_unique`, `artifact_source_uuid_unique`, and
  `work_artifact_uuid_unique`, with all backing indexes `ONLINE` at 100 percent.
- Merged-tree dry-run: observed `338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd`; digest
  `1479335f132bdd92915b3312a26bb157d0d8fcae7e2d114241f6b993055ac3d4`; corpus 191;
  graph sources 54; 13 relocations, 29 refreshes, 137 registrations, 12 unresolved marks, 12
  source-less `UNCLASSIFIED_NEW_SOURCE` conflicts, and 12 then-visible lifecycle contradictions.
- First apply `9cb08c3e-b499-4f23-9b86-2d9997d84e62`: applied 191, skipped zero, retained 12
  conflicts, and advanced the cursor.
- Repeat audit: graph sources 191; 179 `NOOP`; 12 source-less conflicts; zero safe mutations; cursor
  exactly `338b1cb8dc25f9134ccd015edbe6aa0d4563a1cd` for the production checkpoint; digest
  `159bc5590794143daa7e14513b171c6ff8e4c3beb32a01977b2d25095c407ac9`.
- Second apply `1f19dedf-1155-47ea-91a2-89d6bff75eb7`: applied zero, skipped zero, retained 12
  conflicts, and kept the cursor current for the `338b1cb` production baseline.
- Direct acceptance: 29 of 29 current plans have exactly one artifact and one resolvable source;
  duplicate locator groups are zero; all 12 unresolved sources have
  `resolution_reason=source_not_observed_in_corpus_scan`.
- Post-registration audit: 106 lane/lifecycle contradictions remain for owner disposition. No
  lifecycle was inferred or mutated.
- `artifact_validate(artifact_type="wrapups", ...)`: `NOT RUN`; the validator is unavailable in this
  harness, so status remains `PARTIAL`.
- Post-doc-merge audit/apply/zero-repeat: `PENDING`; no closeout docs commit exists yet, and no
  post-doc cursor is claimed current.

## Claim Cross-Check

- Summary checked against verifier-supplied evidence: `yes`
- Files Changed retained from the implementation wrapup record: `yes`
- PR and merge commit copied from verifier-supplied evidence: `yes`
- Verification and production results copied from verifier-supplied evidence: `yes`
- Git, build, test, and graph commands run by this closeout author: `none`

## Completion Checklist

- Phases 0-5 acceptance criteria completed: `yes`
- Phase 6 completed: `no - separately owner-gated`
- Docs updated as required: `yes`
- Current Phase 5 closeout changelog entry added: `yes`
- Workspace operator handoff and 106-item contradiction report referenced: `yes`
- Implementation merged: `yes - PR #8`
- Phase 5 production repair at `338b1cb` complete: `yes`
- Closeout publication reconciled after docs merge: `no - pending audit, approved digest, apply, and zero-repeat re-audit`
- Closeout document committed: `not yet; its eventual commit is intentionally omitted`

## Assumptions

1. The 12 remaining conflicts stay source-less until an owner-approved metadata pass classifies
   their documents.
2. The 106 lane/lifecycle contradictions require human semantic disposition; their directory lanes
   alone are not lifecycle evidence.
3. Phase 6 remains a separate approval and must not be inferred from Phase 5 completion.
4. The cursor evidence is current only for the `338b1cb` production baseline, not for the eventual
   closeout docs commit.

## Risks / Gaps

1. `artifact_validate` is unavailable in this harness, so the wrapup cannot be validator-cleared and
   remains `PARTIAL`.
2. The 106 lane/lifecycle contradictions are prominent remaining classification debt. Resolving them
   requires owner decisions and may feed Phase 6; automatic path-based lifecycle mutation remains
   prohibited.
3. The 12 source-less `UNCLASSIFIED_NEW_SOURCE` conflicts remain intentionally visible. They do not
   compromise existing source identity, but they will repeat until explicit metadata is approved.
4. Merging these closeout artifacts will put the scanned corpus ahead of the persisted cursor. The
   required post-merge reconciliation sequence remains incomplete.

## Follow-Up Tasks

1. After the closeout docs commit merges, run a fresh read-only audit, obtain owner approval of the
   exact digest, apply once, and verify a zero-repeat re-audit with the cursor at the new commit.
2. Review and disposition the 106 lane/lifecycle contradictions one by one without inferring status
   from location.
3. Decide whether to add explicit metadata for the 12 source-less unclassified documents in a
   separately approved documentation pass.
4. Gate and execute Phase 6 only after owner approval of the metadata/status vocabulary and exact
   corpus scope.
5. Run `artifact_validate` in a harness where it is available. Update this wrapup status only after
   validation and the post-doc-merge reconciliation complete; Phase 6 remains separately gated.

## Notes

- Phase 5 production graph mutation occurred only after the recorded owner approvals and verified
  Neo4j dump/consistency gates.
- The temporary isolated-Neo4j container and network used for live tests were removed after the tests passed.
- The `Commits:` list includes the known implementation commits, prior wrapup commit, and PR #8
  merge. The new closeout docs commit is omitted because it does not exist yet and cannot truthfully
  be recorded in the file it will commit.
- No post-doc cursor-current claim is made. The required closeout reconciliation begins only after
  the docs commit merges.

## Review Findings

**Reviewed:** 2026-08-11
**Reviewer:** Codex (GPT-5)
**Implementation score:** 99/100 (A)
**Wrapup score:** 97/100 (A)

No P1-P3 implementation or reporting defect remains after the documentation corrections made during
review. The review independently confirmed the plan-to-implementation mapping, PR and commit anchors,
production cursor and acceptance counts, all four live backup hashes, and an exact row-for-row match
between the 106-item owner-disposition report and the live JSON audit.

Open closeout gates are accurately disclosed rather than treated as defects: the closeout docs are
not yet committed or merged, their merge will require a newly approved reconciliation digest and
zero-repeat proof, and `artifact_validate` is unavailable in this harness. Phase 6 and the 106
lifecycle decisions remain separately owner-gated. No archolith session grade applies because this
was a native Codex task rather than an archolith-context proxy session.
