# WRAPUP — Menhir canonical-self authority boundary

**Date:** 2026-09-05
**Agent:** Codex
**Model:** GPT-5
**Session:** Not exposed by harness
**Status:** READY FOR REVIEW
**Plan / Ticket:** `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\menhir-canonical-self-authority-boundary-2026-09-05.md`
**Worktree:** `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority`
**Branch:** `feat/canonical-self-authority-boundary-20260905`
**Commits:** `36925c342f9b03a745d02c97bdb229c217325784`, `e0dd6da0fba466c2a5cdce94aa35180d028b77e7`
**Verification Scope:** committed diff from plan commit `9a016bfa7800b1b736618c13dcb0b5fc726d2e63` through `e0dd6da0fba466c2a5cdce94aa35180d028b77e7`; wrapup checked in the current worktree
**Docs Updated:** `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\architecture.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\data_models.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\default-off-features.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\README.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\menhir-canonical-self-authority-boundary-2026-09-05.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\menhir-production-release-2026-09-04.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\workflows\canonical-self-migration-runbook.md`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.env.example`, `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\deploy\production.env.example`
**Changelog Updated:** `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\CHANGELOG.md`

---

## Before Writing

The plan was traced backward from its end state: no semantic assertion may attach to canonical self
without exact nondelegated owner confirmation; every alternate writer and reader must fail closed;
`off` and `observe` must retain compatibility; and activation must remain separately gated. That
trace exposed four final implementation gaps during independent review: the outer service builder
could degrade after an authority-patch startup failure, combined extraction did not report required
patch readiness, historical UUID-less self-alias Views could enter generic/ordinary recall, and the
flagged-bootstrap cache fingerprint did not share the row filter. All four are closed and were
re-audited clean. The authorized repository implementation is complete. Disposable database,
live-provider, deployment, activation, and historical remediation steps remain unrun because the
plan explicitly requires separate approval for them.

---

## Summary

Menhir now separates canonical structural identity from authority to assert facts about that
identity. In `enforce`, an exact Ed25519 owner confirmation is bound to the full assertion and
evidence lineage before Graphiti can persist a self edge. The final resolver and every authoritative
reader recheck direction, namespace, counterpart, semantics, temporal values, external episode,
internal Graphiti episode, and actual edge attribution. Confirmation capability is task-local;
unconfirmed material remains a bounded non-recallable proposal.

Alternate typed, event, View, replay, repair, merge, lifecycle, summary, and generic-context paths
are fenced. Historical self nodes and Views are withheld from default recall, while ordinary
entities merely named `user` remain eligible. `enforce` startup now aborts unless Graphiti-backed
reads and every authority-critical patch are available. Configuration remains default-off, and no
database, live provider, deployment, production setting, or historical graph was changed.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\CHANGELOG.md` | Record the authority boundary, audit fixes, and intentionally unrun activation work. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\architecture.md` | Document exact write/read authority, startup readiness, and residual boundaries. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\data_models.md` | Document confirmation proposals and persisted edge-lineage fields. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\default-off-features.md` | Register canonical-self enforcement as default-off. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\README.md` | Index the implementation plan. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\menhir-canonical-self-authority-boundary-2026-09-05.md` | Record implementation evidence and the remaining approval gates. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\plans\menhir-production-release-2026-09-04.md` | Keep the production release plan aligned with the new activation gate. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\workflows\canonical-self-migration-runbook.md` | Add configuration, signing, canary, rollback, and startup-failure procedures. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.env.example` | Document default-off authority configuration. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\deploy\docker-compose.production.yml` | Thread authority settings through the production container without activating them. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\deploy\production.env.example` | Document production authority variables as disabled defaults. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\pyproject.toml` | Include the cryptographic verification dependency. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\scripts\replay_fold_flags.py` | Prevent replay tooling from bypassing the authority mode. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\api\server_support.py` | Wire configured authority mode into the API graph adapter. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\cli\bootstrap.py` | Wire configured authority mode into CLI construction. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\config\settings_model.py` | Add strict binding-mode and offline-confirmation settings. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\core\bootstrap.py` | Wire the mode everywhere and fail `enforce` startup closed instead of degrading. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\core\runtime.py` | Carry authority mode through runtime-local graph construction. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\domain\self_authority.py` | Define canonical proposals, confirmation payloads, temporal normalization, and exact persisted-edge matching. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\domain\self_identity.py` | Separate self-like aliases from trusted subject evidence and keep deterministic UUID identity. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\explorer\app.py` | Wire authority mode into Explorer graph construction. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\consolidation_queries.py` | Fence repair, bridge, delete, and lifecycle mutations around structural self. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\correlation_queries.py` | Refuse merge/unmerge paths that would consume or recreate self authority. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\cypher.py` | Project self provenance and signed edge-lineage fields into readers. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\episode_lifecycle.py` | Prevent lifecycle operations from weakening structural-self constraints. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\episode_maintenance.py` | Preserve proposal/authority behavior during episode maintenance. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\graphiti_client.py` | Verify confirmations on read and require all authority-critical patches at startup. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\graphiti_extraction_patches.py` | Transport opaque endpoints, authorize exact edges, stamp lineage, and preserve signed values through final resolution. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\graphiti_model_patches.py` | Isolate canonical self from ordinary candidate resolution. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\graphiti_patches.py` | Re-export the authority patch family through the shared patch surface. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\memory_graph_adapter.py` | Propagate strict mode to all low-level repositories. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\memory_queries.py` | Exclude unverifiable self context, including normalized historical Views, from generic readers and cache fingerprints. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\scalar_view_repository.py` | Block scalar Views derived from unconfirmed self observations. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\self_authority.py` | Verify pinned Ed25519 confirmations from the offline owner-controlled directory. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\self_binding.py` | Enforce strict rollout parsing, structural predicates, and exact canonical binding. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\typed_assertion_models.py` | Deny direct typed-assertion attachment to canonical self under enforcement. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\typed_assertion_reconciliation.py` | Keep reconciliation from promoting unconfirmed self assertions. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\typed_assertion_write_repository.py` | Gate typed assertion writes at the database low point. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\typed_event_repository.py` | Gate typed event writes and reads for canonical self. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\infrastructure\view_write_repository.py` | Reject self-anchored and UUID-less self-alias Views at the shared writer. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\mcp\tools\ops\force_reenrich.py` | Allow operator-only re-enrichment of pending signed proposals without adding a signing surface. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\enrichment_steps.py` | Carry authority context through enrichment and retain bounded proposals. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\event_consolidation.py` | Keep canonical-self event outputs advisory under enforcement. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\ingest_queue.py` | Carry proposal and confirmation state through queue processing. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\ingest_service.py` | Configure authority mode and confirmation verifier at the ingest boundary. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\ingest_worker.py` | Install task-local authority capability during Graphiti dispatch. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\recall_pipeline.py` | Reverify signed self edges and isolate legacy self-derived nodes/Views before ranking. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\shadow_context_composition.py` | Apply the same signed-edge checks to shadow context. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\src\menhir\services\typed_scalar_service.py` | Make self-shaped scalar extraction proposal-only in enforcement. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_event_consolidation.py` | Cover event-lane authority gating. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_graphiti_combined_extraction_closure.py` | Cover exact signed-edge lineage and resolver preservation. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_live_vps_playbook.py` | Keep deployment-playbook contracts aligned without executing them. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_recall_event_authority_runtime.py` | Cover event-authority exclusion from runtime recall. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_recall_service.py` | Cover canonical nodes, historical Views, ordinary `user` entities, summaries, and scalar observations. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_self_authority.py` | Add end-to-end offline authority, bypass, startup, lineage, replay, and writer acceptance tests. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_shadow_context_composition.py` | Cover signed self-edge filtering in shadow composition. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\tests\test_typed_scalar_self_binding.py` | Cover typed-scalar proposal-only behavior. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\uv.lock` | Lock the cryptographic dependency graph. |
| `C:\Users\thron\Documents\Codex\2026-09-05\fihnd-x20\work\menhir-canonical-self-authority\.agent\for-review\WRAPUP-2026-09-05-menhir-canonical-self-authority.md` | Record the committed work, exact verification, independent audits, and remaining gates. |

## Verification

- `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.venv\Scripts\python.exe -m pytest -q -n 0 tests/test_self_identity.py tests/test_recall_service.py -m unit` — `PASS` — exit 0; 188 passed, 1 known dependency warning.
- `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.venv\Scripts\python.exe -m pytest -q -m unit -n 0` — `PASS` — exit 0; 5,809 passed, 10 skipped, 3,500 deselected, 3 known warnings in 329.55 seconds.
- `C:\Users\thron\IdeaProjects\projects\archolith\menhir\.venv\Scripts\python.exe -m compileall -q src tests` — `PASS` — exit 0 with no output.
- `git diff --check 9a016bfa7800b1b736618c13dcb0b5fc726d2e63..e0dd6da0fba466c2a5cdce94aa35180d028b77e7` — `PASS` — exit 0 with no output.
- `uv lock --check --offline` — `PASS` — exit 0; resolved 102 packages in 2 ms.
- `menhir artifacts validate . --repository menhir` — `FAIL` — exit 1; validated 216 records (including this wrapup) and reported 22 inherited findings. None names a file changed by this plan; the findings are one archived-plan status mismatch, six unindexed plans, one unindexed PDF, and fourteen reference records without declared types.
- Independent blocker re-audit by two read-only reviewers — `PASS` — both reported no remaining Critical, High, or Medium finding after the four blocker fixes and whitespace-normalization correction.
- Disposable Neo4j/Docker uniqueness, concurrency, rollback, and schema checks — `NOT RUN` — require separate approval and isolated storage under the plan.
- Live production-provider/model extraction and disposable canary — `NOT RUN` — require separate approval and exact candidate image/config.
- Deployment, production activation, and historical graph census/remediation — `NOT RUN` — explicitly outside this implementation authorization.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`

## Completion Checklist

- Plan / acceptance criteria completed: `partial` — the authorized repository implementation and offline acceptance criteria are complete; separately approved database/live/deployment/activation gates remain open.
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`

## Assumptions

1. The single-owner deployment and existing logical-to-physical namespace mapping remain the intended operating model.
2. The owner controls the pinned public key and confirmation directory outside every agent-callable surface.
3. `off` remains the deployed default until a separately approved release supplies disposable and live evidence.

## Risks / Gaps

1. Confirmation-file reads and Neo4j relationship persistence cannot share one transaction. Final resolution and every authoritative recall reverify the signature, but a narrow external-file/database race can leave a stored edge that is withheld on subsequent recall.
2. A stale summary whose historical self edge was already deleted has no surviving structural lineage to classify. Prevention is implemented; graph cleanliness still requires a separately approved read-only census and journaled remediation.
3. Database uniqueness, concurrent first-write behavior, rollback, real-provider extraction, exact-image canary, and production activation remain unverified because those operations were not authorized.
4. Repository artifact validation remains red on 22 inherited corpus findings unrelated to this plan.

## Follow-Up Tasks

1. Review commits `36925c342f9b03a745d02c97bdb229c217325784` and `e0dd6da0fba466c2a5cdce94aa35180d028b77e7` against the linked plan.
2. If approved, run the disposable Neo4j/Docker concurrency, uniqueness, rollback, and schema gates against this exact candidate.
3. If those pass, build the exact release image, run the approved live-provider/model canary, and make a separate activation decision with backup and rollback controls.
4. After prevention is proven, authorize a fresh read-only historical census before considering any journaled remediation.

## Notes

- No Docker container, database, live provider, deployment target, production setting, or historical graph was changed during this implementation.
- The plan correctly remains `IMPLEMENTING`; `READY FOR REVIEW` here means the committed repository implementation is reviewable, not that production activation is complete.
