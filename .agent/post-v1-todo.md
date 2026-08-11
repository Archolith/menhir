# Post-v1 TODO

All 12 milestones (M0–M7) are complete. This document tracks post-v1 work discovered during implementation, organized by priority and effort.

> Scope: this doc owns bugs, ops, and deferred features on the **shipped** system.
> The research build-out (oracle pipeline, belief buckets, retrieval tuning,
> control rails, cognitive replay) is sequenced separately in
> `.agent/research/menhir-research-execution-ladder.md`, which draws on the
> `docs/research/` corpus. Keep the two separate: shipped-system work here,
> research → production rungs there.

---

## Priority 1 — Bugs & Code Gaps

### ~~Context window retry budget is unreachable~~ FIXED (2026-03-21)

`retry_process_candidate` now detects context-window errors via `is_context_window_error_text()` and extends the retry cap from `max_attempts` (3) to `context_retry_attempts` (6). Context-window errors bypass the "terminal" classification gate.

### ~~Edge fact repair~~ DONE (2026-03-21)

Synthetic edge facts (mechanical `"{source} {relation_type} {target}"` pattern) are now marked with `[synthetic] ` prefix during Graphiti extraction. `stamp_and_finalize` runs a post-extraction LLM repair pass that rewrites synthetic facts using episode text as context. Provenance tracked via `fact_source` field on edges: `original` / `llm_repaired` / `synthetic_fallback`. Mechanical synthesis kept as last safety net when LLM is unavailable or fails.

---

## Priority 2 — Operational Improvements

### ~~Enrichment SLO metric in `get_memory_stats`~~ DONE (2026-04-01, `8047112`)

`fetch_enrichment_rate()` returns `p95_duration_ms` alongside `avg_duration_ms`
(`infrastructure/telemetry/recall_store.py:387`), and `get_memory_stats` renders it against the
120s target with an explicit `ok` / `MISS` flag (`mcp/tools/ops/get_memory_stats.py:66-73`,
`slo_target_ms = 120_000`). The 95%-within-120s target is checkable without manual SQL.

The telemetry module has moved since (`491f048`, 2026-07-22, telemetry/Graphiti patch split), so
grep for the function name rather than trusting the paths above verbatim.

### ~~Scheduler lifecycle MCP tools~~ DONE (2026-04-01, `8047112`)

`pause_scheduler` and `resume_scheduler` ship as MCP tools (`mcp/tools/ops/pause_scheduler.py`,
`mcp/tools/ops/resume_scheduler.py`). Process restart + `force_release_enrichment_lease` is no
longer the only workaround.

### llama.cpp throughput reservation
No traffic prioritization between menhir enrichment and other llama.cpp consumers (delegate, crypto scheduler). Future capacity control should reserve throughput per workload.

**Source:** `.agent/architecture.md:60`

### Replay harness: log-to-fixture converter
Phase 1 uses hand-authored JSON fixtures. Build a converter from real conversation logs → fixture JSON once the fixture format is validated through usage.

---

## Priority 3 — Deferred Features (Frozen in v1 Scope)

### Lifecycle stages: STALE and ARCHIVED
v1 has `ACTIVE → COMPRESSED → GONE`. Post-v1 adds `STALE` (between ACTIVE and COMPRESSED — signals "aging but not yet summarized") and `ARCHIVED` (long-term cold storage, retrievable but deprioritized).

**Source:** `config/feature_scope.py:24`

### Independent edge decay
Edges currently follow endpoint node lifecycle. Post-v1: edges decay independently based on `last_traversed` and `weight`, allowing high-traffic edges to survive even when connected nodes age.

**Source:** `memory-roadmap.md:62`

### Emotional arousal in sharpness
v1 sharpness = `1/(1+similar_count)` (uniqueness only). Post-v1: integrate emotional arousal as secondary sharpness signal for episodic and preference memory types.

**Source:** `lifecycle_service.py:81-86`

### Emotional queries as primary UX mode
The `emotional` preset exists (α=0.1, β=0.2, γ=0.1, δ=0.0) but has no emotional arousal signal flowing into scoring. It differs from other presets only in adjacency/recency balance. Wire real emotional signals for meaningful differentiation.

**Source:** `architecture.md:172`, `recall.py:21`

### Automated skill/hook promotion
Pattern repetition → passive skill or active hook. Requires cross-session repetition detection, explicit guardrails, quotas, and review loops. Currently manual-only via `flag_memory`.

**Source:** `memory-design.md:197-207`, `config/feature_scope.py:26-27`

### Pending action types (scaffolded, not implemented)
The `pending_actions` SQLite table is ready for three future action types:
- `resolve_conflict` — LLM-backed contradiction resolution
- `refine_summary` — periodic summary improvement
- `extract_emotions` — emotional quotient enrichment

**Source:** `infrastructure/pending_actions.py:9-13`

---

## Priority 3b — Deferred Refactoring

### Wire `TruthAttestation` into pipeline output

`domain/truth/` is the SSOT for all provenance/trust vocabulary (`ReviewState`, `TruthAttestation`, `WardenLabel`, `SOURCE_CONFIDENCE_*` constants). The types and factories are live and tested, but the pipeline still emits scattered objects (`ScoredMemory.warden_label`, `AdmissionResult.label`, raw `source_confidence` floats). The migration path is:

- `RecallService.recall` → return `TruthAttestation` alongside or instead of `ScoredMemory`
- `AssertionPipeline.run` → `AdmissionResult` wraps or converts to `TruthAttestation`
- MCP formatters → accept `TruthClaim` protocol rather than bare field access

Until this is wired, `TruthAttestation.from_source_confidence()` and `TruthAttestation.unscored()` are available as the migration shim for any path that is refactored.

**Source:** `domain/truth/attestation.py`, `services/recall_service.py`, `services/assertion_pipeline.py`

### ~~`ingest_service.py` enrichment step extraction~~ DONE (2026-03-21)

Enrichment pipeline extracted to `services/enrichment_steps.py` with `EnrichmentContext` dataclass. `ingest_service.py` is now a thin orchestrator. Step functions (`try_reconcile_existing`, `run_preflight_rejection`, `run_graphiti_extraction`, `stamp_and_finalize`, `handle_enrichment_failure`) are independently testable.

---

## Priority 4 — Design Expansions

### Conversational git / turn-native change history
The current `diff` attachment path already captures per-episode code changes plus surrounding narrative. A next-step design is to treat turns or episodes as the primary unit of change history, with git commits remaining the canonical code artifact underneath.

Desired properties:
- query by turn/episode rather than only by commit
- bind prompt, response, diff, touched files, and rationale into one recoverable unit
- answer "why did we change this?" and "what reasoning led to this diff?" rather than only "what changed?"
- build a construction narrative across multiple turns even when commits are squashed, delayed, or absent

This is the natural expansion of the existing diff + episode + structural anchoring model, not a separate version-control system.

**External validation (2026-05-06):** `regent-vcs/re_gent` has shipped a standalone version of this primitive as a Go CLI (`rgt log`, `rgt blame`, `rgt rewind`) built on a BLAKE3-hashed DAG of Steps stored in `.regent/` alongside `.git/`. They've proven real appetite for this. Their architecture is external + append-only; ours is graph-native with semantic extraction on top — the approaches are complementary. The one primitive they have that we lack entirely: **per-line prompt attribution** (`rgt blame`). See memory-backlog.md `git_diff_attachment` follow-up ideas for the graph-native version of this.

### Temporal awareness
v1 uses simple recency signal (`last_accessed` timestamp). Post-v1 design intent:
- Store time structure in the graph (rhythm, epochs)
- Compute suppression/resurfacing in a separate reasoning layer
- Distinguish "fresh but redundant" from "old and worth resurfacing"
- Drift detection

**Source:** `memory-design.md:242-254`

### Freeform Cypher query generation
Currently fixed query templates only. Opening up to agent-authored Cypher requires: sandbox, dry-run mode, read/write guardrails, resource limits.

**Source:** `memory-design.md:258-269`

### ~~Code graph companion MVP — Phase 0~~ DONE (2026-03-21)

`ingest_project` MCP tool scans a project directory and writes structural entities (project, directory, file, entrypoint, config, test, endpoint, dependency) + edges (CONTAINS, DEPENDS_ON, TESTS, IMPORTS, EXPOSES, CALLS) directly to Neo4j. Also queues a narrative episode for Graphiti semantic extraction. Fingerprint-based skip detection for unchanged projects.

### ~~Code graph Phase 1: incremental diff + heat tracking~~ DONE (2026-03-26)

- **Per-file mtime incremental diff**: `FileEntry.file_mtime` populated during scan. Stored on File Entity nodes. `write_project` queries stored mtimes at start, computes `changed_paths` / `deleted_paths`, passes to `_write_symbols` for selective delete+write. First scan or force = full replace; subsequent scans = only changed files' symbols are deleted and rewritten.
- **Background error surfacing**: Fire-and-forget writes (`_do_write`, `_background_symbol_rescan`) push errors to a server-side `deque`. `routes.backend_invoke` drains and attaches as `x-yawn-bg-warnings` response header. `BackendClient._request` reads and stores in client-side `deque`. `BaseTool.execute` appends `[background-error]` lines to next MCP tool response.
- **Fingerprint skip fixed**: `_merge_entity` ON MATCH SET now includes `n += $extra`, so `scan_fingerprint` updates after first write. `logs/` and `.server.pid` excluded from fingerprint via `.gitignore`.
- **Heat tracking**: `hot_count` property on File Entity nodes, incremented each time a file appears in `changed_paths` during incremental write. Exposed in `query_structure("files")` output.

Remaining phases:
- **Phase 2**: Multi-project workspace scan (`ingest_workspace`), cross-project edge validation, embedding generation for structural entities, structural recall preset
- **Phase 2b**: Document ingestion (`ingest_document`) — queue doc file content as a narrative episode for Graphiti semantic extraction; creates a `structure_role: "document"` entity so `file_context` recall and `blast_radius` surface doc-linked memories. Complements code graph without replacing it.
- **Phase 3**: language-specific import/endpoint/reference parsers beyond the current Python-first quality level

### TODO graph contract cleanup
The direct `:Todo` subsystem now exists in the runtime, hook UX, context builder, and blast-radius output, but it is still a parallel surface rather than a fully documented first-class graph concept.

Follow-up work:
- define whether TODOs are operator-only state or part of the canonical graph model
- add explicit project-aware file-linking rules instead of suffix-only path matching
- decide whether TODOs belong inside context token budgeting or should remain a separate adjunct section
- extend live/backend integration tests beyond repository/unit coverage
- ~~single-todo read~~ DONE (2026-08-02) — `get_todo(uuid)` returns the full record. The
  read surface was list-only, and `list_todos` truncates content at 100 chars, so a long
  multi-part todo could not be read back through any tool: the text existed in the graph
  but was unreachable. `get_todo` also returns the edges written at create time
  (REFERENCES_FILE / CREATED_FROM / CONCERNS), and `list_todos` now names it when it
  truncates.

### Full data reconciliation
Sidecar consistency is spot-checked only. Build full migration safeguards and audit trail alignment between Neo4j graph state and SQLite sidecar.

**Source:** M7 implementation plan (archived)

---

## Priority 5 — Scale & Hardening

### Threshold tuning from operational data
All v1 thresholds are fixed constants. Once real usage generates enough data, consider percentile-based decay brakes and adaptive promotion thresholds.

| Threshold | Current | Revisit When |
|-----------|---------|--------------|
| `SHARPNESS_PROMOTE_THRESHOLD` | 0.5 | Uniqueness distribution changes |
| `PERSISTENT_EDGE_PROMOTE_THRESHOLD` | 3 | Graph grows past ~1000 entities |
| `SIMILARITY_CONFLICT_THRESHOLD` | 0.85 | Embedding model changes |
| `DECAY_COMPRESS_DAYS` | 30 | Usage patterns establish natural access cadence |
| `DECAY_GONE_DAYS` | 90 | Same |
| `DECAY_REHYDRATION_EXEMPT_COUNT` | 3 | Summary quality measured |

**Source:** `memory-roadmap.md:272`, `lifecycle_service.py:27-43`

### Multi-tenant / cloud deployment
v1 is single-user local only. Multi-tenant requires per-user key envelopes, process isolation, TLS on Bolt, auth beyond static key, and fundamentally different architecture.

**Source:** `memory-roadmap.md`, M7 implementation plan (archived)

### `sync_edge_counts` scaling
Currently touches every Entity node in a single transaction. Fine for personal-scale graphs (hundreds to low thousands). Migrate to `CALL {} IN TRANSACTIONS` for batched writes at scale.

**Source:** `consolidation_queries.py:46-51`

---

## External Evaluation (Watch List)

Projects under evaluation for potential adoption or borrowing:
- **MCP Memoria** — alternative MCP memory approach
- **mem0 MCP** — lightweight memory layer
- **Hippocampus** — biologically-inspired memory model
- **GraphRAG** — strategy routing for hybrid retrieval
- **regent-vcs/re_gent** *(added 2026-05-06)* — "Git for AI agent activity." Go CLI + VSCode extension. DAG of Steps (one per tool call) stored in `.regent/` alongside `.git/`. BLAKE3 content-addressed, SQLite index. Three primitives: `rgt log`, `rgt blame` (per-line prompt attribution), `rgt rewind` (time-travel). Hook-driven, zero manual commits. Validates our "Conversational git" post-v1 item. Their `rgt blame` primitive has no analog in our system — see memory-backlog.md for the graph-native design. Skip adopting; borrow the line-attribution idea.
- **zhangfengcdt/memoir** *(added 2026-05-06)* — Git-style versioning (branch/commit/merge/rollback) applied to memory rather than code. Semantic path taxonomy (`profile.professional.skills.python`) instead of UUIDs. O(log n) hierarchical lookup + LLM-semantic dual search. Claude Code plugin with session-lifecycle hooks. Alpha v0.1.9; no conflict governance, no lifecycle decay, no graph. Validates semantic-path browsability idea. See memory-backlog.md for `canonical_path` entity property proposal derived from this.

**Source:** `memory-backlog.md:37-40`

---

## Live Verification (Run Before Declaring v1 Shipped)

These require the live Neo4j + llama.cpp stack:

- [ ] `python smoke_test.py` passes
- [ ] `python integration_test.py` passes
- [ ] `pytest --run-online -m online` passes (M0 baseline 7/10, sidecar check, existing live tests)
- [ ] `get_memory_stats` health check: queue depth < 20, no OPEN breakers, recall p95 < 5s
- [ ] Enrichment SLO spot-check: 95% of episodes READY within 120s (manual SQL query in ops runbook)
