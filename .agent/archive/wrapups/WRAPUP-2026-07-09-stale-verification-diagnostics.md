# WRAPUP — menhir / stale verification diagnostics v1

**Date:** 2026-07-09
**Agent:** opencode
**Model:** deepseek/deepseek-v4-flash
**Status:** READY FOR REVIEW
**Plan / Ticket:** specs delivered inline in user prompt (no plan file)
**Worktree:** N/A
**Branch:** `diagnostics/stale-verification-diagnostics-v1`
**Commits:** `5addcf6`
**Verification Scope:** commit `5addcf6` on branch `diagnostics/stale-verification-diagnostics-v1`
**Docs Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\docs\runbooks\stale-verification-diagnostics.md`
**Changelog Updated:** `C:\Users\you\IdeaProjects\projects\archolith\menhir\CHANGELOG.md`

---

## Summary

Added a read-only stale verification diagnostics report — a CLI script that fetches stale file-anchored memories and their verification receipts, classifies each receipt against its anchor using deterministic rules (memory_uuid, project, path, timestamp), and produces a diagnostics report showing valid receipts, ignored receipts with reasons, and latest valid outcome per anchor.

Report-only: never reads file contents, never clears dirty flags, never writes receipts, never modifies memories.

## Files Changed

| File | Why |
|------|-----|
| `scripts/maintenance/report_stale_verifications.py` | New — CLI script for stale verification diagnostics |
| `tests/test_report_stale_verifications.py` | New — 47 tests for classification, report building, CLI, safety |
| `docs/runbooks/stale-verification-diagnostics.md` | New — runbook with CLI usage, JSON mode, safety notes |
| `CHANGELOG.md` | Updated — entry for stale verification diagnostics v1 |

## Verification

- `python -m pytest tests/test_report_stale_verifications.py -q` — `PASS` — 47 passed
- `python -m pytest tests -q -k "stale or verification or tool_event or diagnostics or report"` — `PASS` — 239 passed
- Safety: file content / transcript / dirty-clearing / write-receipt patterns checked in source — `PASS` — none found
- CLI --json output is valid parseable JSON — `PASS` — confirmed in test_json_mode_output_is_parseable
- Malformed timestamps handled conservatively (never crash, never treated as valid) — `PASS` — confirmed in test_malformed_timestamp_is_ignored, test_malformed_timestamp_in_ignored_list

## Claim Cross-Check

- Summary checked against actual code/diff: **yes**
- Files Changed checked against actual modified files: **yes** — `git diff HEAD~1 HEAD --stat` shows 4 files, 1206 insertions
- Commit list checked against actual commit hashes or working-tree state: **yes** — `5addcf6`
- Verification results copied from actual command output: **yes** — 47 passed; targeted suite 239 passed

## Completion Checklist

- Plan / acceptance criteria completed: **yes** — all specified deliverables and safety rules met
- Docs updated as required: **yes** — runbook created
- Changelog updated as required: **yes** — entry added
- Work committed: **yes** — commit `5addcf6`

## Assumptions

1. The existing API endpoints `GET /api/tool-events/stale` and `GET /api/tool-events/stale-verifications` return the expected response shapes (confirmed by reading routes.py and tool_event_repository.py).
2. The `project` field is set on verification receipts and stale anchors consistently for matching.
3. Timestamps are ISO-8601 with timezone (Z or offset). Naive timestamps are treated as unparseable.

## Risks / Gaps

1. N+1 HTTP requests: per-memory verification fetches mean N+1 requests for N unique memory UUIDs. Acceptable for a diagnostics tool.
2. No API endpoint added — the spec left this optional and said to skip if it risked broad changes. The CLI composes existing readonly endpoints.
3. The `RECEIPT_VALID` test data had a missing `basis` field which caused `test_json_mode_output_is_parseable` to classify the receipt as `timestamp_error` (the test's `RECEIPT_VALID` dict had `"verified_at": ""` erroneously — not `"2026-07-09T02:00:28Z"`). Fixed by correcting the test fixture. All 47 tests now pass.

## Follow-Up Tasks

1. None — this is the complete v1 deliverable.

## Notes

- The `main()` function returns an int (0/1) instead of always raising `SystemExit`, which differs from the `record_stale_anchor_verification.py` pattern. The `if __name__ == "__main__"` guard wraps it with `raise SystemExit(main())` for CLI use. Tests call `main()` directly and check the return value.
- All 10 minimum required tests are implemented: valid classification, wrong-path ignored, pre-dirty ignored, malformed timestamp ignored, no-receipt status, latest-valid-wins, JSON parseability, human output, network failure exit, no-file-content safety.
