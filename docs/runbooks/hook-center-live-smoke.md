# Hook Center Live Smoke v1

End-to-end smoke harness that proves Hook Center components work together against a
throwaway Menhir instance.

## What it proves

| # | Check | Component |
|---|-------|-----------|
| 1 | Server is reachable via `GET /api/tool-events/dirty` | Server readiness |
| 2 | `POST /api/tool-events` accepts a `file_changed` event and returns `accepted=true` | Event endpoint |
| 3 | `GET /api/tool-events/dirty` returns diagnostics with the expected fields | Dirty diagnostic |
| 4 | `GET /api/tool-events/stale` returns stale anchor count | Stale endpoint |
| 5 | `report_dirty_files.py` CLI fetches and prints dirty/stale data | Report script |
| 6 | `menhir_policy_guard.py` warns on a frozen path and blocks on a frozen path | Policy guard |

## What it does NOT prove

- Automatic structure rebuild (v0 does not do this).
- Recall down-ranking of stale anchors (not wired in v0).
- File content safety (the smoke sends only a synthetic hash, never real content).
- Symbol-level invalidation (v0 does not attempt this).
- OpenCode file-event support (not available in v0).

## Prerequisites

- Python 3.11+
- A throwaway Menhir server running on `http://127.0.0.1:8099` (or custom URL)

### Starting a throwaway Menhir instance

From the menhir repo root:

```bash
# Using the dev launch config (adjust to your local setup)
menhir --port 8099 --benchmark-mode
```

Or via your local Docker/test compose setup targeting port 8099.

The smoke script does **not** start Menhir automatically — it expects a running server.

## Running the smoke

```bash
# Default server at http://127.0.0.1:8099
python scripts/smoke/hook_center_live_smoke.py

# Custom URL
python scripts/smoke/hook_center_live_smoke.py --url http://localhost:9000

# Custom project and path
python scripts/smoke/hook_center_live_smoke.py --project my-smoke --path src/test.py

# Skip policy guard tests (useful in CI without a temp filesystem)
python scripts/smoke/hook_center_live_smoke.py --skip-policy

# Require stale anchors to exist (fails if count is 0)
python scripts/smoke/hook_center_live_smoke.py --require-stale

# JSON output
python scripts/smoke/hook_center_live_smoke.py --json
```

## Expected output

### PASS

```
Hook Center Live Smoke
Server: http://127.0.0.1:8099
Server reachable: yes
POST /api/tool-events: accepted=true marked_dirty=true
Dirty endpoint: dirty_files=1 stale_anchors=0
Stale endpoint: count=0
Report script: PASS
Policy guard warn: PASS
Policy guard block: PASS

Result: PASS
```

### PASS_WITH_UNSCANNED_FILE

```
POST /api/tool-events: accepted=true marked_dirty=false
  (accepted but file not in structure graph — expected in v0)

Result: PASS_WITH_UNSCANNED_FILE
```

This is **not a failure**. It means the event was accepted but no structure-file node
matched — the file hasn't been scanned into the structure graph yet. Hook Center v0
documents this as expected behavior.

### FAIL

```
Result: FAIL
```

Only for: server unreachable, POST rejected, dirty/stale endpoint unavailable, report
script failure, or policy guard behavior mismatch.

## Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| `server unreachable` | Menhir not running on the expected URL |
| `POST rejected` | Wrong port, no agent-tier auth, or invalid payload |
| `accepted=true marked_dirty=false` | File not yet in structure graph (normal for first run) |
| `report script failed` | Script path wrong or server URL mismatch |
| `policy guard` fails | Temp file or subprocess issue; try `--skip-policy` |

## Safety and privacy

- No file content is uploaded.
- No transcripts are captured.
- The smoke event sends only a synthetic path, operation, and hash — no real file data.
- The policy guard runs locally and never sends data to Menhir.
- Use a throwaway server, namespace, and project to avoid interfering with production data.

## Result states

| State | Meaning |
|-------|---------|
| `PASS` | All checks passed, event marked a structure file dirty |
| `PASS_WITH_UNSCANNED_FILE` | All checks passed, event accepted but no structure node matched |
| `FAIL` | One or more checks failed |
