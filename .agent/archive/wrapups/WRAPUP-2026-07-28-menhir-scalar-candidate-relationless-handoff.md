# WRAPUP — Menhir scalar candidate relationless-extraction remediation and continuation handoff

**Date:** 2026-07-28
**Agent:** Codex
**Model:** GPT-5 (Codex)
**Status:** PARTIAL
**Plan / Ticket:** None — ad hoc remediation of the active `scalar-current-candidate-v3-20260728` run
**Worktree:** `C:\Users\you\IdeaProjects\projects\archolith\menhir`
**Branch:** `main`
**Commits:** `811dd41b6af68ed45978fe91f8c06179340ec34b`, `012b0c53cbef83050bb9472dd010c05ae8ca448c`
**Verification Scope:** Menhir commits `811dd41b6af68ed45978fe91f8c06179340ec34b` and `012b0c53cbef83050bb9472dd010c05ae8ca448c`; archolith-bench run `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\results\lme-ku-buildout\scalar-current-candidate-v3-20260728` with a four-row manifest
**Docs Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-07-28-menhir-scalar-candidate-relationless-handoff.md`
**Changelog Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\CHANGELOG.md`

---

## Before Writing

There was no formal plan artifact. The required end state is a manifest-backed continuation of the
78-item candidate arm without replaying the four completed namespaces, with the repaired grocery
list turn retained and no terminal failure from empty assistant boilerplate. Working backwards:

1. The full candidate and recall score are still incomplete.
2. The next two-namespace window must complete and grow the manifest from four to six rows.
3. That window must rebuild `lme-945e3d21` and `lme-d7c942c3` from scratch because neither has a
   manifest row.
4. The exact grocery-list evidence atom must produce a grounded `user -> new app` relationship.
5. Generic assistant boilerplate that extracts only `user` and no edge must complete as intentional
   empty, not terminal failure.
6. Both code paths are implemented and offline-verified, but the final paid continuation has not
   been restarted after commit `012b0c5`.

The remaining benchmark work is therefore explicit in Risks / Gaps and Follow-Up Tasks.

---

## Summary

Commit `811dd41` fixes the real extraction miss that stopped the candidate run: the exact sentence
`I'm actually using a new app I recently downloaded.` returned one entity and zero edges in five of
five isolated `gpt-4o-mini` calls. Menhir now appends a first-person relation-completeness contract
and makes at most one focused corrective extraction call when a model response contains entities
but no usable edge. The normal edge-bearing path still makes one extraction call, caller
instructions are preserved, and a failed repair cannot erase the original underflow evidence.

The first live continuation on `811dd41` exposed a distinct policy case in
`lme-945e3d21`: generic assistant boilerplate asking the user to share work tasks returned only the
`user` label and no edge on both the initial and corrective calls. That is the entity-only form of
the existing assistant self-echo policy, not a missing first-hand memory. The run was stopped before
the unmanifested window could waste more paid work. Commit `012b0c5` now accepts only an explicitly
prefixed assistant turn whose entire extraction consists of self labels and zero raw edges as an
intentional empty result. User turns and assistant turns containing any non-self entity still enter
the repair/failure path.

The Menhir tree is clean at `012b0c5`. The candidate container is stopped. The manifest remains at
four completed items, so the benchmark resume code will preserve those four rows and fully delete
and rebuild only `lme-945e3d21` and `lme-d7c942c3`. Original, stopped-attempt, and active
mixed-code provenance are preserved in the run results directory.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\infrastructure\graphiti_extraction_patches.py` | Adds relation-completeness instructions, one bounded relationless repair, receipt observability, and the narrow assistant-self-only policy-empty guard. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\services\enrichment_steps.py` | Threads source provenance into the extraction receipt and reports repair/policy state on terminal errors and intentional-empty completion. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\tests\test_graphiti_combined_extraction_closure.py` | Covers the exact app turn, one-call success path, bounded repair, preserved underflow, caller instructions, assistant-self-only success, and non-self/user-turn fail-closed controls. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\CHANGELOG.md` | Records the measured 5/5 miss, live canary, bounded repair contract, and assistant policy-empty follow-up. |
| `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\results\lme-ku-buildout\scalar-current-candidate-v3-20260728\run_provenance.json` | Records the original four-item phase, stopped `811dd41` attempt, and `012b0c5` ready-to-resume boundary. |
| `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\results\lme-ku-buildout\scalar-current-candidate-v3-20260728\run_provenance.phase-1-c093783.json` | Immutable copy of original run provenance before the first repair continuation. |
| `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\results\lme-ku-buildout\scalar-current-candidate-v3-20260728\run_provenance.phase-2-811dd41-stopped.json` | Immutable copy of the stopped first remediation attempt. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-07-28-menhir-scalar-candidate-relationless-handoff.md` | Durable continuation procedure and verification contract. |

## Verification

- `python probe_exact_turn_extraction.py` against the production prompt/model before the fix — `PASS` — five of five isolated temperature-zero calls reproduced one entity and zero edges for the exact app sentence; no graph writes were made.
- `python probe_exact_turn_extraction.py` through the repaired Menhir wrapper — `PASS` — the live canary returned `user` and `new app`, one grounded edge, zero orphan drops, and no repair call.
- `.\.venv\Scripts\python.exe -m pytest tests/test_graphiti_combined_extraction_closure.py tests/test_graphiti_combined_extraction_patch.py tests/test_enrichment_failures.py tests/test_ingest_gate.py tests/test_ingest_guard.py tests/test_project_ingest_service.py -q` — `PASS` — 96 passed, 1 skipped.
- `ruff check --ignore F401,F841 src/menhir/infrastructure/graphiti_extraction_patches.py src/menhir/services/enrichment_steps.py tests/test_graphiti_combined_extraction_closure.py` — `PASS` — all checks passed.
- `.\.venv\Scripts\python.exe -m compileall -q src\menhir` — `PASS` — no compile errors.
- `.\.venv\Scripts\python.exe -m pytest -q` at the final `012b0c5` worktree — `PASS` — 4,378 passed, 170 skipped, 4 warnings in 253.66 seconds.
- `git diff --check` before each commit — `PASS` — no whitespace errors.
- `python C:\Users\you\IdeaProjects\projects\ctharvey\cth.agentsmith\scripts\wrapup_validator.py C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-07-28-menhir-scalar-candidate-relationless-handoff.md --json` — `PASS` — 14 checks, 0 failures.
- Live resume on `811dd41` — `FAIL` — assistant boilerplate episode `66c1186d-f864-4861-ae94-0390fc00aeab` returned only `user` with zero edges twice and remained `FAILED`; this directly motivated and is covered by `012b0c5`.
- Full 78-item continuation and recall scoring on `012b0c5` — `NOT RUN` — deliberately stopped at the four-row manifest boundary so this handoff could preserve the exact continuation state.

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

The code remediation is committed and fully offline-verified. The paid candidate build and recall
score remain incomplete by design and are the subject of the continuation steps below.

## Assumptions

1. The four manifest rows are the authoritative completion boundary. The ingest script skips those
   question IDs and resets every namespace in the first unmanifested window before resubmission.
2. The active Neo4j volume is benchmark-only and may be mutated by the benchmark reset endpoints;
   it is not production Neo4j.
3. Two-namespace concurrency remains the intended cost/throughput balance.
4. The mixed-code candidate is acceptable only because the phase boundary is explicit: four rows
   were built on `c093783`, no rows were manifested on `811dd41`, and all remaining rows will be
   built on `012b0c5`.

## Risks / Gaps

1. The benchmark wrapper rewrites active run and graph provenance at startup. Preserve the annotated
   files before starting and restore them immediately after the wrapper emits both
   `provenance recorded` messages.
2. The first paid window on `012b0c5` has not yet proven that both the assistant policy-empty turn
   and the exact grocery-list app turn succeed together in the real pipeline.
3. The full build is expected to take roughly 8–10 hours after restart, followed by recall and QA
   scoring.
4. `artifact_validate` was not exposed as an MCP tool in this Codex session. Use the standalone
   validator command in Follow-Up Task 7; keep this wrapup `PARTIAL` while the benchmark is
   unfinished regardless of validator outcome.
5. Do not push either Menhir commit unless the user separately requests a push.

## Follow-Up Tasks

1. Confirm the starting state:

   ```powershell
   $menhir = 'C:\Users\you\IdeaProjects\projects\archolith\menhir'
   $bench = 'C:\Users\you\IdeaProjects\projects\archolith\archolith-bench'
   $results = Join-Path $bench 'results\lme-ku-buildout\scalar-current-candidate-v3-20260728'
   git -C $menhir status --short
   git -C $menhir rev-parse HEAD
   (Get-Content -Raw (Join-Path $results 'manifest.json') | ConvertFrom-Json).Count
   docker ps -a --filter 'name=menhir-lme-scalar-current-candidate-v3-20260728' --format '{{.Names}}|{{.Status}}'
   ```

   Expected: clean Menhir tree, head `012b0c53cbef83050bb9472dd010c05ae8ca448c`,
   manifest count `4`, and stopped candidate container.

2. Preserve the annotated provenance immediately before the final resume:

   ```powershell
   Copy-Item (Join-Path $results 'run_provenance.json') (Join-Path $results 'run_provenance.pre-final-resume-012b0c5.json')
   Copy-Item (Join-Path $results 'graph-provenance-menhir-lme-scalar-current-candidate-v3-20260728.json') (Join-Path $results 'graph-provenance-menhir-lme-scalar-current-candidate-v3-20260728.pre-final-resume-012b0c5.json')
   ```

3. Start the manifest-backed continuation in a hidden background process:

   ```powershell
   $env:LME_KU_RUN_ID = 'scalar-current-candidate-v3-20260728'
   $env:LME_KU_ARM = 'candidate'
   $env:LME_KU_ALLOW_RESUME = '1'
   $env:LME_KU_CHECKPOINT_ITEMS = '0'
   $env:LME_KU_INGEST_CONCURRENCY = '2'
   $env:LME_KU_KEEP_NEO4J_UP = '0'
   $env:LME_KU_ALLOW_DIRTY = '0'
   $stdout = Join-Path $results 'resume-012b0c5.stdout.log'
   $stderr = Join-Path $results 'resume-012b0c5.stderr.log'
   Start-Process -FilePath 'C:\Program Files\Git\bin\bash.exe' `
     -ArgumentList @('/c/Users/you/IdeaProjects/projects/archolith/archolith-bench/scripts/longmemeval/run_knowledge_update_buildout.sh') `
     -WorkingDirectory $bench `
     -RedirectStandardOutput $stdout `
     -RedirectStandardError $stderr `
     -WindowStyle Hidden
   ```

4. After the new stderr log contains both `provenance recorded` and `graph provenance recorded`,
   restore the annotated files so the wrapper's completion update preserves the mixed-code record:

   ```powershell
   Copy-Item (Join-Path $results 'run_provenance.pre-final-resume-012b0c5.json') (Join-Path $results 'run_provenance.json') -Force
   Copy-Item (Join-Path $results 'graph-provenance-menhir-lme-scalar-current-candidate-v3-20260728.pre-final-resume-012b0c5.json') (Join-Path $results 'graph-provenance-menhir-lme-scalar-current-candidate-v3-20260728.json') -Force
   ```

   Add the actual final resume timestamp to the restored files when convenient; do not delete any
   phase archive.

5. Wait for the manifest to reach six rows, then inspect the two new rows. Both
   `945e3d21` and `d7c942c3` must report `failed_remaining=0`,
   `scalar_consolidated=true`, and at least three scalar LLM calls:

   ```powershell
   $rows = Get-Content -Raw (Join-Path $results 'manifest.json') | ConvertFrom-Json
   $rows | Select-Object -Last 2 question_id,namespace,ready,failed_remaining,scalar_consolidated,scalar_llm_calls,turn_evidence,typed_assertions,scalar_views
   ```

6. Independently verify the repaired window before trusting the remaining 72 items:

   - In `resume-012b0c5.stderr.log`, the generic task-prioritization assistant turn should log
     `Policy-empty enrichment (success)` with `self_only_relationless=True` and should not log
     `relationless_extraction`.
   - Query namespace `lme-d7c942c3` for the episode containing
     `I'm actually using a new app I recently downloaded.`; it must be `READY` with no processing
     error.
   - Query the same namespace for an entity relationship connecting `user` and `new app`; retain the
     returned relationship type and fact in the run notes.
   - Confirm both new namespaces have zero `FAILED`, zero queued/processing episodes, and no
     cross-namespace entity relationships.
   - If any check fails, stop the background process tree and container before changing code. The
     window is still unmanifested until both rows are written, so the next resume can safely reset it.

7. Validate this wrapup mechanically:

   ```powershell
   python C:\Users\you\IdeaProjects\projects\ctharvey\cth.agentsmith\scripts\wrapup_validator.py `
     C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-07-28-menhir-scalar-candidate-relationless-handoff.md `
     --json
   ```

8. Let the candidate build and recall scoring finish. Confirm the final manifest has 78 unique rows,
   the wrapper records `completed_at` and `harness_exit`, and the candidate container is stopped.
   Then update this wrapup's Verification, Risks / Gaps, Completion Checklist, and Status.

## Notes

- Active run: `scalar-current-candidate-v3-20260728`
- Container: `menhir-lme-scalar-current-candidate-v3-20260728`
- Volume: `menhir-lme-data-scalar-current-candidate-v3-20260728`
- Ports: Bolt `7694`, Neo4j HTTP `7481`, ingest Menhir `8124`, recall Menhir `8125`
- Fixture: `C:\Users\you\IdeaProjects\projects\archolith\archolith-bench\fixtures\longmemeval\knowledge_update_subset.json`
- Fixture SHA-256: `bba252a302e7b257a0f7457fe97411f7de144aafd1c6b44c98a0e88ee8570907`
- Candidate settings: scalar threshold `2/3`; attribute/scope/subject reconciliation on; scalar
  View authority on; consolidation and recall audits on; TurnEvidence required; concurrency `2`.
- Completed question IDs: `6a1eabeb`, `6aeb4375`, `830ce83f`, `852ce960`
- Next window: `945e3d21`, `d7c942c3`
- Do not start a baseline arm, touch production Neo4j, delete the candidate volume, replay completed
  namespaces, push commits, or alter unrelated untracked archolith-bench files.
