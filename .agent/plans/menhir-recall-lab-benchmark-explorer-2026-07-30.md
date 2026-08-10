# Bench-Run Explorer for Recall Lab

## Why
- LME benchmark runs produce manifest files and checkpoint scores in `archolith-bench/results/`, but browsing them requires the standalone `:8200` dashboard or direct filesystem access.
- Operators reviewing a run's task-by-task accuracy, evidence, scalar views, and derivation path must switch between the standalone dashboard and the Menhir Recall Lab.
- A unified view inside Menhir Recall Lab eliminates context-switching.

## Scope
- **In scope:** Bounded filesystem catalog (`BenchRunCatalog`) that reads LME run manifests and checkpoints from `MENHIR_BENCH_RESULTS_ROOT` (required; fail closed if absent); read-only task projection (`BenchRunTaskReader`); routes under `/explorer/recall-lab/bench-runs/` (3 HTML + 3 JSON); privacy redaction; source badges (LIVE BENCHMARK GRAPH / ACTIVE CONFIGURED / ARTIFACT-ONLY); contract name `bench-inspection/v1`; telemetry audit for fold outcomes; full task inspection including TurnEvidence/assertions with fold badges/state/history/scalar views/history entries/memory inventory/derivation classification (absolute/delta/mixed); DI support.
- **Out of scope for this pass:** Writing new Recall Lab experiments from benchmark tasks (prefill links only); graph writes; importing `archolith_bench` runtime modules; second Neo4j driver/URI.

## Proposed Design

### Architecture
- Menhir owns the integrated UI, API, and auth/privacy behavior under the existing `explorer` module. Bench owns execution, fixtures, checkpoints/scores, provenance, and artifacts.
- `menhir/explorer/bench_runs.py` provides:
  - `BenchRunCatalog`: reads manifest/checkpoint/provenance files from `MENHIR_BENCH_RESULTS_ROOT`. Run discovery handles one-level (`results/<run>`) and two-level (`results/<group>/<run>`) nesting. All symlinks are rejected. Duplicate run IDs are excluded as ambiguous.
  - `BenchRunTaskReader`: combines catalog data with optional live graph queries through `request.app.state.repo` (shared Neo4j pool). For the active run, queries TurnEvidence, TypedAssertions, scalar_state/history views, relationship facts, and telemetry audit for fold outcomes.
  - Path validation via `_safe_resolve`: every intermediate component is checked for symlinks before continuing, and the final resolved path must be within the root.
  - Privacy: recursive `_redact_nested` covers all known text-carrying keys at any nesting depth.
  - Config: `MENHIR_BENCH_RESULTS_ROOT` is required. When absent, the catalog reports empty results with `is_configured=False`. The provider is lazily cached once.
- Routes in `create_explorer_router()`:
  - `GET /explorer/recall-lab/bench-runs` — HTML listing
  - `GET /explorer/recall-lab/bench-runs/{run_id}` — HTML run detail
  - `GET /explorer/recall-lab/bench-runs/{run_id}/tasks/{namespace}` — HTML task detail
  - `GET /explorer/api/recall-lab/bench-runs` — JSON listing
  - `GET /explorer/api/recall-lab/bench-runs/{run_id}` — JSON run detail
  - `GET /explorer/api/recall-lab/bench-runs/{run_id}/tasks/{namespace}` — JSON task detail
- Task detail shows: question/reference answer, per-arm checkpoint scores, live TurnEvidence rows, TypedAssertions with fold outcome badges (current/historical/abstained/expired/not_folded/write_failed/not_materialized), source quotes mapped from evidence, scalar_state views, scalar_history entries/op_counts, relationship facts, and memory inventory with derivation classification.
- Badge logic: `LIVE BENCHMARK GRAPH` only after successful graph query returns non-empty content. `ACTIVE CONFIGURED` for active runs without successful query. `ARTIFACT-ONLY` for non-active runs.

### Provenance
- `run_provenance.json` identity merges direct `latest_attempt` identity fields (menhir_commit, bench_commit, variant, arm, etc.) over top-level `identity`.
- Run exposes: variant, arm, model, commits, attempt_count, noncanonical, resumed, phases (name/status/completed_at), harness_exit.

### Audit fold outcomes
- Ported from `archolith_bench/scalar_viewer.py` (`_read_audit`, `annotate_assertion_fold_outcomes`). Reads `lifecycle_events` telemetry where `phase='consolidation_audit'`, filters by namespace, selects the matching pass by assertion_id/source_key, annotates each assertion with fold outcome (state + history) and source quote from evidence mapping.

## Alternatives Considered
- **Second DB connection:** Rejected. Live task detail uses `request.app.state.repo`.
- **Importing `archolith_bench` runtime:** Rejected. Query logic is ported, not imported.
- **Browser submitting filesystem paths:** Rejected. Server resolves from allowlisted root only.
- **Silent graph fallback:** Rejected. Non-active runs are artifact-only; active runs with empty/failed queries show distinct warnings.

## Risks
- Results root directory structure may vary. Two-level + one-level discovery covers known patterns.
- Telemetry DB may be absent; audit folds gracefully return no matching events.
- Standalone `:8200` explorer remains untouched; its docs mark Menhir as the owner.

## Invariants
- `bench-inspection/v1` returned in every API response.
- No Neo4j writes from bench_runs.py.
- All symlinks rejected (not just outside-root).
- Duplicate run IDs excluded as ambiguous.
- Privacy redaction uses the same `_reveal` path as the rest of the explorer.
- `MENHIR_BENCH_RESULTS_ROOT` must be explicitly configured; no fallback.

## Validation
- Unit tests for catalog, path safety (symlinks, traversal, dupes), checkpoint parsing, privacy redaction, provenance.
- Route tests with TestClient for all 6 HTML/JSON routes, hidden/reveal behavior, 404.
- Fake shared-repo tests for live query: non-empty, empty active, exception, fold outcomes, source quotes, memory inventory.

## Docs To Update
- `menhir/.agent/endpoints.md` — new routes; document fail-closed config
- `menhir/.agent/CHANGELOG.md` — entry for this feature
- `menhir/.agent/architecture.md` — mention bench-runs provider under explorer
- `archolith-bench/.agent/CHANGELOG.md`, `architecture.md` — transitional :8200, launch env vars
- `archolith-bench/scripts/longmemeval/README.md` — Menhir RL integration
