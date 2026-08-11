# Productionize the View primitives — proven → always-on

> **ARCHIVED 2026-07-11 (ctharvey-approved).** All three workstreams verified shipped and live:
> WS1 `sync_experience_counters` scheduler job (`maintenance_scheduler.py:34,121,347,450`, off under
> benchmark mode), WS2 embedder seam (`embed=self.experience_embed` threaded into the job), WS3
> `entity_view_key_idx` (`schema.py:42,104`). The "READY TO EXECUTE / inert" block below is the
> original pre-execution plan body, superseded by the COMPLETE banner. Design rationale lives in the
> `event-fold-view-architecture` and `aggregation-as-consolidation` frame docs. Archived per owner
> rule (a) fully implemented/shipped.

**Status: COMPLETE (2026-07-02).** All three workstreams shipped and committed on branch
`claude/menhir-chain-handoff-doc-7iuat2`; full unit suite green (912 passed, 3 skipped). WS3 (schema
indexes) and WS1 (scheduler job) landed first, then WS2 (sync embedder seam). The primitives now run
on their own — the hourly `sync_experience_counters` maintenance job folds telemetry into
supersedable counter Views in prod (off under benchmark mode), each carrying a cosine surface when an
OpenAI-compatible embed provider is configured. **Deviation:** WS2 edit 3 (wire the embedder at the
consolidator's "scheduled call site") was dropped — `quantstate_consolidator.consolidate()` has no
production/scheduled call site (invoked explicitly only) and already accepts an injectable `embed`,
so there was nothing to wire. See CHANGELOG 2026-07-02.

---

**Status: READY TO EXECUTE (planned 2026-07-02).** The two experience-counter primitives and the
QuantState consolidator are built, tested end-to-end, and committed — but **inert**: they only fire
on manual invocation. This plan makes them run on their own in production. Companion to
[`event-fold-view-architecture.md`](event-fold-view-architecture.md) (historical architecture decision) and
[`ingest-primitive-family.md`](../../reference/ingest-primitive-family.md) (the MVP cut).

Three independent workstreams; do them in order (each is safe alone). One project: menhir-frontier.

## Current state (what's built, where)

- `infrastructure/view_repository.py` — `ViewRepository` (generic) + `CounterKind`/`TimelineKind`;
  `record_counter`/`record_timeline`. `QuantStateRepository` = alias.
- `services/failure_counter_bridge.py` — `sync_failure_counters(store, graph_adapter, namespace,
  subject, min_count)` folds `telemetry.failure_events` → counters.
- `services/instability_counter_bridge.py` — `sync_instability_counters(store, graph_adapter,
  namespace, min_count)` folds `telemetry.memory_revisions` → belief-instability counters.
- `services/quantstate_consolidator.py` — prose→perception→fold→`record_counter` (injected LLM +
  embed).
- Both bridges call `graph_adapter.record_counter(...)` **without `name_embedding`** today (BM25
  surface only, no cosine vector).

## Workstream 1 — schedule the counter bridges in the real consolidation loop

**Seam (confirmed):** `services/maintenance_scheduler.py` `MaintenanceScheduler` already runs
periodic jobs of exactly this shape (`retry_failed_enrichments`, `auto_resolve_conflicts`, …). Add
one more job. Constructed at `core/runtime.py:181`; **disabled under `MENHIR_BENCHMARK_MODE=1`**
(`runtime.py:470`) — so it auto-runs in prod, stays manual in benchmark (exactly the promote/
backfill pattern we want; no benchmark regression).

**Telemetry store access:** the scheduler already uses module-level `record_failure_event` /
`record_mcp_event` from `menhir.infrastructure.telemetry` (imports at
`maintenance_scheduler.py:15`). The `McpTelemetryStore` is reachable the same way — the new task can
obtain the store from that module rather than threading it through the constructor. Confirm the
module exposes a store accessor; if not, add one (cheap) rather than widening the constructor.

**Edits (mirror the existing `retry_failed_enrichments` job exactly):**
1. `maintenance_scheduler.py` constructor field: `experience_counter_interval_s: float = 3600.0`
   (+ `experience_counter_enabled: bool = True`). Hourly is plenty — counts are cumulative.
2. `__post_init__` (`:86`): register `"sync_experience_counters": _JobState(interval_s=...)` (guard
   on the enabled flag like `refresh_structure_graphs` at `:94`).
3. `_run_due_jobs` (`:238`): add an `elif name == "sync_experience_counters"` dispatch.
4. Add `_make_sync_experience_counters()` factory (`:317` block).
5. `services/scheduler_tasks.py`: add `async def sync_experience_counters(graph_adapter, *,
   namespace="agent-experience") -> dict` that (a) gets the telemetry store, (b) calls
   `sync_failure_counters(...)` and `sync_instability_counters(...)`, (c) returns
   `{"failure_counters": n, "instability_counters": m}` for the scheduler's telemetry record.
   Match the return-dict + signature style of `retry_failed_enrichments`.

**Verify:** unit-test the task with a fake store + adapter (assert both bridges invoked, dict
shape). Integration: with telemetry rows present and benchmark mode OFF, start a server, confirm the
job appears in `status_snapshot()["jobs"]` and increments `runs` after one interval.

## Workstream 2 — wire the embedder seam (counters get a cosine surface)

**Why:** bridges omit `name_embedding`, so counters are BM25-only. Recall works (validated) but a
paraphrased query with no lexical overlap leans entirely on BM25. A real embedding = cosine surface
too (the QuantState surfacing test used `text-embedding-3-small` and got cosine 0.813).

**Seam:** the embedder lives behind `infrastructure/providers.py` / `infrastructure/graphiti_client.py`
(same one ingest uses). Source it from `built` in `runtime.py` and pass an `embed: Callable[[str],
list[float]]` down.

**Edits:**
1. `failure_counter_bridge.py` / `instability_counter_bridge.py`: add optional `embed=None` param;
   when provided, pass `name_embedding=embed(<the record's retrieval surface>)` to `record_counter`.
   The surface for a counter is `ViewRepository.retrieval_text(subject, counter, value)` — embed
   THAT (the same text `record_counter` writes as `name`), not the raw key.
2. `scheduler_tasks.sync_experience_counters`: obtain the embedder from the graph adapter / built
   context and thread it into both bridge calls.
3. `quantstate_consolidator.py`: same — its `embed` is already injectable; wire the real one at the
   scheduled call site (today it's test-injected).

**Guard:** embedding failure must not drop the counter — wrap `embed()` so a provider error logs +
falls back to `name_embedding=None` (BM25-only) rather than losing the write.

**Verify:** after a scheduled run, a counter node has a non-null `name_embedding`; a paraphrased
`/api/recall` (no shared words) surfaces it (reuse the qs_surface paraphrase probes).

## Workstream 3 — `view_key` schema index migration (the careful one)

**Reality check:** lower-risk than feared. The L4 artifact indexes set the exact precedent
(`schema.py:34-41` + `PHASE_ONE_REQUIRED_INDEXES`): add the index to the required list, bootstrap
creates it `IF NOT EXISTS`, the graph reports `schema_not_ready` **only** for the brief window until
the index builds on first boot after the change, then green. That transient window is the intended,
benign behavior — not a risk to design around.

**Edits (`infrastructure/schema.py`):**
1. Add a `_view_index_queries()` helper (mirror `_artifact_index_queries()` at `:81`):
   `CREATE INDEX entity_view_key_idx IF NOT EXISTS FOR (n:Entity) ON (n.view_key)`,
   `entity_view_kind_idx ON (n.view_kind)`, `entity_view_current_idx ON (n.view_current)`.
   (`view_key` is the supersession lookup in `_current_by_key`; `view_kind`+`view_current` back the
   `_fetch_current`/`list_views` filters.)
2. Include it in `get_phase1_bootstrap_queries()` (`:221`).
3. Add `"entity_view_key_idx"` (at least) to `PHASE_ONE_REQUIRED_INDEXES` (`:25`).
4. Do NOT bump `_SCHEMA_V` — no node backfill needed (these are indexes, not new node fields; View
   nodes already carry the props).

**Verify:** on a fresh boot, `SHOW INDEXES` lists `entity_view_key_idx` ONLINE; the schema-ready
check passes; `_current_by_key`'s `MATCH (n:Entity) WHERE n.view_key=$k` uses the index (PROFILE
shows NodeIndexSeek, not AllNodesScan). On the existing built graph, confirm the one-boot
`schema_not_ready` window closes after the index populates.

## Sequencing & risk

1. **WS3 first** (index) — pure additive DDL, unblocks efficient supersession lookups, precedent-backed.
2. **WS1** (schedule) — the behavior change; gated OFF in benchmark so no A/B regression.
3. **WS2** (embedder) — quality bump on top; guarded so it can't drop writes.

Each ships + commits independently. None touches recall (already done) or the write core.

## Out of scope (tracked elsewhere)
- D0 entropy win-metric proof (floor→1 on a counting slice) — separate measurement task.
- A third View kind (`current_value`/preference) — separate capability.
- LME framework reorg (`recursive-toasting-lovelace.md`) — archolith-bench, separate session.
