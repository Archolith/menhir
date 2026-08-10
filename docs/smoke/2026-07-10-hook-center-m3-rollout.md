# M3 Hook Center Rollout Receipt

**Date:** 2026-07-10 (runs captured on throwaway server clocks, UTC)
**Repo:** `menhir` @ `main` (HEAD `1dd11ed`; working tree adds this receipt + runbook fix)
**Milestone:** MVP roadmap **M3 - Hook Center rollout** (`docs/roadmap/menhir-mvp-roadmap.md`)

This receipt closes M3's execution checklist: the file-event host hook is installed for the
active host (Claude Code), and both Hook Center smokes pass green against real backends.

## M3 gate vs. evidence

| M3 gate (roadmap) | Evidence |
|---|---|
| File edits mark matching structure file nodes dirty | stale-lane `[1] tool_event_accepted: marked_dirty=True`, `[2] dirty_file_visible=True` |
| Stale anchored memories appear with `stale_anchor=true` | stale-lane `[3] stale_anchor_visible=True`, `[4] recall_stale_label: stale=True` |
| Context output includes an actionable stale warning | stale-lane `[5] formatter_stale_advisory`, `[6] context_warning_atomic=True` |
| Post-dirty verification receipt changes advisory without clearing dirty state | stale-lane `[7]/[8]/[12]` PASS, `no_mutation=True` |
| Install the file-event hook for the active local agent host | Claude Code `PostToolUse` hook registered (below); producer dry-run verified |
| v1 advisory-only (no auto down-rank/delete/re-anchor/dirty-clear) | all `safety.*` flags true; `outdated` receipt mutates no lifecycle |

## 1. Live component smoke

Self-serves a throwaway Neo4j-backed Menhir; exercises the endpoints, dirty/stale
diagnostics, report CLI, and policy guard (warn + block).

```bash
python scripts/smoke/hook_center_live_smoke.py
```

Result: **PASS_WITH_UNSCANNED_FILE** — server reachable; `POST /api/tool-events accepted=True`
(`marked_dirty=False` expected: the synthetic path is not in the structure graph); dirty/stale
endpoints OK; report script PASS; policy guard warn PASS; policy guard block PASS.

## 2. Stale-anchor lane smoke (full MVP gate)

Proves the whole lane end-to-end against a **real** Neo4j: file event -> dirty -> stale detect
-> recall label -> context warning -> verification receipt (path-aware, post-dirty only).

### Correct invocation (see note)

```bash
# Pure self-serve: the launcher owns the server AND its Neo4j. Zero --neo4j flags.
python scripts/smoke/hook_center_stale_lane_smoke.py

# Fast path used for this run: reuse an already-running throwaway Neo4j (no second container).
# Still zero --neo4j flags -- the env var feeds the launcher so both halves share one DB.
export MENHIR_TEST_NEO4J_URI=bolt://localhost:7688
export MENHIR_TEST_NEO4J_USER=neo4j
export MENHIR_TEST_NEO4J_PASSWORD=smokepass
python scripts/smoke/hook_center_stale_lane_smoke.py
```

> **Harness note (why this matters):** the smoke's `_cli()` self-serves unless `--no-self-serve`
> / `--url` / `MENHIR_URL` is present. In self-serve, passing `--neo4j-uri` redirects only the
> *in-process* fixture/recall backend while the HTTP server keeps its launcher-owned Neo4j ->
> split-brain, `marked_dirty=False`, full cascade FAIL. Only the fully-external mode
> (`--no-self-serve --url ...`) takes `--neo4j-*` flags. The runbook example that passed
> `--neo4j-uri` in self-serve mode was stale and has been corrected in
> `docs/runbooks/hook-center-stale-lane-smoke.md`.

### Human output

```
server ready: http://127.0.0.1:56242
fixture created: project=smoke-hook-center-stale-lane path=src/smoke_target.py memory_uuid=smoke-memory-001
[1] tool_event_accepted: accepted=True marked_dirty=True
[2] dirty_file_visible: True dirty_at=2026-07-10T23:11:24.354Z op=edit hash=smoke-synthetic-hash
[3] stale_anchor_visible: True path=src/smoke_target.py
[4] recall_stale_label: stale=True control_stale=False
[5] formatter_stale_advisory: action=verify_current_file_before_relying
[6] context_warning_atomic: both_present=True neither_present=True
[9/10] wrong_path + pre_dirty ignored: no_enrich=True verification=None
[7] verification_receipt_recorded: recorded=True listed=True
[8] post_dirty_receipt_enriches: stale=True outcome=still_valid
[11] malformed_timestamp_conservative: status=400
[12] outdated: action=do_not_rely_update_or_supersede no_mutation=True
smoke data cleaned

Result: PASS
```

**PASS** — all 12 lane checks green. Exit code 0.

## 3. File-event host hook install (Claude Code)

Registered on `PostToolUse` for the file tools in the workspace
`.claude/settings.local.json` (alongside the existing `UserPromptSubmit` TurnEvidence hook):

```json
{ "hooks": { "PostToolUse": [
  { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
    "hooks": [ { "type": "command",
      "command": "/path/to/menhir/.venv/bin/python",
      "args": [ "/path/to/menhir/scripts/hooks/menhir_file_event.py" ],
      "timeout": 8 } ] } ] } }
```

Producer verified (dry-run, no server needed):

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"src/foo.py"},"transcript_path":"/x/y.jsonl"}' \
  | .venv/Scripts/python.exe scripts/hooks/menhir_file_event.py --dry-run
# {"would_send": true, "operation": "edit", "path": "src/foo.py", "content_uploaded": false}
```

The producer's real POST target is `POST /api/tool-events`, which the live smoke above
independently confirmed accepts events (`accepted=True`). Fail-open: menhir down / unreadable
file / malformed input -> exit 0, never blocks the tool.

## Safety confirmation

- **No file content uploaded** — only a local sha256 provenance hash + path.
- **No transcript captured**; no assistant/tool turns.
- **Throwaway server + project** — disposable Neo4j, disposable project namespace
  `smoke-hook-center-stale-lane`; cleanup deletes strictly by `smoke_project` marker.
- **v1 advisory-only** — no auto structure rebuild, no dirty clearing, no down-rank, no
  re-anchor, no deletion/expiration. The `outdated` receipt mutates no lifecycle.

## Follow-ups (not M3 blockers)

- OpenCode has no clean file-event hook surface (documented v0 limit); only Claude Code / Codex
  are supported. Codex hook is not registered in this session (Claude Code is the active host).
