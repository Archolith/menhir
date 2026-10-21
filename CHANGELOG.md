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

## 2026-10-18 - Keep scalar View text out of the shared keyword index

- Scalar Views (`scalar_state`, `scalar_history`) are `:Entity` nodes, so their text sat in Graphiti's `node_name_and_summary` full-text index. Lucene weighs terms across the whole index, so every View version shifted the keyword ranks of ordinary entities in every namespace. In the LME counting A/B the 4R count pass added 16 of them and pushed a summary entity from keyword rank 5 to 7, below the 0.15 similarity floor, and the answer went from 5 to 4.
- `infrastructure/view_models.py`: new `ViewKind.keyword_indexed` (default true); false for `ScalarStateKind` and `ScalarHistoryKind`.
- `infrastructure/view_write_repository.py`: for those kinds `record()` writes the surface into `view_name` / `view_summary` and leaves `name` / `summary` empty, on create and on the authoritative refresh. `content` keeps the summary (it is not indexed). Other kinds are written as before.
- Readers return the moved text: `MEMORY_RETURN_FIELDS` and `ENTITY_METADATA_FIELDS` (`infrastructure/cypher.py`), `fetch_current_scalar_view_for_slot` (the name the authority lane renders), `list_views`, and the explorer's live scalar-state query.
- `infrastructure/schema.py`: scalar activation moves indexed text on existing scalar Views (pre-change nodes, or an older writer during a rolling deploy) into `view_name` / `view_summary`. Idempotent; newer text wins.
- Recall no longer finds scalar Views through keyword search. In the A/B outputs it never had: they reached answers only through the authority lane. Each View still counts toward its namespace's `group_id` term, which shifts every score in that namespace equally and leaves ranks alone.
- Tests: new `tests/test_scalar_view_keyword_index_live.py` (online; fails on the old writer). `tests/test_scalar_state_view.py`: two assertions moved from `name` to `view_name`, because they pinned where the surface is stored.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-15 - Add a counting lane that records single happenings after the scalar phase

- New flag `personal_memory_counting_enabled` (`MENHIR_PERSONAL_MEMORY_COUNTING_ENABLED`), default off, requires scalar state. Plumbed through `config/feature_flags.py`, `config/settings_model.py`, `core/runtime.py`, `services/maintenance_scheduler.py`, `api/routes_handlers.py`, `api/routes_support.py` and `.env.example`.
- `services/counting_consolidation.py` (new): runs in `scheduler_tasks.consolidate_personal_memory` right after the scalar phase, sharing its call counter and budget, so counting goes without when the budget runs out. It stores `occurrence` claims only, with its own prompt and claim ordinal 1, so a counting claim never shares a head with a scalar claim on the same span. Each page's prompt lists the scalar claims already stored for those turns ("already captured"). Off: no calls, no counting watermark, no `counting_*` result keys or audit fields.
- `infrastructure/turn_evidence_repository.py` + adapter: own `:CountingConsolidationWatermark` cursor whose loader only returns turns at or before the scalar cursor for the scalar version in effect; no scalar cursor or another scalar version pauses counting. New bounded `scalar_claims_for_turns`. The label is added to every watermark wipe (namespace delete, merge/unmerge resets, scalar purge).
- `services/typed_scalar_rules.py`: lane parameters on `extract_typed_scalars_once` / `parse_scalar_row` (defaults keep the scalar lane unchanged); occurrence rows parsed with an importance; a planned, hypothetical or negated clause is dropped as `occurrence_not_happened`; the gate takes the majority importance among agreeing samples, ties to the higher level.
- `services/typed_scalar_service.py`: `perceive_and_persist` forwards the lane parameters. `typed_assertion_repair_repository.py` / `typed_scalar_persistence.py`: pending-binding repair keeps `importance`.
- `services/scalar_state_service.py`: a counted View (no anchor) now gets provenance edges: its occurrences as `CONTRIBUTED_TO`, absorbed ones as `SUPERSEDED_ANCHOR`. Stated Views are unchanged. Counted Views stay advisory in recall.
- Tests: new `tests/test_counting_extraction.py`, `tests/test_counting_consolidation.py`, `tests/test_counting_cursor_live.py` (online).
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-15 - Store single happenings as scalar occurrences and count them where no total was stated

- `domain/typed_assertion.py`: new operation `occurrence` (one happening on a `count` or `money` slot; value a positive integer or a positive amount) with an `importance` of `tracked`, `notable` or `routine`, kept out of every identity key. `OPERATIONS` is unchanged, so the scalar parser still drops an occurrence row; stored assertions validate against the new `ASSERTION_OPERATIONS`.
- `domain/scalar_state_fold.py`: a slot with no stated absolute folds its occurrences into a lower-bound value with `basis="counted"`. It materializes only when an applied occurrence is `tracked` or `notable` (else abstains `routine_only`). An occurrence from a turn that also gave the slot a delta is absorbed, so "three more times" is not counted twice. A slot with a stated absolute is unchanged: occurrences are neither contributors nor superseded anchors.
- `infrastructure/typed_assertion_models.py`, `typed_assertion_repair_repository.py`, `typed_assertion_write_repository.py`, `projection_coverage_repository.py`: persist and read `importance` (null for every non-occurrence, so existing nodes are written as before).
- `services/scalar_state_service.py`: a counted View's audit carries `scalar_basis`; stated Views' audit is unchanged.
- Nothing writes occurrences yet; the counting lane that will is a separate, flagged change (#170).
- Tests: new `tests/test_scalar_occurrence_fold.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-15 - Default the OpenAI chat model to gpt-6-luna

- `config/settings_model.py`: `openai_chat_model` defaults to `gpt-6-luna` (was `gpt-4o-mini`). It resolves to the existing gpt-6 profile (reasoning request shaping, extraction precision block, contained-edge pruning), the model the LongMemEval builds already run. Only OpenAI deployments with `OPENAI_CHAT_MODEL` unset or blank change; `chat_provider` still defaults to `local`.
- `cli/setup.py` reads that default, so `menhir setup --provider openai` now fills a blank chat model with `gpt-6-luna`; `.env.example` ships it too, with the Flex tier hint and the gpt-4o-mini floor. Existing `.env` values are never overwritten.
- Standard rates per 1M tokens: $0.10 in / $0.01 cached / $0.50 out (gpt-4o-mini: $0.15 / $0.075 / $0.60); Flex (`MENHIR_OPENAI_SERVICE_TIER=flex`) is half. It spends reasoning tokens, billed as output.
- Docs: `docs/post-install.md`, `docs/model-governance.md`.
- Tests: changed because they pinned the old default: the `openai` row of `test_provider_defaults_fill_blanks_without_overwriting_choices_or_opt_outs` and the openai branch of `test_provider_defaults_create_fresh_state_env` in `tests/test_cli_setup.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-15 - Say which kind of provider error a failed or retried LLM call hit

- `infrastructure/openai_rate_limit.py`: the 429 backoff warning names the kind (`flex_capacity` for Flex `resource_unavailable`, `rate_limit`, or `quota`) with the provider code and type, the retry hint and the request id. New `provider_error_code` builds a content-free label (`RateLimitError:429:resource_unavailable`, `APITimeoutError`), walking Graphiti's rewrap; provider fields are kept only in an identifier shape, and message text is never included. Neither helper raises.
- `infrastructure/observability.py`, `infrastructure/telemetry/recorders.py`, `infrastructure/telemetry/llm_usage_store.py`: a failed call's usage event carries that label into a new `llm_usage_events.error_code` column (added to existing sidecars on open); `error` stays redacted. The store drops a value outside the label shape. `fetch_llm_usage_summary` adds `failed_by_error_code`.
- `infrastructure/telemetry/erasure_inventory.py`: `error_code` is listed as non-content.
- Tests: new `tests/infrastructure/test_provider_error_detail.py`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-12 - Write an anchored time only when it dates the fact's own start

- `infrastructure/anchored_time.py`: new `state_start_guard`, run after `clause_guard`. A "since" phrase is temporal only with "ever since" or a perfect (have/has/had plus a participle, not "'ve got" or an -ed adjective before its noun) right after the subject of the one clause it attaches to: for a leading since-clause, the first clause after its comma that is not a side clause (", as I've said"), up to and/but/so/or/yet/plus or a dash; for a trailing "since", the last non-subordinate comma clause before it, from its last coordinator or subordinator. A temporal "since" dates only that clause: when the clause shares no word with the fact, the item keeps its basis and is not written (`state_start`); otherwise it means "because" and the item is dropped (guard `causal_since`), for every kind. A state takes a time only when the text marks its start (temporal "since", "for" + amount + unit running up to now, or start/begin in the expression's own coordinate clause, which shares a word with the fact; a fronted time takes the next clause only when it opens its comma clause, and a back-reference such as "it all started" is not enough); otherwise the item keeps its basis, so facts anchored on it still place, and `plan_overlay` does not write it (outcome `state_start`, checked before `graphiti_inside_window`). A state whose window reaches past the speech date is never written either (same outcome). Every whole-word occurrence of a repeated expression is judged, the strictest wins, up to 8 (past that, conservative); matching folds case and curly apostrophes without shifting positions. Sentence bounds are computed once per turn. Graphiti's own dates are unchanged. Accepted misses: coordinated objects ("I started yoga and meditation") or predicates before a trailing since ("more rested and focused since"), no-comma leading since-clauses, a comma right before a trailing since, "since March" with no perfect, a perfect after other words ("I think I've ..."), "it all started", states started this week, paraphrased facts.
- `domain/event_time.py`: a `state_start` row never renders the borrowed expression or a Graphiti date inside (or a day off) the borrowed window; it shows Graphiti's own date outside it, else "event time unknown".
- `infrastructure/graphiti_extraction_policy.py`, `scripts/eval_anchored_time.py`: run the guard on the production path and in the offline eval; `guard_drops` lists both guards' indices.
- Offline over the 22 frozen and gate runs: wrong writes 4 -> 0 (K04.3, G17.1, G28.0 x2) and no correct fact lost (writes 140 -> 138 after round 5: R26.0's "it all started" write, which Graphiti's date already scores correct); R29.2 is blocked on any speech date, not only within the year rule's 31 days. Chosen after seeing these runs, so not blind.
- Tests: new `tests/test_anchored_time_state_start.py`. Changed because they pinned changed behavior: the `heldout2:frozen` row of `PINNED_O2` in `tests/test_anchored_time_replay.py` (false dates 2/76 -> 0/76, overridden 11 -> 9, kept inside 13 -> 12) and the `heldout3:frozen` row (kept inside 16 -> 15).
- `CHANGELOG-archive.md`: retain the oldest former current entry.
