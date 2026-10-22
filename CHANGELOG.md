## 2026-10-21 - Lazy scalar and event schema activation survives concurrent first use

In the LME pilot, 3 of 30 concurrent first `POST /api/phase3/run` calls on a fresh graph returned 500 with `Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists`. Neo4j 5 raises it when two transactions create the same rule at once, even with `IF NOT EXISTS`.

- `infrastructure/schema_ddl.py` (new):
  - `execute_schema_statement` treats only `EquivalentSchemaRuleAlreadyExists` as success. Name or schema conflicts and every other error still raise.
  - `await_schema_online` waits, bounded at 60 s per rule, for the named rules to come ONLINE. A timeout propagates.
- `infrastructure/typed_assertion_reconciliation.py` (`activate_scalar_state`), `infrastructure/typed_event_repository.py` (`activate`):
  - Run their DDL through the helper.
  - Then wait only for the rules they created. Otherwise a caller that lost the race could see the winner's index still POPULATING and fail the scalar ONLINE check.
  - Unrelated indexes are not awaited.
- Tests: new `tests/infrastructure/test_lazy_schema_activation_race.py`.
  - Offline: the fake-driver race, the codes that are re-raised, and the await order and timeout.
  - Online: 16 concurrent `ensure_scalar_state_activated` plus 16 event activations on a fresh schema, 3 rounds. It fails without the fix.
- Changed test: `test_activate_runs_idempotent_ddl` in `tests/test_typed_event_repository.py` now separates the `CALL db.awaitIndex` waits from the DDL statements it checks.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - POST /api/phase3/run reports lanes that failed

In the LME pilot a counting namespace failed inside the pass (a provider 500), yet the request returned 200 with nothing marking it failed, so the bench recorded it as done.

- `api/routes_support.py`, `api/routes_handlers.py`: `Phase3RunResponse` gains `ok` (false when any lane failed closed for the namespace) and `failed_lanes` (lane, namespace, failed count). The status stays 200 and the existing counters are unchanged.
- `services/lane_outcomes.py`: new `failed_lanes()` reads each lane's failed counter; the scalar lane now has one.
- `services/scalar_consolidation.py`: when scalar-state activation is refused, `scalar_namespaces_failed` counts the scalar targets. Before, the lane was skipped with only a log warning, the same way the event lane already reports its activation refusal.
- Tests: new `tests/test_phase3_run_lane_failure.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - E2E work root is always absolute; dev group installs build

- `tests/e2e/conftest.py`, `tests/e2e/_harness/config.py`: `MENHIR_E2E_WORK_ROOT` is resolved to an absolute path. A relative root broke E2E-20's upgrade step, where pip runs from another cwd and could not find the candidate wheel.
- `pyproject.toml`, `uv.lock`: the `dev` group adds `build` (the wheel frontend E2E-19/20 use; CI already installs it), so `uv sync` is enough for a local E2E run. The relock also adds platform markers to `secretstorage`'s dependencies.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - Default the inferred self tier to enforce

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: `inferred_self_mode` defaults to `enforce` (owner decision for #343). Installs that leave it unset, fresh or upgraded, bind what agent-written memories say about the user to the inferred self (`user-inferred`) instead of a new "user" node, and recall reads both self tiers; pin `off` to opt out. An unrecognized value still falls back to `off`.
- `cli/bootstrap.py`: the hook CLI's `RecallService` now receives the mode, so hook-injected context reads the same tiers as backend recall (it ignored the inferred tier before).
- Existing "user" forks created before the upgrade stay where they are; `scripts/report_self_forks.py` lists them and consolidation stays blocked.
- E2E-19 runs `two_tier_self` on the shipped defaults; its opt-out step also pins `MENHIR_INFERRED_SELF_MODE=off`.
- `.agent/default-off-features.md`: the mode moves to a "graduated to default-on" section.
- Tests: new upgrade test in `tests/test_release_config_upgrade.py` for unset versus pinned modes. Changed: `tests/e2e/test_e2e_19_fresh_install.py` (default-on expectations and opt-out pin).
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-13 - Assess Anthropic Scion as prior art for revision intelligence

- `docs/research/prior-art/anthropic-scion-source-control-comparison.md`: dated comparison of Anthropic's proprietary Scion, its open-source status, Git-compatible alternatives, Menhir overlap and risks; no VCS replacement proposed.
- `.agent/plans/backlog/menhir-scion-exploration-2026-10-13.md`: proposed gated inventory, benchmark and limited-spike plan for Git revision metadata and content-addressed sync, with proof obligations and stop conditions.
- `docs/research/prior-art/README.md`, `docs/research/README.md`, `.agent/plans/backlog/README.md`: route the research and plan in canonical indexes.
- `CHANGELOG-archive.md`: retain the oldest dated current entry.

## 2026-09-24 - explicit staging is a Beacon guardrail

The rule was only in `.agent/maintenance.md` and the startup list, so agents working from Beacon
missed it: in the Beacon agent-task evaluation, two plans for archiving a Menhir plan proposed
`git add -A -- <paths>`.

- `AGENTS.md`: a new `<!-- beacon:guardrail id=explicit-staging severity=medium -->` bullet, "Stage
  files explicitly by path; never `git add .` or `git add -A`. Leave unrelated changes unstaged.",
  after the existing implementation rules (no existing line moves). Checked locally: `beacon build`
  serves it as the guardrail `explicit-staging`.
- `CHANGELOG-archive.md`: the oldest 2026-09-17 entry moved there (10-entry limit).

## 2026-10-14 - Scan release archives from private output directories

- `deploy/build_release_image.py`: bind only the public image archive into each scanner, keeping private parent directories and sibling artifacts inaccessible.
- `tests/test_build_release_image.py`: require the archive-only read-only bind for both Syft and Grype.
- `.agent/reviews/menhir-release-container-audit-2026-10-14.md`, `.agent/plans/menhir-168-phase0-baseline-contract.md`: record the real unpublished image/fork checks, scanner reproduction, upstream-base scan refusal and remaining #168/#123 gates. Runtime defaults are unchanged.

## 2026-10-21 - Make OAuth JWKS-fetch 503s diagnosable

- Ported from 0847c6e on `refactor/file-size-refactor`. Diagnostics only; no auth outcome changes.
- `api/oauth.py`: a failed JWKS fetch logs `OAuth JWKS fetch failed` with the failure kind, the message (JWKS URI redacted to scheme://host/path), elapsed seconds, timeout, `force_refresh` and whether keys were cached; a payload with no `keys` list logs as `MalformedJWKS`. Process-wide `JWKS_REFRESH_STATS` counts failures.
- `api/auth.py`: an OAuth `server_error` 503 logs the request id the client receives, the path, the detail and the cause's type.
- `api/production_routes.py`: `/readyz` adds an `oauth_jwks` block (`refresh_failures`, `last_failure_at`, `last_failure_kind`), per process and never affecting readiness.
- `docs/runbooks/oauth-remote-mcp-checklist.md`: failure-mode row for the 503. The original `deploy/RUNBOOK.md` section was deployment-specific, and that file no longer lives in this repo.
- Tests: new `tests/test_oauth_jwks_diagnostics.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - Split 27 oversized modules that main has not changed since the file-size branch

- Ports part of `refactor/file-size-refactor` (2802ebb): only the 27 originals whose pre-split text is identical to main's. Each keeps its name and re-exports what moved, so no import site changes; moved code lives in 87 new sibling modules. 16 `src/menhir` modules (`api/oauth_authorize`, `cli`, `core/runtime_preflight`, `domain/{artifact_reconciliation,projection_coverage,scalar_dependency_evidence}`, `explorer/{extraction_lab,recall_packet_prototype}`, `infrastructure/{evidence_publication_intents,graph_operations,projection_lifecycle_repository}`, `infrastructure/telemetry/{event_store,lifecycle_store,recall_store}`, `snapshot/{protocol,receive}`), 6 services (`correlation_service`, `deterministic_scalar_extractor`, `metric_write_coordinator`, `research_scalar_isolated_adapter`, `saga_reconcile_dispatcher`, `structural_scalar_composer`) and 5 scripts.
- Every top-level definition of main's originals appears unchanged (AST-equal) in the split, except class bodies that became mixin bases (each method unchanged), `__all__` additions, and lookups routed through the original module so existing monkeypatch seams still apply. Extracted Cypher constants equal the original inline queries.
- Fixes two defects in the branch's split: `EvidencePublicationIntentRepository` regains `@dataclass(frozen=True)` (its constructor was lost), and `telemetry/recall_store.py` keeps its module `logger`.
- Not ported: the 75 other split targets (changed on main since, or no longer present); they need a fresh split.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-21 - Two-tier self identity: an inferred user tier behind MENHIR_INFERRED_SELF_MODE

- New setting `MENHIR_INFERRED_SELF_MODE` (`off` default, `observe`, `enforce`; unrecognized falls back to `off`). With `enforce`, an episode that cannot reach the anchored speaker marker (agent-written memories, agent-quoted user statements) is offered a Menhir-allocated `MenhirInferredUser_` marker, and what the model attaches to it lands on one inferred self per namespace (`user-inferred`, `entity_role=inferred_self`, `self_tier=inferred`, never `is_self`) instead of a new ordinary "user" node (#343). `observe` records what would have been offered and binds nothing.
- `domain/self_identity.py`, `infrastructure/self_binding.py`, `infrastructure/graphiti_extraction_policy.py`: inferred uuid, envelope and offer gate; `bind_inferred_self` with full rollback; one marker per prompt, never alongside the anchored marker; foreign or duplicate markers are quarantined; a marker prefix already present in the episode text withdraws the offer. Under `enforce`, an `agent_quoted` evidence projection no longer binds to the anchored self (with the mode `off` that path is unchanged).
- `infrastructure/graphiti_resolution_policy.py`, `infrastructure/correlation_queries.py`, `domain/merge_eligibility.py`, `infrastructure/episode_lifecycle.py`: the inferred node is never a dedup candidate, never merges in either direction, and is not reported as a self fork. `link_self_tiers` draws a fact-less `INFERRED_VIEW_OF` edge from the anchored self to the inferred self.
- Recall (`services/recall_pipeline.py`, `services/recall_policies.py`, `mcp/formatters.py`): with the mode on, first-person queries admit the inferred self to the View gate (event-history authority stays anchored-only), self entities carry `self_tier`, and an inferred fact whose near-duplicate is visible on the anchored self is dropped from that result, with the anchored fact marked `corroborated`.
- Ingest (`services/ingest_worker.py`, `services/ingest_queue.py`, `infrastructure/episode_lifecycle.py`, `infrastructure/schema.py`): under `enforce`, a client episode whose text is exactly its single admitted turn's text finishes READY without extraction while that turn's projection is not FAILED (`extraction_skipped_for`); if the projection fails or is deleted, lease recovery (on every pass, whatever the mode) and the projection's own failure path return the client to PENDING, once (`verbatim_skip_revoked`). New index `episodic_extraction_skipped_for_idx`.
- `scripts/report_self_forks.py`: read-only per-fork review report (facts, source episodes) and an approval manifest template, all unapproved. Folding approved forks into the inferred tier stays blocked until a journaled consolidation tool exists.
- E2E-19 gains `two_tier_self` on a backend opted into `enforce`; the deterministic provider answers the inferred marker for one agent-written fixture sentence.
- `.agent/default-off-features.md`: lists the new mode.
- Tests: new `tests/test_inferred_self_tier.py`, `tests/test_inferred_self_tier_live.py`, `tests/test_inferred_self_recall.py`, `tests/test_verbatim_skip_worker.py`, `tests/test_verbatim_skip_live.py`, `tests/test_report_self_forks_live.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
