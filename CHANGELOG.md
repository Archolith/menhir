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

## 2026-10-21 - Fresh-install and checkout-config E2E lanes; state-dir .env fallback

- `env_file.py`: `resolve_env_file` falls back to `state_dir()/.env` after `ENV_FILE` and `./.env`, so a wheel install configured by `menhir setup` reads its settings from any cwd (`menhir serve` and the stdio bridge included). `docs/post-install.md` states the order.
- `tests/e2e/_harness/features.py`: every flag default comes from `menhir.config.feature_flags.FEATURES` by env var, and the Warden gate, contradiction interrupt and source memories are registered. `mvp_default` now matches shipped defaults, so explorer, the experience counter and detailed revisions run on there.
- New lane E2E-19 (`tests/e2e/test_e2e_19_fresh_install.py`, harness `tests/e2e/_harness/fresh_install.py`, #168 item 59): a fresh wheel install on shipped defaults (temp HOME, no `ENV_FILE`, no benchmark mode or flags) through setup, `menhir up`, the README client, ingest, Warden-gated recall, correction, restart, opt-out and restore, and canonical-self binding: an admitted first-person user turn binds to the canonical self node under the default `enforce` (fact rewritten to `user`, no speaker marker left) and does not under `off`. The deterministic provider (`tests/e2e/_harness/providers.py`) answers the speaker marker for that fixture sentence only; `MENHIR_E2E_CHAT_BASE_URL`/`MENHIR_E2E_CHAT_MODEL` send chat to a real model for a manual pass.
- New lane E2E-20 (`tests/e2e/test_e2e_20_checkout_config.py`, #168 item 60): `setup --repo` copies `.env.example` with every flag commented, the template's documented defaults equal the code's, checkout and state-dir configs resolve to the same settings, and an upgrade from the v0.2.3 release wheel (downloaded, digest pinned) keeps the `.env` byte-identical through a setup rerun, apart from setup filling blank local URL/model keys with their defaults, while unset flags take the new defaults and pinned ones hold.
- `.env.example`: `MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED` gets its own commented default (`false`). Its only mention was wrapped prose that read as a commented `=true.` default, which E2E-20 caught.
- `tests/e2e/_harness/ci_evidence_gate.py`: both lanes are required.
- Tests: new `tests/test_env_file_state_dir_fallback.py`. Changed: `test_resolve_env_file_prefers_ENV_FILE_then_cwd` in `tests/test_env_file_and_neo4j_probe.py` pins an empty `MENHIR_STATE_DIR` so a developer's `~/.menhir/.env` cannot satisfy its None case.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-20 - Turn the Warden gate and its contradiction guard on by default

- `config/settings_model.py`, `config/feature_flags.py`, `domain/retrieval_tuning.py`, `.env.example`: `frontier_warden_gate` and `frontier_contradiction_interrupt` default true (#173). Installs that leave them unset, fresh or upgraded, get scope and Oracle admission and the contradiction interrupt on recall; set either to false to opt out.
- The belief gate stays opt-in. It marks an Entity superseded when any of its fact edges expired; with git, test or log evidence that scores as a conflict, and the contradiction guard then refuses the entity under the current lens, including for history questions the Warden's lens reads as current. Follow-up: #350.
- The evidence anchor stays opt-in, and `RetrievalTuningConfig.enable_evidence_anchor` now defaults false to match the setting (it was true). It reads provenance from the client label, so it refuses ordinary conversation; reimplementation is #349.
- `explorer/recall_lab.py`: `RecallLabTuning` follows the new defaults; arms D-H pin their guards explicitly so their meaning does not change (D stays the strict profile with the anchor on).
- Verifier sync is not a Warden and stays opt-in.
- `.agent/default-off-features.md`: the two settings move to a "graduated to default-on" section; the belief-gate row records why it stays opt-in; the evidence-anchor row cites #349.
- Tests: `tests/test_settings.py`, `tests/test_release_config_upgrade.py` and `tests/test_recall_service.py` assert the new defaults; new tests that each Warden default opts out through its env var, and that default recall keeps a git-backed entity with one expired fact edge (the belief gate refuses it). Tests that relied on the old defaults now pin them explicitly: `tests/test_recall_service.py`, `tests/test_warden_execution_status.py` (which also checks that default recall is `applied` without the anchor), `tests/test_warden_verifier_interactions.py` and `tests/test_git_staleness_recall_e2e.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-20 - Default canonical-self binding to enforce

- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: `canonical_self_binding_mode` defaults to `enforce` (owner decision for #169). Installs that leave it unset, fresh or upgraded, now bind declared user subjects to the canonical self; pin `off` to opt out. An unrecognized value still falls back to `off`.
- Existing same-named "user" forks are left in place and logged as `SELF_FORKS_REQUIRE_MIGRATION`; there is still no consolidation tool. Turning `enforce` back to `off` is safe while no consolidation has run.
- Known cost: agent-written memories that repeat user facts store them on a separate "user" entity, so recall can return both (#343).
- `.agent/workflows/canonical-self-migration-runbook.md`, `.agent/default-off-features.md`: record the new default; the runbook's activation canary applies to the first deploy that picks it up.
- Tests: `tests/test_self_binding.py` and `tests/test_release_config_upgrade.py` assert the new default; new upgrade test for unset versus pinned modes.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-20 - Turn Oracle ranking and the intent lens on and remove their flags

- `domain/retrieval_tuning.py`: `enable_oracle_ranking` and `enable_intent_lens` default true. They stay per-request knobs for Recall Lab comparisons; there is no deployment flag.
- `config/settings_model.py`, `config/feature_flags.py`, `.env.example`: remove `frontier_oracle_ranking` / `MENHIR_FRONTIER_ORACLE_RANKING` and `frontier_intent_lens` / `MENHIR_FRONTIER_INTENT_LENS`; both are listed in `RETIRED`. A deployment that still sets them gets the new default.
- `services/assertion_pipeline.py`: score ties keep the caller's order instead of candidate-id order, so an oracle outage (every oracle neutral) leaves recall order unchanged.
- `services/recall_pipeline.py`: the frontier only writes the Warden receipt when a guard is configured, so default recall still reports `disabled` with no notice. With only the contradiction or currentness guard configured, the receipt now reads `computed_not_applied` (the chain runs because Oracle ranking does).
- `explorer/recall_lab.py`: the Production arm follows the new default; the facet-only arm turns Oracle ranking and the intent lens off explicitly.
- Tests: `tests/test_settings.py`, `tests/test_release_config_upgrade.py`, `tests/test_warden_safety_matrix.py`, `tests/test_recall_service.py` and `tests/e2e/_harness/features.py` updated for the new default; new tests for default recall's Warden receipt and for tie order.
- Evidence and follow-ups: #172; remaining AMA checks in #345; diversity gate stays opt-in (#344).
- `CHANGELOG-archive.md`: retain the oldest former current entry.
