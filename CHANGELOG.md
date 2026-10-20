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

## 2026-10-11 - Finish the speaker-role follow-ups from the #272 review

- `domain/turn_role.py`: turn evidence from another namespace no longer proves an episode's speaker; namespaces compare through `normalize_namespace`, as the claim query's `subject_endpoint_eligible` does (review F6).
- `mcp/tools/ingest/add_memory.py`, `mcp/tools/ingest/add_memory_and_track.py`, `services/ingest_intake.py`: MCP `add_memory` and `add_memory_and_track` take the same optional `role` as `POST /api/memory`, refused before any write when it is not an exact speaker; `ingest_episode` forwards `role` (review F11). TEMPORAL direct writes check it but do not use it (no extraction).
- `scripts/backfill_admitted_on.py`: the admission join links only captured user turns (never `agent_quoted`) to episodes whose declared `turn_role` is absent or `user`, on raw text or the legacy `'user: ' + t.text`, so a backfilled edge can never prove `user` for an episode declared otherwise (review R1/R2 of #273). `scripts/freeze_scalar_samples.py` and `scripts/probe_scalar_extraction.py` read user `:TurnEvidence` with the scheduler's query and no prefix strip; `scripts/measure_absence_band.py` and `scripts/replay_fold_flags.py` keep reading the legacy prefixed LME corpus their frozen captures index into, now labelled as such. The suburbs smokes send raw text with `role="user"` (review F9).
- Stale docstrings about the removed Episodic fallback and prefixed assistant turns are corrected (review F10).
- Docs: `.agent/endpoints.md` and `docs/agent-usage.md` list the MCP `role`.
- Tests: new `tests/mcp/test_add_memory_role.py` and `tests/test_backfill_admitted_on_live.py` (online); F6, F11 and legacy-strip cases added to `tests/test_turn_role_resolution.py`, `tests/test_turn_role_wiring.py`, `tests/test_turn_role_ingest.py` and `tests/test_freeze_scalar_samples.py`. Changed because they pinned changed behavior: `test_extraction_passes_proven_evidence_role` (now supplies the turn's namespace), `test_tracked_write_signature_does_not_grow_permissions_or_invent_options` (expects `role`), and in `test_freeze_scalar_samples.py` the `_Session` helper, `test_graph_turn_evidence_source_matches_scheduler_and_uses_exact_namespace`, `test_graph_missing_exact_namespace_fails_closed` and `test_graph_duplicate_namespace_selection_fails_closed`; `test_graph_legacy_source_matches_scheduler_and_uses_exact_namespace` is replaced by `test_graph_source_is_turn_evidence_only_and_keeps_raw_text`.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-11 - Take the speaker role from structured data, never from episode text

- `domain/turn_role.py`: `resolve_turn_role` takes an episode's speaker from its single `ADMITTED_ON` turn when the content equals that turn's text (surrounding whitespace ignored) and the turn is not `agent_quoted`, else from the declared `turn_role`, else `unknown` (#271).
- `api/routes_support.py`, `api/routes.py`, `core/backend_*.py`, `services/ingest_intake.py`, `infrastructure/memory_graph_adapter.py`, `infrastructure/episode_lifecycle.py`: optional `role` on `POST /api/memory`, persisted ON CREATE as `Episodic.turn_role` and returned by the claim query with the turn's `source_kind`; roles match exactly on every transport, and an invalid one is a 422 (400 through the bridge).
- `infrastructure/graphiti_extraction_policy.py`, `infrastructure/anchored_time.py`, `services/enrichment_steps.py`: the anchored-time user gate, the assistant echo/self-label policy and self-like endpoint retention read the receipt's `turn_role`; `_episode_role`, `strip_user_prefix` and the titled-list prefix split are removed, and a titled list whose title is a speaker or self label is refused.
- `infrastructure/memory_graph_adapter.py`, `infrastructure/personal_memory_queries.py`, `services/scheduler_tasks.py`, `services/correction_resolver.py`: personal-memory consolidation reads `:TurnEvidence` only; the `user:`-prefixed Episodic fallback and its two strips are removed.
- Not compatible with graphs whose episodes carry their role only as a text prefix: those episodes now resolve to `unknown`. Rebuild them, or send `role` for extraction; counter consolidation needs user `:TurnEvidence` and ignores `role`.
- Tests: new `tests/test_turn_role_*.py`; prefix-encoding fixtures updated to structured roles in the anchored-time, closure, high-wave1, titled-list, cf193 and turn-evidence tests; `test_strip_user_prefix_removes_only_the_leading_role` removed with its helper.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
