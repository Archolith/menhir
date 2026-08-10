# Plan: hook-center-tool-events-v0

Branch: `feat/hook-center-tool-events-v0` (menhir) + a Claude/Codex file-event hook.

**Goal:** reduce stale file references by OBSERVING file/edit events through hooks and marking affected
structure-file nodes / anchored memories dirty — instead of trusting the LLM to call a memory tool.

## Grounding (existing code, 2026-07-08) — REUSE, don't reinvent

menhir already has the machinery:
- Structural code graph = `:Entity` nodes with `structure_project`, `structure_path`,
  `structure_role='file'`, `file_mtime` (`infrastructure/structure_queries.py`).
- Anchoring = `(sem:Entity {structure_role IS NULL})-[:ANCHORED_TO {created_at: datetime()}]->
  (struct:Entity {structure_role:'file'})` (`infrastructure/structural_anchoring.py`). The edge's
  `created_at` IS the anchor time.
- Staleness domain = `domain/git_staleness.py` (`derive_structural_staleness`, worktree-hash compare).
- Adapter delegates to repos; route reads `runtime_ctx.built.graph_adapter` (turn-evidence pattern).

So v0 = a narrow event layer that dirty-marks the EXISTING file `:Entity` and exposes stale detection.
**No new node architecture, no schema change** (dirty markers are properties on existing nodes; we
MATCH, not MERGE) → stays a targeted-gate change, not a shared-infra/full-suite one.

## Design

1. **Normalized event schema** (`ToolEventRequest`, Pydantic) — the suggested shape: `event_type`
   (v0: `file_changed`), `source_client`, `source_kind`, `session_id`, `namespace`, `project_root`,
   `cwd`, `path` (optional at model level; required at runtime for `file_changed`), `old_path`,
   `operation` (write|edit|delete|rename|create), `before_hash`, `after_hash`, `mtime`, `git_branch`,
   `git_commit`, `metadata`. Unsupported `event_type` values are accepted-and-ignored without path.
2. **Endpoint** `POST /api/tool-events` (agent tier) — forward-compatible name (handles tool + file);
   v0 processes `file_changed`. Validates shape, calls the repo via `asyncio.to_thread`, returns
   `{accepted, matched, marked_dirty, operation}`. Never needs file content or a transcript.
   `GET /api/tool-events/dirty` (readonly) — diagnostic list of dirty files + stale anchors.
3. **`ToolEventRepository`** (`infrastructure/tool_event_repository.py`, mirrors TurnEvidenceRepository):
   - `record_file_event(...)`: MATCH file `:Entity` by `(structure_project?, structure_path=path)`;
     SET `structure_dirty=true`, `dirty_at=datetime()`, `last_event_op`, `last_event_after_hash`,
     `last_event_mtime`, `last_event_source_client`. rename → mark BOTH `old_path` and `path`. Returns
     `{accepted, matched, marked_dirty, project_fallback_used}`. **Project-scope fallback:** if
     `project` is supplied and the scoped match returns 0, retries with `project=None` (path-only) so
     a `structure_project` mismatch is not a silent miss. Stores NO content. If no file node matches
     → accepted, not marked (documented v0 limit: file not yet scanned).
   - `list_dirty_files(project=None)` — diagnostic.
   - `stale_anchored_memories(project=None)` — `(sem)-[a:ANCHORED_TO]->(f {structure_dirty:true})`
     WHERE `f.dirty_at > a.created_at` → the "anchored memory detectable as stale" behavior.
   - `clear_file_dirty(project, path)` — teardown/idempotence.
4. **Adapter delegators** on `memory_graph_adapter`: `record_file_event`, `list_dirty_files`,
   `stale_anchored_memories`, `clear_file_dirty` (additive).
5. **Hook** `scripts/hooks/menhir_file_event.py` — stdlib, reads a Claude/Codex `PostToolUse` JSON
   (tool_name in Edit/Write/MultiEdit/NotebookEdit; `tool_input.file_path`), normalizes → event,
   local sha256 of the file (hash only, never content), POSTs to `/api/tool-events`, fail-open. Reuses
   `menhir_turn_evidence_common` helpers (git_probe, log_failure, config). `source_client` explicit.
   Codex/Claude share the PostToolUse shape → one adapter. OpenCode has no clean file-event hook
   surface (its plugin API is chat.message-centric) → documented limitation.
   **Path normalization:** absolute paths under `project_root` are converted to repo-relative before
   POSTing (e.g. `/repo/src/foo.py` → `src/foo.py`). The original path may be stored in
   `metadata.original_path`. Hashing uses the original filesystem path.
6. **Tests** (targeted): schema accepts minimal event; missing optional metadata non-fatal;
   Claude/Codex normalization; hook fail-open when Menhir unreachable; dirty marking; anchored-memory
   stale detection; delete/rename don't crash; no file content sent.
7. **Docs** `docs/hook-center-tool-events.md` — hook-center model, captured/not-captured, safety
   defaults, stale-marking, install/disable, the invariant.

## Non-goals honored
No assistant/tool transcript capture, no raw content ingestion, no auto structure rebuild, no new
Phase 3 View kinds, no consumer extraction change, no freeform Cypher, no new storage arch. Producer
TurnEvidence + Phase 3 consumer behavior unchanged (this is a disjoint new endpoint/repo).

## Gate (risk-based, per the cadence — NOT full suite)
New subsystem, additive (new endpoint/repo/adapter delegators; no schema/runtime/dep change):
`pytest tests -q -k "tool_event or file_event or hook_center or dirty or stale"` + adjacent
structure/anchoring/api-route suites + a mocked-adapter smoke. Full suite NOT run (no shared-infra
trigger).
