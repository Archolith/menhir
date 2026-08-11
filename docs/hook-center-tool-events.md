# Hook Center / Tool Event Capture (v0)

How menhir avoids **stale file references** by observing file/edit events through hooks — instead of
trusting the LLM to remember to call a memory-update tool.

## Purpose

Hooks observe file/tool events. Hooks do not wrap editing tools. Hooks do not rely on the LLM to call
menhir. Hooks do not upload file contents by default. Hooks fail open. Menhir uses events to mark
file-linked context dirty/stale. A later sweep/refresh can consume dirty flags.

## Lifecycle

```
1. Hook observes a file event (edit/write/delete/rename).
2. Hook normalizes the event and POSTs to menhir.
3. Menhir marks matching file Entity node(s) dirty.
4. Stale anchored memories become detectable when dirty_at > ANCHORED_TO.created_at.
5. Recall/diagnostics can label stale anchors (v1) or avoid stale anchors (future).
6. A later structure refresh/sweep can re-scan changed files and clear dirty flags.
```

Important v0 constraints: no automatic structure rebuild, no automatic re-anchoring of memories.
v0 only makes stale references detectable.

## What is and isn't captured

| Captured | NOT captured |
|----------|--------------|
| The changed file **path** (+ old_path on rename) | File **contents** (never uploaded) |
| The **operation** (write/edit/delete/rename/create) | Raw tool transcripts |
| Optional **hash** of the file (provenance, local sha256) | Assistant turns / full transcript mode |
| Optional mtime, git branch/commit, session id | Secrets |

Only a path + optional provenance leaves the machine. The hash is computed locally and is a digest,
not content.

## Event shape

```json
{
  "event_type": "file_changed",
  "source_client": "claude_code | codex | opencode | unknown",
  "source_kind": "hook",
  "session_id": "...",
  "project": "menhir",
  "repository": "menhir",
  "project_root": "/abs/repo",
  "cwd": "...",
  "path": "src/foo.py",
  "old_path": null,
  "operation": "write | edit | delete | rename | create",
  "before_hash": null,
  "after_hash": "sha256...",
  "mtime": "2026-07-08T12:00:00Z",
  "git_branch": "main",
  "git_commit": "abc1234",
  "metadata": {}
}
```

Only `event_type` is required at the request-model level.
For v0 `file_changed` events, `path` is required at runtime (400 if missing or blank).
Unsupported `event_type` values are accepted-and-ignored without requiring a path.
Everything else is optional so a hook with partial metadata still succeeds.
`project` scopes structural dirty marking. `repository` is the stable graph `ArtifactSource`
identity; it is never inferred from `project_root` or a worktree directory name.

## Path normalization

The hook normalizes absolute file paths under `project_root` to repo-relative paths before POSTing:

```
C:/Users/me/projects/menhir/src/foo.py  →  src/foo.py
/home/me/projects/menhir/src/foo.py     →  src/foo.py
```

The structure graph stores file nodes by `structure_path`, which is repo-relative. Sending
repo-relative paths prevents accepted-but-unmarked events. When normalization occurs, the hook may
keep the original absolute path in `metadata.original_path`. Hashing still uses the local filesystem
path; dirty marking uses the structure-relative path.

Relative paths are kept as-is. Absolute paths outside `project_root` are sent unchanged.

## Dirty marking and project fallback

On a `file_changed` event, menhir:

1. **Marks the file node dirty** — sets `structure_dirty=true`, `dirty_at`, `last_event_op`, and the
   optional `last_event_after_hash`/`mtime` on the matching `:Entity` file node(s). Rename marks both
   the old and new path. No structure rebuild happens (v0 is observation only).
2. **Falls back on project mismatch** — when `project` is supplied, menhir first tries to scope the
   match by `structure_project`. If that finds nothing, it retries path-only marking. This avoids
   false negatives when `project_root` basename does not exactly match the stored `structure_project`.
   For stale-reference prevention, conservative over-marking is safer than silently missing a changed
   file. Project scoping is a precision hint, not a hard failure boundary in v0.
3. **Returns diagnostic info** — `{accepted, matched, marked_dirty, paths}`. `matched` = how many
   existing file nodes were found and marked. When project fallback was used, `project_fallback_used`
   is set to `true`.

A file not yet in the structure graph is accepted but marks nothing (there is no node to mark). This
is a documented v0 limit. Symbol-level invalidation is not attempted — a changed file dirties the
file anchor, not individual symbols.

### Accepted-but-unmarked cases

`accepted=true, marked_dirty=false` can mean:

- unsupported `event_type` in v0
- file not yet scanned into the structure graph
- path does not match any known file node even after fallback

None of these are errors.

## Stale-anchor detection

menhir already has a structural code graph (`:Entity {structure_role:'file', structure_path,
structure_project}`) and anchors memories to it via `(:Entity)-[:ANCHORED_TO {created_at}]->(:file)`.
A memory `(sem)-[a:ANCHORED_TO]->(f)` is **stale** when `f.structure_dirty` and
`f.dirty_at > a.created_at` (the file changed *after* the memory was anchored).
`GET /api/tool-events/dirty` lists these for diagnostics.

v0 exposes stale anchors for detection and labelling; recall does not automatically down-rank or
filter them unless that integration is explicitly wired.

## Endpoint behavior

```
POST /api/tool-events       (agent tier)   -> {accepted, event_type, operation, matched, marked_dirty, ignored_reason, artifact_reconciliation}
GET  /api/tool-events/dirty  (readonly)     -> {dirty_files, stale_anchors, counts}
GET  /api/tool-events/stale  (readonly)     -> {stale_anchors, count}
```

### POST /api/tool-events

**file_changed (valid):**
```json
{"event_type": "file_changed", "path": "src/foo.py", "operation": "edit"}
```
→ `200 accepted=true, marked_dirty=true` (or `marked_dirty=false` if file not in graph)

**file_changed with missing/blank path:**
→ `400 path is required for a file_changed event`

**unsupported event_type:**
```json
{"event_type": "tool_ran"}
```
→ `200 accepted=true, marked_dirty=false, ignored_reason="unsupported event_type in v0"`

**503:** tool-event capture is unavailable (runtime not ready).

### Artifact source reconciliation (second consumer)

A file event has two independent consumers. Structural dirty marking is the first and is
unconditional. The second is `WorkArtifact` source reconciliation, and it is **strictly additive**:
it runs *after* the structural mark is recorded, and no outcome it produces can undo, block, or
change that mark.

| Operation | What reconciliation does |
|---|---|
| `rename` | Relocates the one source at `old_path` to `path`, storing `after_hash`, `git_commit`, and the destination's corpus lane. Refuses if `old_path` names more than one source or the destination is claimed. |
| `edit` / `write` | Refreshes integrity when the path already identifies exactly one source. |
| `create` | Nothing. The event carries no document metadata, and a filename is not an identity — the next corpus audit registers it from the record actually read. |
| `delete` | Nothing. Marking a source unresolved needs the whole-corpus view an audit has and an event does not. |
| anything outside the corpus routes | Nothing, and nothing is reported. |

The response field `artifact_reconciliation` is `null` unless reconciliation was attempted;
otherwise it carries `{attempted, applied, reason, ...}`. A repository error is caught, logged, and
returned as `applied: false` — the coding tool that sent the event never sees a failure for this
leg.
When `repository` is absent or blank, structural dirty marking still runs and reconciliation returns
`{attempted: true, applied: false, reason: "repository_identity_missing"}` without calling the
artifact adapter. A malformed non-string repository value is treated the same way.

**This is an accelerator, not the coverage backstop.** The hook only recognizes named file tools. A
shell `mv`, `apply_patch`, an IDE refactor, a branch switch, or an external editor all move files
without emitting an event menhir can read. Those are caught by the Git/startup recovery audit
(`MENHIR_ARTIFACT_RECONCILE_MODE`) or by `menhir artifacts audit` run by hand.

The recovery audit reads a graph-backed cursor keyed by the explicit repository identity and uses
that commit as the default Git rename interval. Audit itself never advances the cursor. A clean
`safe_apply` or operator apply advances it with compare-and-set; conflicts, skipped writes, missing
Git HEAD, or a cursor changed by another process retain it. `--from-commit` is a visible,
digest-bound evidence override, not a cursor mutation.
If Git cannot compare the selected base with HEAD, audit reports the invalid evidence base and
`safe_apply` refuses before artifact writes. This prevents a moved-and-edited file from becoming an
unresolved old source plus a newly registered duplicate.

### GET /api/tool-events/dirty

Returns `{dirty_files: [...], stale_anchors: [...], counts: {dirty_files: N, stale_anchors: M}}`.

### GET /api/tool-events/stale

Returns only stale anchored memories. Supports `project` and `limit` query parameters.

```json
{"stale_anchors": [{"memory_uuid": "...", "name": "...", "project": "menhir",
                    "path": "src/foo.py", "dirty_at": "...", "anchored_at": "...",
                    "operation": "edit"}], "count": 1}
```

## Hook behavior

`scripts/hooks/menhir_file_event.py` — a stdlib-only producer that:

- reads a Claude/Codex `PostToolUse` JSON from stdin
- normalizes supported file tools to a `file_changed` event
- hashes the changed file locally (sha256; skip on delete; never content upload)
- normalizes absolute paths under `project_root` to repo-relative
- posts to `POST /api/tool-events`
- supports `--dry-run` (prints `{would_send, operation, path, content_uploaded: false}`)
- uses `MENHIR_TOOL_EVENTS_URL` or `MENHIR_EVENTS_URL` or default `http://127.0.0.1:8090/api/tool-events`
- uses `MENHIR_AGENT_KEY` for bearer auth when present
- fails open (menhir down / unreadable file / malformed input → exit 0, never blocks the tool)

### Supported operations

| Tool name | Operation |
|-----------|-----------|
| Write | write |
| Edit | edit |
| MultiEdit | edit |
| NotebookEdit | edit |
| create | create |
| delete, rm | delete |
| move, rename | rename |

Delete events skip hashing and upload no file data.

## Client support

Claude Code and Codex share a `PostToolUse` hook shape (`tool_name` + `tool_input.file_path`),
so one adapter handles both. **OpenCode** currently has no clean file-event hook surface (its plugin
API is `chat.message`-centric, not tool/file-centric), so v0 does not support OpenCode file events
— documented limitation.

`source_client` is set explicitly (`claude_code` / `codex` / `unknown`).

## Install

Register the hook on `PostToolUse`, matched to the file tools (Edit / Write / MultiEdit /
NotebookEdit). The host pipes the event JSON to the script on stdin.

**Claude Code** (`.claude/settings.local.json`):

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [ { "type": "command",
          "command": "C:\\...\\menhir\\.venv\\Scripts\\python.exe",
          "args": [ "C:\\...\\menhir\\scripts\\hooks\\menhir_file_event.py" ],
          "timeout": 8 } ] }
    ]
  }
}
```

**Codex** (`hooks.json`): a `PostToolUse` entry with the same command. See
`scripts/hooks/file-event-hooks.example.json` for a ready-to-merge Claude/Codex block.

Check what a hook would send without posting:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"src/foo.py"}}' \
  | python scripts/hooks/menhir_file_event.py --dry-run
# {"would_send": true, "operation": "edit", "path": "src/foo.py", "content_uploaded": false}
```

## Configuration (env)

| Var | Meaning |
|-----|---------|
| `MENHIR_TOOL_EVENTS_URL` | Endpoint. Default `http://127.0.0.1:8090/api/tool-events`. |
| `MENHIR_AGENT_KEY` | Bearer token (agent tier). Unset => unauthenticated attempt. |
| `MENHIR_SOURCE_CLIENT` | Override the detected client name. |
| `MENHIR_TURN_EVIDENCE_ENABLED` | Set falsey (`0`/`false`/`no`/`off`) to disable the hook (fail-open no-op). |
| `MENHIR_TURN_HOOK_LOG` | Failure-log path; else `<home>/.claude/menhir-turn-hook.log`. |
| `MENHIR_ARTIFACT_RECONCILE_MODE` | Server-side startup recovery: `off` \| `audit` \| `safe_apply`. Default `audit` — drift is reported, nothing is mutated. `safe_apply` lets the server write to the graph on boot and is an operator choice after the one-time repair. An unrecognized value falls back to `audit`, so a typo can neither disable detection nor enable writes. |
| `MENHIR_ARTIFACT_RECONCILE_REPO` | Working-tree path the startup pass audits. Unset means the pass is skipped regardless of mode. |
| `MENHIR_ARTIFACT_RECONCILE_REPOSITORY` | Stable repository identity recorded on graph source locators. The file-event producer uses this first, then repository-local Git config `menhir.artifactRepository`; it never infers identity from a worktree directory name. Required whenever the startup pass is enabled. |

## Safety and privacy

- No raw file content is uploaded by default.
- No assistant turns are captured.
- No tool transcripts are captured.
- No full transcript mode is added.
- The file hash is local sha256 provenance, not content upload.
- Hooks fail open and do not block Claude/Codex/OpenCode.
- Delete events skip hashing.
- Unreadable files do not crash the hook.
- Malformed hook input exits 0.
- Menhir down exits 0 / logs locally.
- Failure logs record operation + path length, never file content.

To disable: set `MENHIR_TURN_EVIDENCE_ENABLED=0` or remove the hook registration.

## Live smoke

A live end-to-end smoke harness is available at `scripts/smoke/hook_center_live_smoke.py`.
See [`docs/runbooks/hook-center-live-smoke.md`](runbooks/hook-center-live-smoke.md) for setup,
usage, and expected results.

A deeper **stale-anchor lane** smoke — file event -> dirty -> stale detection -> recall label
-> formatter/context warning -> verification-receipt enrichment (path-aware, post-dirty only) —
runs against a real Neo4j at `scripts/smoke/hook_center_stale_lane_smoke.py`. See
[`docs/runbooks/hook-center-stale-lane-smoke.md`](runbooks/hook-center-stale-lane-smoke.md).

---

# Hook Center Actionability Pack v1

Hook Center v0 marks files dirty and makes stale anchors detectable.
Actionability Pack v1 adds stale diagnostics, a report CLI, and an optional
pre-edit policy guard.

## Stale-anchor diagnostic endpoint

`GET /api/tool-events/stale` (readonly) — returns stale anchored memories directly,
separate from the combined dirty-file diagnostic. Supports `?project=` and `?limit=`.
No write behavior, no automatic expiration, no re-anchoring.

## Dirty-file report script

`scripts/maintenance/report_dirty_files.py` — fetches `GET /api/tool-events/dirty` and
prints a human-readable summary or raw JSON. Report-only: never clears dirty flags,
never refreshes structure.

```bash
python scripts/maintenance/report_dirty_files.py          # readable summary
python scripts/maintenance/report_dirty_files.py --json   # raw JSON
```

Config: `MENHIR_TOOL_EVENTS_URL`, `MENHIR_URL`, `MENHIR_AGENT_KEY`.

## Policy Guard

`scripts/hooks/menhir_policy_guard.py` — optional `PreToolUse` hook that can warn or
block edits to protected files before the tool runs. Reads policy from
`.menhir/policy.json`. Never calls an LLM, never uploads file contents.

Modes:

| Mode | Behavior |
|------|----------|
| `watch` | Log locally if useful; exit 0 |
| `warn` | Print a warning if a path matches policy; exit 0 |
| `block` | Print a blocking message if a path matches `frozen_paths`; exit 2 |

Policy format (`.menhir/policy.json`):

```json
{
  "mode": "warn",
  "frozen_paths": [".env*", "*.key", "docker-compose.prod.yml", "migrations/**"],
  "branch_scope": ["scripts/hooks/**", "src/menhir/api/**", "docs/**", "tests/**"]
}
```

- `frozen_paths` — `fnmatch` patterns; a match is always protected.
- `branch_scope` — if set, a path that matches no pattern gets a warning.
- Rename/move checks both old and new paths.
- Non-file tools, missing paths, and malformed input all exit 0 (fail-open).
- No policy file → exit 0 (disabled).

## Recall Stale Labeling (v1)

When recall results are shaped, the service enriches each item with stale-anchor metadata.
Labeling is label-only: stale items are **not** hidden, deleted, expired, or down-ranked.

### Output shape

Each recall result item gains stale metadata when `stale_anchor_info` is available:

```json
{
  "stale_anchor": true,
  "stale_reason": "file_changed_after_anchor",
  "dirty_at": "2026-07-08T12:00:00Z",
  "anchored_at": "2026-07-01T00:00:00Z",
  "path": "src/foo.py",
  "stale_action": "verify_current_file_before_relying",
  "stale_advisory": "This memory is anchored to a file that changed after anchoring. Inspect the current file before relying on it. If outdated, update or supersede the memory."
}
```

Items checked but not stale:

```json
{
  "stale_anchor": false
}
```

Items from recall paths that do not perform stale labeling simply omit these fields.

### Core rule

A memory anchor is stale when:

```
file.structure_dirty = true
AND file.dirty_at > ANCHORED_TO.created_at
```

Meaning: the file changed after the memory was anchored to that file.

### Wiring

- Stale labeling runs inside `RecallService.recall()` after temporal enrichment and before
  the final `RecallResult` construction.
- Calls `adapter.stale_anchored_memories(project=..., limit=200)` (best-effort; failure logs
  and continues without labels).
- The `_compact_scored_item` formatter includes stale fields when `stale_anchor_info` is set.
  When stale, it also adds `stale_action` and `stale_advisory` — LLM-facing action hints
  instructing the agent to inspect the current file before relying on stale memory.
- The `_compact_memory_item` formatter (used by `recall_context_memories`) also passes
  through stale fields when `stale_anchor_info` is present in the row dict.
- `ContextBuilderService.build_context()` emits an inline stale warning line
  (`⚠️ Stale file anchor: <path> changed after...`) when a scored memory is stale.
- `recall_context_memories` tool passes `stale_anchor_info` from recall results through
  to its formatted output.
- `STALE_ACTION` and `STALE_ADVISORY` constants live in `src/menhir/services/stale_labeling.py`
  and are imported by both formatters and context builder.
- Pure helper `label_stale_anchors()` in `src/menhir/services/stale_labeling.py` is available
  for dict-level enrichment outside the service layer.

### Known limitations (v1)

- No automatic structure rebuild or dirty clearing.
- No recall filtering or down-ranking based on staleness.
- No symbol-level invalidation.
- No graph writes from labeling.
- No Phase 3 or TurnEvidence changes.
- Advisory is per-item and repeats for each stale result; no top-level deduplication.
- Advisory tells the LLM what to do but does not enforce action.
- Stale advisory in `recall_context_memories` only applies to `relevant` items (which come
  from `RecallService.recall()`), not `recent` items (which come from `fetch_recent_memories`
  and are not stale-labeled).

## Stale Anchor Verification Receipts (v1)

Verification receipts are durable audit records that capture the outcome of inspecting
a stale file-anchored memory against the current file. They do **not** clear dirty flags,
refresh files, or mutate memory state.

### Allowed outcomes

| Outcome | Meaning |
|---------|---------|
| `still_valid` | Current file was inspected and memory still appears correct |
| `outdated` | Current file was inspected and memory is no longer correct |
| `needs_review` | Agent/user could not confidently determine correctness |
| `superseded` | Memory was inspected and should be superseded by another memory or fact |

### How to record a receipt

```bash
POST /api/tool-events/stale-verifications
{
  "memory_uuid": "m1",
  "project": "menhir",
  "path": "src/foo.py",
  "outcome": "still_valid",
  "verified_by": "agent",
  "basis": "inspected_current_file"
}
```

List receipts:

```bash
GET /api/tool-events/stale-verifications?memory_uuid=m1
```

### CLI helper

```bash
python scripts/maintenance/record_stale_anchor_verification.py \
  --memory-uuid m1 --project menhir --path src/foo.py \
  --outcome still_valid --verified-by agent
```

### How receipts appear in recall/context output

When a stale item has a post-draft verification receipt, the recall output includes
`stale_verification`:

```json
{
  "stale_anchor": true,
  "stale_reason": "file_changed_after_anchor",
  "path": "src/foo.py",
  "stale_action": "verify_current_file_before_relying",
  "stale_advisory": "This memory is stale because its anchored file changed, but it was later verified against the current file. Use it with normal caution.",
  "stale_verification": {
    "outcome": "still_valid",
    "verified_at": "2026-07-09T00:00:00Z"
  }
}
```

For `outdated` outcomes, the advisory changes to a stronger warning:

```json
{
  "stale_action": "do_not_rely_update_or_supersede",
  "stale_advisory": "This memory was verified against the current file and appears outdated. Do not rely on it as current truth. Update or supersede it."
}
```

Only the latest post-dirty verification is used. A verification whose `verified_at`
is before the file's `dirty_at` is treated as pre-draft and not reassuring.

### Important invariants

- Receipts do **not** clear dirty flags.
- Receipts do **not** mutate memory content or state.
- Receipts do **not** auto-refresh files.
- Receipts do **not** filter, down-rank, delete, or expire memories.
- Pre-dirty receipts are excluded from enrichment (conservative).
- Verification lookup failure does not break recall (best-effort).

## Known limitations (v0)

- A file not yet in the structure graph is accepted but marks nothing.
- Symbol-level invalidation is not attempted.
- No automatic structure rebuild: a later structure refresh consumes dirty flags.
- OpenCode has no clean file-event hook surface.
- Recall labels stale anchors in output (stale_anchor=true|false per result), but does not
  automatically down-rank or filter them.

## Non-goals

- No assistant/tool transcript capture.
- No raw content ingestion.
- No automatic structure rebuild in v0.
- No symbol-level invalidation in v0.
- No Phase 3 View changes.
- No TurnEvidence producer behavior changes.
- No new storage architecture.
- No changes to how Graphiti enrichment or the scheduler work.
- Producer `:TurnEvidence` capture and Phase 3 consumer consolidation are untouched — Hook Center is a
  disjoint new endpoint/repo.
