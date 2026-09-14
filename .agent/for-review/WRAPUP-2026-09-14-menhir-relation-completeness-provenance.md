# WRAPUP — Menhir relation completeness and provenance integration

**Date:** 2026-09-14
**Agent:** Codex
**Model:** GPT-5
**Session:** Not exposed by this harness
**Status:** COMPLETE — merged through PR #108; pull-request and post-merge `main` CI passed
**Plan / Ticket:** `C:\Users\thron\Documents\Codex\2026-09-13\it-i\outputs\menhir-relation-completeness-integration-plan.md`; Archolith/menhir#90; Archolith/menhir#94; Archolith/menhir#96
**Worktree:** `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration`
**Branch:** `integrate/relation-completeness-provenance-20260914`
**Commits:** `9dcdce33fe1e88cdb3b8e12d705ecc4949ace881`, `244e0123489ffab6cc264dc31b6eebdb18ad8672`, `8974ae15eedfc9221c548c40e72eaf195342c48a`, `6366817a11da457de9c825db3ac4a21b4b90d104`, `3e47ed1b1137a2c847e21dfe508c5e20894abcc5`, `f74b937c91fb755d7014073971e9aa1b80535453`
**Verification Scope:** six commits above, merged through PR #108 as `d7d79e35b20afb1488ec10b553e407c713f17b60`; pull-request and post-merge `main` CI passed
**Docs Updated:** `C:\Users\thron\Documents\Codex\2026-09-13\it-i\outputs\menhir-relation-completeness-integration-plan.md`; `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\.agent\for-review\WRAPUP-2026-09-14-menhir-relation-completeness-provenance.md`
**Changelog Updated:** `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\CHANGELOG.md`

---

## Before Writing

The plan was checked backward from its merge-ready end state. The accepted base is remotely durable
through PR #107. The five behavior commits were restacked in their preserved order, and the sixth
commit adds only directly related diagnostic coverage, a formatting correction, and the changelog
entry. The declared environment, affected suites, an isolated Neo4j test, a third-person three-call
model gate, a default scalar control, and the isolated LongMemEval date smoke were run. The remaining
publication, GitHub CI, merge, issue-closure, and remote-inclusion steps were subsequently verified. PR #108 merged
as `d7d79e35`; issues #90 and #94 closed; the merged integration and patch-equivalent source
branches/worktrees were removed during approved cleanup. Existing graph data was not migrated or repaired.

---

## Summary

Third-person episodes now receive no custom relation-completeness prompt and cannot be rebound to
the canonical `user`; first-person episodes continue to share one first-person predicate for prompt
and alias decisions. Named third parties remain typed-scalar subjects. Derived Menhir Views are
removed from Graphiti's semantic dedupe candidates, preventing later relationships from corrupting
View provenance. If an unchanged FACT refresh is refused, the diagnostic names node state,
MENTIONS parity, contributor scope/lifecycle, and fence generation. The implementation is locally
verified and merged to `origin/main` through PR #108.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\src\menhir\infrastructure\graphiti_extraction_patches.py` | Gates first-person instructions and removes custom third-person relation prompting. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\src\menhir\infrastructure\graphiti_model_patches.py` | Excludes structural/View candidates from Graphiti dedupe. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\src\menhir\infrastructure\view_write_repository.py` | Diagnoses the exact unchanged-FACT provenance refusal gate. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\src\menhir\services\typed_scalar_rules.py` | Keeps named third parties as scalar subjects. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\tests\test_relation_completeness_first_person_gate.py` | Covers first-, mixed-, and third-person prompt/repair behavior. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\tests\test_graphiti_view_candidate_isolation.py` | Covers View markers and candidate filtering. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\tests\test_typed_scalar_prompt_third_party_subject.py` | Covers named-third-party scalar extraction guidance. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\tests\infrastructure\test_view_refusal_diagnosis.py` | Directly probes refresh diagnostic fields and fallback behavior. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\tests\test_fact_provenance.py` | Proves broken MENTIONS parity is named and causes no further mutation on real Neo4j. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\CHANGELOG.md` | Records the integrated behavior while preserving the ten-entry cap. |
| `C:\Users\thron\IdeaProjects\.agent\worktrees\menhir-relation-completeness-integration\.agent\for-review\WRAPUP-2026-09-14-menhir-relation-completeness-provenance.md` | Records review anchors, evidence, and residual gaps. |

## Verification

- Focused and neighboring relation/View/scalar test matrix — `PASS` — 276 passed, 1 skipped, 1 warning on the five-commit restack. The exact consolidated invocation is not retained in this compacted session record, so this line is evidence of the observed result rather than a reproducible command transcript.
- `.\.venv\Scripts\python.exe -m pytest tests\infrastructure\test_view_refusal_diagnosis.py tests\test_graphiti_view_candidate_isolation.py -q` — `PASS` — 24 passed, 1 warning after the review correction.
- `.\.venv\Scripts\python.exe -m pytest tests\test_fact_provenance.py::test_refresh_refusal_names_broken_old_mentions_parity_without_mutating --run-online -q` — `PASS` — 1 passed, 1 warning against isolated Neo4j at `bolt://localhost:7688`.
- `uvx ruff check --select F811 --output-format concise .` — `PASS` — all checks passed.
- `uvx ruff check --select F821,ASYNC --output-format concise src` — `PASS` — all checks passed.
- `git diff --check` — `PASS` — exit code 0 and no whitespace findings.
- `SS_DIAG=1 bash scripts/run_scalar_state_e2e.sh` with `MENHIR_MAIN` set to this worktree — `PASS` — three third-person calls kept `Alice`, the non-View census was `Alice`, `37 coins`, `12 books`, and `7:30 AM` with no `user`, and a `clock_time` View stored `07:30`.
- Default `bash scripts/run_scalar_state_e2e.sh` scalar control — `PASS` — harness verdict PASS, 6 current Views, 8 assertions, zero duplicate keys/slots, no non-agent tiers, and no namespace leak; disclosed residual: 3/7 expected View slots committed and the money-event control produced a stray assertion.
- Isolated LongMemEval date smoke (`cc5ded98`, container `menhir-lme-datesmoke-integration-20260914`, Menhir `f74b937c`, Bench `d51b166c`) — `PASS WITH DISCLOSED HARNESS SEAM` — 23 turns, 23 ready, 0 failed, 23 `ADMITTED_ON` links, 100% user-sourced, 21 entities, and 38 `RELATES_TO` edges. The byte-identical valid-at verifier passed all 12 stored timestamps when launched from the canonical repository layout. The wrapper's own final invocation failed because the detached worktree changed its hard-coded sibling fixture lookup; ingest and graph verification did not fail.
- LongMemEval supersession inspection — `PASS WITH REPRESENTATION CAVEAT` — the current `user` summary contains only the expected “about two hours each day” state. A merged `coding challenges` topic summary retains both the historical one-hour and current two-hour text, and no explicit hour-valued `RELATES_TO` fact survived. The September 10 control note did not preserve comparable entity/edge totals, so no numeric-parity claim is made.
- `$env:SCHEDULER_URL='http://127.0.0.1:9'; .\.venv\Scripts\python.exe -m pytest -q` — `FAIL` — 9,626 passed and 380 skipped; only two tests failed because the deliberate URL override changed their asserted default from port 8082 to port 9.
- `.\.venv\Scripts\python.exe -m pytest tests\test_llama_endpoint.py::test_acquire_llama_url_async_ensures_scheduler tests\test_services_pipeline.py::test_processing_heartbeat_loop_pings_scheduler_for_scheduler_managed_graphiti -q` — `PASS` — both override-sensitive tests passed with their normal environment.
- `.\.venv\Scripts\menhir.exe artifacts validate . --repository menhir` — `BASELINE FINDINGS ONLY` — validated 225 records and reported the same 22 inherited corpus findings; none names this wrapup.
- GitHub PR and post-merge `main` CI — `PASS` — lint, offline tests, and online tests completed successfully for PR #108, and the subsequent `main` push workflow also passed.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`
- The original wrapup was committed at `f364384c`; this closeout update is part of the focused artifact-reconciliation commit.

## Completion Checklist

- Plan / acceptance criteria completed: `yes`
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`; the implementation merged through PR #108 and this wrapup records the closeout.
- PR publication, CI, merge, issue reconciliation, final plan state, and remote inclusion are complete.

## Assumptions

1. GitHub's clean-room offline and online jobs are the final authority for the two local scheduler-environment artifacts.
2. Existing graph corruption is outside this prevention-and-diagnostics change; no production graph was read or mutated.

## Risks / Gaps

1. Repository artifact validation still reports 22 inherited corpus findings, but none concerns this wrapup.
2. The default stochastic scalar benchmark passed its hard invariants but committed only 3/7 expected View slots and over-perceived the money-event control; that is disclosed rather than treated as part of these five fixes.
3. The LongMemEval control produced the correct current two-hour user summary, but the historical and current values coexist in a merged topic summary and not as an explicit hour-valued relation. This is model-output representation variance on a fixture already documented as stochastic, not evidence of a new first-person prompt regression.
4. Existing View-to-memory contamination is not migrated or cleaned; this patch prevents new candidate selection and improves refusal diagnosis.
5. The full local suite was not green in one invocation because isolating the active host scheduler changed two tests' literal expected URL; those exact tests passed immediately without the override. Per operator direction, the full suite will not be rerun.
6. The umbrella issue #90 plan was transitioned to `IMPLEMENTED` in its metadata and archived after the accepted merge. The graph had no artifact registered for its declared UUID, so no graph lifecycle row could be transitioned.

## Follow-Up Tasks

1. Complete: PR #108 was published and all GitHub lint/offline/online checks passed.
2. Complete: PR #108 merged as `d7d79e35`, and all reviewed commits are represented on `origin/main`.
3. Complete: issues #90/#94/#96 closed with the merge; #92/#95 remain untouched.
4. Complete for this artifact: repository validation ran and named no finding against this wrapup; the 22 inherited corpus findings remain separate maintenance debt.
5. Complete: remote inclusion was verified before the original source and integration branches/worktrees were removed.

## Notes

- Base PR #107 merged as `6eb4237f3abdaa14836513a3888df71e85d43eea` before the five fixes were restacked.
- The original source worktree at `cb078fe48710deaf7ccd2269bfeae5386bd5cf65` was removed after patch-equivalence and remote inclusion were verified.
- The integration has six commits because the sixth contains only review-requested tests, formatting, and changelog evidence; it adds no new feature behavior.
