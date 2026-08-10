# WRAPUP — Menhir suburbs extraction fix

**Date:** 2026-07-16
**Agent:** Codex
**Model:** GPT-5
**Status:** PARTIAL
**Plan / Ticket:** `C:\Users\you\IdeaProjects\.agent\plans\menhir-resolve-suburbs-extraction-failure-implementation-plan.md`
**Worktree:** `C:\Users\you\IdeaProjects\projects\archolith\menhir`
**Branch:** Menhir `main`; workspace `master`
**Commits:** `c949dfa5e87ba70e1d3a498f81b89b6af77c3980`; `2c10c045805295bbc252cccfbdcd1595acf43504`
**Verification Scope:** Menhir implementation commit `c949dfa5e87ba70e1d3a498f81b89b6af77c3980`, workspace plan commit `2c10c045805295bbc252cccfbdcd1595acf43504`, and the commands/results recorded below
**Docs Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\architecture.md`; `C:\Users\you\IdeaProjects\.agent\plans\menhir-resolve-suburbs-extraction-failure-implementation-plan.md`
**Changelog Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\CHANGELOG.md`

---

## Before Writing

The plan was checked backwards from the required end state. The isolated production replay proves
that the target message creates a Rachel-to-suburb relation, Graphiti invalidates and expires the
prior Chicago assertion, current recall includes the suburb fact, and cleanup leaves zero namespace
nodes. That behavior comes from the production client installing the combined-extraction bridge,
which was selected only after its repeated gate passed and the narrower prompt-only candidate
failed its stop gate. The starting canonical `lme-830ce83f` namespace remained read-only throughout;
all mutation evidence came from unique smoke namespaces.

The only incomplete closeout requirement is mechanical wrapup validation: the required
`artifact_validate` tool is not available in this harness. For that reason this document remains
`PARTIAL` even though the implementation, tests, docs, changelog, evidence, and commits are complete.

---

## Summary

Menhir now uses Graphiti 0.29.2's typed combined node-and-edge extractor for standard
single-episode ingestion. A task-local, one-use `ContextVar` carries combined edges across
Graphiti's separate node-resolution and edge-resolution phases; custom edge schemas and cache
misses fall back to the upstream edge extractor.

The real-model gate improved both affected value fixtures from 0/10 baseline captures to 10/10,
without regressing the three precision controls. A 14-episode isolated production replay created
`Rachel -> the suburbs`, invalidated and expired Chicago, returned the current suburb in recall,
and verified zero nodes after cleanup. No schema, endpoint, or recall-ranking change was needed.

## Files Changed

| File | Why |
|------|-----|
| `C:\Users\you\IdeaProjects\.agent\plans\menhir-resolve-suburbs-extraction-failure-implementation-plan.md` | Records the gated implementation plan, failed prompt candidate, selected combined design, and actual results. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\CHANGELOG.md` | Records the fix and validation evidence. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\architecture.md` | Documents the production combined-extraction compatibility layer and fallback. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\explorer\extraction_lab.py` | Adds the combined-extraction experimental arm. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\explorer\test_extraction_lab.py` | Verifies the combined arm does not mutate the separate node prompt. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\infrastructure\graphiti_client.py` | Installs the compatibility patch during production client construction. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\src\menhir\infrastructure\graphiti_patches.py` | Implements typed combined extraction, task-local edge handoff, and fallback behavior. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\tests\test_graphiti_combined_extraction_patch.py` | Covers idempotency, one-use caching, concurrent task isolation, and custom-schema fallback. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\scripts\run_suburbs_extraction_gate.py` | Runs the interleaved repeated real-model recall/precision gate. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\scripts\smoke\suburbs_extraction_live_smoke.py` | Runs and cleans an isolated production-path sequential replay. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\results\suburbs_extraction_gate.json` | Retains all 100 arm/trial extraction records and the passing summary. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\results\suburbs_extraction_live_smoke.json` | Retains ingest, entity, edge, invalidation, recall, and zero-node cleanup evidence. |
| `C:\Users\you\IdeaProjects\projects\archolith\menhir\.agent\for-review\WRAPUP-2026-07-16-menhir-suburbs-extraction-fix.md` | Provides this commit-anchored implementation handoff and validator gap disclosure. |

## Verification

- `.\.venv\Scripts\python.exe scripts\run_suburbs_extraction_gate.py --trials 10` — `PASS` — suburbs 0/10 baseline versus 10/10 combined; downtown 0/10 versus 10/10; all three controls 10/10 safe/correct.
- `.\.venv\Scripts\python.exe scripts\smoke\suburbs_extraction_live_smoke.py` — `PASS` — 14/14 ingested; one suburb entity and Rachel-to-suburb edge; Chicago invalidated and expired; recall error `null`; cleanup remaining nodes `0`.
- `.\.venv\Scripts\python.exe -m pytest -q tests\test_graphiti_combined_extraction_patch.py tests\test_graphiti_client.py src\menhir\explorer\test_extraction_lab.py -p no:cacheprovider` — `PASS` — 114 passed, 2 dependency/configuration warnings.
- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` — `PASS` — 3,351 passed, 100 skipped, 4 dependency/configuration warnings in 278.52 seconds.
- `.\.venv\Scripts\python.exe -m py_compile scripts\run_suburbs_extraction_gate.py scripts\smoke\suburbs_extraction_live_smoke.py src\menhir\infrastructure\graphiti_patches.py` — `PASS` — no output.
- `git diff --check` — `PASS` — no whitespace errors; only line-ending conversion warnings were printed before staging.
- Secret-pattern scan across changed code, docs, scripts, and evidence — `PASS` — no credential or API-key patterns found.
- `artifact_validate(artifact_type="wrapups", filename="WRAPUP-2026-07-16-menhir-suburbs-extraction-fix.md")` — `NOT RUN` — the validator tool is unavailable in this harness.

## Claim Cross-Check

- Summary checked against actual code/diff: `yes`
- Files Changed checked against actual modified files: `yes`
- Commit list checked against actual commit hashes or working-tree state: `yes`
- Verification results copied from actual command output: `yes`
- Mechanical artifact validation remains unavailable; this is why status is `PARTIAL`.

## Completion Checklist

- Plan / acceptance criteria completed: `yes`
- Docs updated as required: `yes`
- Changelog updated as required: `yes`
- Work committed: `yes`

## Assumptions

1. Graphiti remains within the guarded 0.29.x dependency range; an upgrade requires revalidating the
   compatibility patch and rerunning both evidence scripts.
2. Standard ingestion does not supply custom edge schemas. Custom-schema calls intentionally use
   upstream edge extraction and were unit-tested, but were not part of the real-model gate.

## Risks / Gaps

1. `artifact_validate` was unavailable, so the required mechanical wrapup validation is incomplete
   and the status cannot honestly be `READY FOR REVIEW`.
2. The production behavior relies on Graphiti internal call sites. The existing version guard and
   tests reduce upgrade risk but do not eliminate it.
3. The canonical failing namespace was deliberately not mutated. Acceptance is based on the
   equivalent isolated replay plus read-only canonical baseline evidence.
4. The first full-suite invocation hit the shell runner's 120-second timeout and closed pytest's
   output pipe; the quiet rerun with a 600-second ceiling completed successfully.

## Follow-Up Tasks

1. Run the mechanical wrapup validator when `artifact_validate` becomes available, fix any findings,
   and promote this document to `READY FOR REVIEW`.
2. Re-run the extraction gate and isolated replay when upgrading Graphiti beyond 0.29.x or changing
   the production extraction model.

## Notes

- The failed prompt-only candidate was removed and was never installed in production.
- The live replay reads the active credential from environment at runtime; no credential is stored
  in the runner or result artifact.
- Neo4j emitted missing-property warnings for unrelated structural recall queries during the replay;
  recall itself returned without a search error.
