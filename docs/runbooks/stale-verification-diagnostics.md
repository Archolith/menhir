# Stale Verification Diagnostics — runbook

`scripts/maintenance/report_stale_verifications.py` produces a read-only diagnostics
report showing the state of verification coverage for stale file-anchored memories.

```
stale anchors -> verification receipts -> classify by path/project/timestamp
-> report: which anchors have valid receipts, which receipts are ignored (and why),
  latest valid outcome per anchor
```

## What the report does

For each stale file-anchored memory (a memory anchored to a file that changed after
anchoring), the report identifies:

- Valid same-path post-dirty verification receipts
- Ignored receipts and the reason for ignoring them
- The latest valid outcome per anchor
- Stale anchors with no valid receipt

The report classifies each receipt independently against its stale anchor using
deterministic rules (memory_uuid, project, path, timestamp comparison). A receipt is
valid only if it matches on all four dimensions and its `verified_at` is at or after
the anchor's `dirty_at`.

## What it does NOT do

- No auto-refresh
- No dirty clearing
- No memory supersession
- No review-task creation
- No deletion or expiration
- No ranking or filtering changes
- No lifecycle mutation
- No Phase 3 changes
- No TurnEvidence changes
- No file content capture
- No transcript capture

The core invariant: **a wrong current-state view is worse than a miss.** This report
is diagnostics-only — it never marks a stale memory fresh.

## CLI usage

```bash
# Human-readable output
python scripts/maintenance/report_stale_verifications.py \
  --project smoke-hook-center-stale-lane

# With explicit server URL
python scripts/maintenance/report_stale_verifications.py \
  --url http://127.0.0.1:8099 \
  --project smoke-hook-center-stale-lane

# JSON mode (parseable JSON only on stdout)
python scripts/maintenance/report_stale_verifications.py \
  --project smoke-hook-center-stale-lane --json
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `MENHIR_URL` | Base URL for the menhir server |
| `MENHIR_TOOL_EVENTS_URL` | Exact URL prefix for tool-events endpoints |
| `MENHIR_READONLY_KEY` | Bearer token for readonly endpoints |
| `MENHIR_AGENT_KEY` | Fallback Bearer token (used if READONLY_KEY is not set) |

`MENHIR_READONLY_KEY` is tried first for readonly endpoint auth; `MENHIR_AGENT_KEY`
is used as fallback.

## Data sources

The script composes two existing readonly API endpoints:

- `GET /api/tool-events/stale?project=<project>` — returns stale anchored memories
- `GET /api/tool-events/stale-verifications?memory_uuid=<uuid>` — returns verification
  receipts for a memory, which are then classified against each stale anchor

No new API endpoint is required. No Cypher is issued from the script.

## Example output

### Human mode

```
Stale verification diagnostics
Project: smoke-hook-center-stale-lane

Counts:
  stale anchors:              3
  with valid receipt:         1
  without valid receipt:      2
  ignored receipts:           4
  valid/still_valid:          1
  valid/outdated:             0

Items:
  src/example.py :: abc
    status: valid_still_valid
    dirty_at: 2026-07-09T01:55:28Z
    latest valid receipt: still_valid @ 2026-07-09T02:00:28Z by smoke-agent
    ignored receipts: 2

  src/missing.py :: def
    status: no_receipt
    dirty_at: 2026-07-09T01:55:28Z
```

### JSON mode

```json
{
  "project": "smoke-hook-center-stale-lane",
  "counts": {
    "stale_anchors": 3,
    "with_valid_receipt": 1,
    "without_valid_receipt": 2,
    "ignored_receipts": 4,
    "valid_still_valid": 1,
    "valid_outdated": 0
  },
  "items": [
    {
      "memory_uuid": "abc",
      "path": "src/example.py",
      "dirty_at": "2026-07-09T01:55:28Z",
      "anchored_at": "2026-07-01T00:00:00Z",
      "latest_valid_receipt": {
        "outcome": "still_valid",
        "verified_at": "2026-07-09T02:00:28Z",
        "verified_by": "smoke-agent",
        "basis": "inspected_current_file"
      },
      "status": "valid_still_valid",
      "ignored_receipts": [
        {
          "outcome": "still_valid",
          "path": "src/other.py",
          "verified_at": "2026-07-09T02:00:28Z",
          "reason": "wrong_path"
        }
      ]
    }
  ]
}
```

## Status values

| Status | Meaning |
|--------|---------|
| `no_receipt` | No verification receipts exist for this anchor |
| `only_ignored_receipts` | Receipts exist, but all are ignored (wrong path, pre-dirty, etc.) |
| `valid_still_valid` | Latest valid receipt outcome is `still_valid` |
| `valid_outdated` | Latest valid receipt outcome is `outdated` |
| `valid_unclear` | Latest valid receipt outcome is `needs_review` or `superseded` |

## Receipt validity rules

A receipt is valid for a stale anchor when all of these hold:

```
receipt.memory_uuid == stale_anchor.memory_uuid
receipt.project == stale_anchor.project
receipt.path == stale_anchor.path
receipt.verified_at is a valid ISO-8601 UTC timestamp
stale_anchor.dirty_at is a valid ISO-8601 UTC timestamp
receipt.verified_at >= stale_anchor.dirty_at
```

A receipt is ignored (with reason) when any fails:

| Reason | Condition |
|--------|-----------|
| `wrong_memory_uuid` | receipt.memory_uuid != anchor.memory_uuid |
| `wrong_project` | receipt.project != anchor.project |
| `wrong_path` | receipt.path != anchor.path |
| `pre_dirty` | receipt.verified_at < anchor.dirty_at |
| `timestamp_error` | verified_at missing, empty, or unparseable |
| `anchor_timestamp_error` | anchor dirty_at missing, empty, or unparseable |

## Safety notes

- The script never reads or sends file contents
- The script never captures or sends transcripts
- The script never clears dirty flags
- The script never writes verification receipts
- The script never modifies memories
- Malformed timestamps are handled conservatively — they never crash the report,
  and they never count as valid

## Known limitations

- This report only diagnoses verification coverage. It does not decide or apply
  lifecycle actions.
- Per-memory verification fetches mean N+1 HTTP requests for N unique memory UUIDs
  in the stale set.
- Timestamp comparisons use ISO-8601 UTC parsing. Timestamps without timezone
  info are treated as unparseable (conservative).
- The report does not surface verification receipts for non-stale anchors.

## Unit tests

```bash
python -m pytest tests/test_report_stale_verifications.py -q
```

Also run a targeted test suite:

```bash
python -m pytest tests -q -k "stale or verification or tool_event or diagnostics or report"
```
