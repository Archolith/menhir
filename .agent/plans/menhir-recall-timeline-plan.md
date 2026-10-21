---
artifact_schema: 1
artifact_type: plan
artifact_status: ACTIVE
---

# menhir -- `recall_timeline`: on-demand time navigation over memories (#172 follow-up)

**Base.** Project scope: `Archolith/menhir`, worktree `C:/Users/thron/IdeaProjects/.agent/worktrees/menhir-timeline`,
branch `feat/timeline-tool`, stacked on `perf/171-source-memory-latency` (`1064612d`, PR #210, which provides
`tenant_scope_prefilter_cypher`).

**Owner decisions (2026-10-01):**
1. Navigate by RECORDED time (episode `valid_at`); world-time facts are optional detail.
2. Separate tool. `recall_history` is unchanged.
3. Typed scalar and event histories are in v1.
4. Remove the appended `build_context` brief timeline.

## Behavior contract

### A. Core (phase A)

**A1. Total order.** Episode order is `(n.valid_at, coalesce(n.created_at, datetime('1970-01-01T00:00:00Z')), n.uuid)`.
- Only episodes with `n.valid_at IS NOT NULL` take part.
- Ties are common: all turns of a session share `valid_at`.

**A2. Visible episode predicate.** Every episode read uses exactly the predicates of
`infrastructure/memory_queries.py::search_episode_embeddings`, minus the embedding ones:
- `n.processing_state IS NOT NULL AND n.processing_state <> 'FAILED'`;
- `non_structural_memory_cypher("n")`;
- `default_recall_visibility_cypher("n")`;
- `tenant_scope_cypher("n")`, with `tenant_scope_prefilter_cypher("n")` FIRST when the namespace is scoped
  (same `namespace_spellings` check);
- params from `tenant_scope_params(namespace)`.

No hand-written `.namespace` / `.group_id` comparisons: the CF-127 guard `tests/test_cf127_tenancy_scope_predicate.py`
must still pass.

**A3. Queries** (new methods in `infrastructure/memory_queries.py` + pass-throughs in `memory_graph_adapter.py`).
All are read-only and set no `last_accessed`.
- `timeline_page(*, namespace, subject_uuid=None, after=None, before=None, window_from=None,
  window_to=None, limit)`
  - `after` / `before` are cursor keys `(valid_at_iso, created_at_iso, uuid)`, both exclusive.
  - Returns rows `{uuid, valid_at, created_at, content, session_id, source}` in ASCENDING order.
  - `before` queries DESC with LIMIT and the method reverses the rows.
  - With `subject_uuid`: `MATCH (s:Entity {uuid:$subject_uuid})<-[:MENTIONS]-(n:Episodic)`.
  - Without it: `MATCH (n:Episodic)`.
  - Compare instants with `datetime($x)`.
- `timeline_anchor(*, uuid, namespace, subject_uuid=None)`: the anchor episode row, only if it passes A2
  (and MENTIONS the subject when given). Otherwise None.
- `timeline_facts(*, episode_uuids, namespace)`
  - Returns `{episode_uuid: [ {fact, valid_at, invalid_at, expired_at} ]}` from
    `(:Entity)-[r:RELATES_TO]->(:Entity)` where `ANY(e IN r.episodes WHERE e IN $episode_uuids)`.
  - Tenancy via `tenant_scope_cypher("r")`.
  - At most 50 episodes and 20 facts per episode, ordered by `r.valid_at`.
- `resolve_timeline_subject(*, subject, namespace)`
  - Accepts an entity uuid, or an exact case-insensitive `name` among visible `:Entity` nodes in scope
    (A2's tenancy + `default_recall_visibility_cypher`, not views: `NOT coalesce(n.is_view,false)`).
  - Returns up to 5 `{uuid, name}`.

**A4. Domain** (new `domain/timeline.py`, frozen dataclasses):
- `TimelineEntry`
  - `uuid, recorded_at (ISO), created_at (ISO|None), session_id, source`;
  - `headline` (whitespace-collapsed content, at most 160 chars + `…`);
  - `content` (None unless `detail="full"`; then at most `_RECALL_HISTORY_MAX_CHARS` from `recall_pipeline`);
  - `facts: tuple[TimelineFact, ...]`;
  - `is_anchor: bool`.
- `TimelineFact`: `fact, valid_at, invalid_at, expired_at, time_basis="world"`.
- `TypedHistory`: `kind ("scalar"|"event"), view_uuid, label (attribute or predicate[/domain]), total,
  offset, entries: tuple[TypedHistoryEntry,...], prev_offset, next_offset`.
- `TypedHistoryEntry`: `valid_at, value` (scalar value or event `object_display`), `operation`
  (scalar) or `time_basis` (event), `quote` (`stated_span` / quote), `episode_uuid`.
- `TimelineResult`: `thread ("namespace"|"subject"), subject_uuid, subject_name, entries,
  prev_cursor, next_cursor, histories, note, time_basis="recorded"`.
- Cursor codec: `encode_cursor(key, direction, namespace_key, subject_uuid)` /
  `decode_cursor(token, *, namespace_key, subject_uuid)`.
  - Format: urlsafe-base64 JSON `{"v":..,"c":..,"u":..,"d":"after"|"before","ns":..,"s":..}`.
  - `ns` = `"|".join(namespace_to_group_ids(namespace) or ["*"])`.
  - Decode raises `ValueError` on bad base64/JSON, missing keys, an unknown direction, or an `ns`/`s`
    mismatch, so a cursor cannot cross tenants or threads.

**A5. Service** (`services/timeline_service.py`):
`async run_recall_timeline(service, *, namespace, query=None, subject=None, at=None, window_from=None,
window_to=None, around=None, cursor=None, direction="both", limit=10, detail="headline", facts=False,
history_view=None, history_offset=None, history_limit=10) -> TimelineResult`

- **Starting point.** Exactly one of `at` / (`window_from` and/or `window_to`) / `around` / `cursor` /
  `query`. Otherwise `ValueError`.
  - Exception: `subject` alone is allowed and means "latest page of the subject thread".
- **Arguments.**
  - `limit` clamps to 1..50 and `history_limit` to 1..50.
  - `detail` is in {headline, full}; `direction` is in {before, after, both}.
  - Invalid ISO instants raise `ValueError`.
- **Subject.** Resolved via `resolve_timeline_subject`.
  - 0 matches: `ValueError("unknown subject")`.
  - More than 1 match: `ValueError` listing candidates `name (uuid)`.
- **Modes:**
  - `at`
    - `direction=both`: `limit//2` entries at or before plus the rest after. "At or before" includes
      equal instants: implement as `before` the key `(at, +inf, +inf)` and `after` the same key, using
      a sentinel high created_at/uuid.
    - `before` / `after`: one side.
  - Window: ascending from the window start, `limit` entries, `next_cursor` if more.
  - `around`: anchor (`is_anchor=True`) plus neighbors (split like `at`). Anchor not visible:
    `ValueError("unknown or hidden memory")`.
  - `cursor`: continue in the cursor's direction from its key.
  - `query` only: seed = top-1 hit of `graph_adapter.search_episode_embeddings` (embed via
    `graphiti_client.embed_query`), then behave as `around` that uuid. No hits: empty result with note.
  - `subject` only: latest page (`before` the max key).
- **Cursors.** `prev_cursor` = key of the first entry with direction `before`. `next_cursor` = key of
  the last entry with direction `after`. Each is None when that side was probed and returned fewer
  rows than requested; probe one extra row to know.
- **`facts=True`.** Attach `timeline_facts` for the page's episodes.
- **Typed histories** (only when the thread is a subject):
  - Group id: `gid = namespace_to_group_ids(namespace)[0]` when scoped, else None.
  - Views: `graph_adapter.list_scalar_history_views(subject_uuid=..., namespace=gid)` and
    `graph_adapter.list_event_timeline_views(subject_uuid=..., namespace=gid)`.
  - Each history's page comes from `list_scalar_history_entries` / `list_event_timeline_entries`
    (`view_uuid`, offset, limit, namespace=gid).
  - The default page is the LATEST `history_limit` entries: `offset = max(0, total - history_limit)`.
    Fetch the total first via a page call with `limit=1`, or use the `total` returned.
  - `history_view` + `history_offset` page one history. With `history_view`, only that history is
    returned.
  - At most 10 histories; note when truncated.
- **Failures.** Graph and embedding failures raise. No partial results, no access updates, as
  `run_recall_history`.
- **Wiring.** Expose as `RecallService.recall_timeline(...)` in the same way as `recall_history`.

### B. Wiring and removal (phase B)

**B1. Surfaces.** Mirror every `recall_history` surface:
- `core/backend_protocol.py` (~153), `core/backend_client_ops.py` (~123),
  `core/backend_runtime_data_ops.py` (~358);
- REST `POST /api/recall/timeline` in `api/routes.py` (beside ~195), with `RecallTimelineRequest` /
  `RecallTimelineResponse` in `api/routes_support.py` (beside ~321);
  - add `"recall_timeline"` to the list at `routes_support.py` (~653);
  - namespace resolution identical to `/api/recall/history`;
  - `ValueError` maps to 400 as the history route does;
- MCP `mcp/tools/recall/recall_timeline.py` (pattern: `recall_history.py`), registered in
  `RECALL_TOOLS`, not `always_visible`;
- registries: `access_contract.py` (~53, `AGENT_ALLOWED_TOOLS`), `explorer/feature_taxonomy.py` (~28),
  `mcp/feedback.py` (~35 `RATABLE_OPERATIONS`), `mcp/tools/ops/get_memory_stats.py` (~23),
  `.agent/concept-ids.yaml` (`mcp.tool.recall_timeline`), `endpoints.md`, `tasks-mcp.md`.

**B2. Remove the appended brief timeline:**
- `services/context_builder.py`: the `brief_builder_enabled` field and its comment, plus the append
  block (`if not fail_closed and self.brief_builder_enabled and not truncated:` ... through its end).
- `config/settings_model.py`: `frontier_brief_builder` field and env parse.
- Both bootstraps (`core/bootstrap.py`, `cli/bootstrap.py`): `brief_builder_enabled=` args.
- `.env.example`: the `MENHIR_FRONTIER_BRIEF_BUILDER` lines.
- Delete `src/menhir/domain/brief_builder.py` (no other users).
- Delete ONLY `tests/test_context_builder.py::test_brief_lifecycle_note_survives_long_fact_clipping`
  (it tests the deleted module), plus any import only it used.
- Keep `include_invalidated=True` in build_context recall: it has its own documented reason.
- Docs: `.agent/default-off-features.md` brief row becomes "removed 2026-10-01, superseded by
  recall_timeline", and the brief mention in `.agent/plans/menhir-feature-flag-registry.md`.
- Any test or doc that still references `frontier_brief_builder` / `MENHIR_FRONTIER_BRIEF_BUILDER` must
  be updated (grep). Report each one.

**B3. Docs.** `.agent/CHANGELOG.md` gets one dated entry (2026-10-01) covering the new tool and the
brief removal.

## Tests (new `tests/test_recall_timeline.py`; existing tests only as B2 says)

**Phase A**
- **Unit-test the query strings** like `tests/test_source_memory_latency_171.py` / `test_recall_history.py`:
  - prefilter first when scoped and absent when unscoped;
  - `tenant_scope_cypher` present;
  - no hand-written namespace comparison;
  - MENTIONS join only with a subject.
- **Cursor codec:**
  - round-trip;
  - tamper or garbage rejected;
  - namespace mismatch and subject mismatch rejected;
  - `"default"` and `""` produce the same `ns` key.
- **Service with a stub adapter** (fake rows):
  - tie ordering by created_at then uuid;
  - `at` split and the inclusive-equal rule;
  - window paging and next_cursor;
  - `around` anchor flag and hidden anchor -> ValueError;
  - `query` seeds `around`;
  - `subject` latest page;
  - exactly-one-start validation;
  - limit clamps;
  - `detail=full` content cap and headline cap;
  - `facts=True` attaches facts;
  - typed histories default to the latest page, `history_view`/`history_offset` paging, at most 10
    histories, gid for default `""`;
  - ambiguous subject -> ValueError with candidates;
  - no access-update calls.

**Phase B**
- REST, MCP and backend parity like `tests/test_recall_history.py`.
- `AGENT_ALLOWED_TOOLS` contains `recall_timeline`.
- build_context with any settings never contains `=== Timeline ===`.
- `MemorySettings` has no `frontier_brief_builder`.
- The CF-127 guard still passes.

## Test policy (executor)
- **Phase A:** `uv run pytest -q tests/test_recall_timeline.py tests/test_cf127_tenancy_scope_predicate.py
  tests/test_recall_history.py`.
- **Phase B:** additionally `tests/test_context_builder.py tests/test_context_packing_172.py` (if present),
  `tests/test_api_routes.py`, plus any test touching `access_contract`/`feature_taxonomy`/`RATABLE`
  (grep).
- `uv run ruff check` on changed files. At most once more after a fix.
- No full suite (the reviewer runs it). No commit, no push.

## Non-goals
- No write-path changes, stored NEXT/PREV edges, or schema/index changes.
- No change to `recall_history`.
- No default flips.
- No change to the typed-history repositories.
- No ranking changes.
