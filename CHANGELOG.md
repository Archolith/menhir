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

## 2026-10-05 - Apply configured Warden profiles to context recall

- `tests/test_consumer_session_e2e.py`: `_ingest_project` takes a graph-issued structure scan generation before the walk, as `scan_and_write_project` does, so the `needs_llm` session test no longer fails on the write fence with `StaleStructureScan`. Test-only.
- `infrastructure/memory_queries.py`: `timeline_anchor` accepts a receipt (queue-node) uuid and resolves it to its Graphiti episode only through a visible, non-FAILED queue node in the caller's namespace; Graphiti uuids still anchor directly. Query mode and `around=<receipt>` no longer fail with "unknown or hidden memory" for live memories.
- `services/timeline_service.py`: the query-mode seed search applies the current embedding-model filter (#220), so stale-model vectors cannot pick the anchor.
- `tests/test_recall_timeline_receipt_anchor.py`, `tests/test_recall_timeline.py`: disposable-graph coverage for receipt/episode anchors, hidden/FAILED/pending receipts, cross-silo receipts, subject threads and the model filter; offline query-shape checks.

- `config/feature_flags.py`: add a 98-entry metadata inventory for boolean/mode settings, legacy settings readers and environment controls, with aliases, scoped prerequisites, observation markers and version requirements. Enforce inventory/default/binding/documentation consistency with AST and mutation controls; backfill 38 commented template entries without changing runtime behavior or defaults.
- `.env.example`, `docs/post-install.md`, `.agent/default-off-features.md`: document bounded campaign controls, correct event-history variable names and explain upgrade precedence, explicit opt-outs and persisted-data limits; verify setup/template upgrades across 20 switches without changing defaults.
- `src/menhir/cli/setup.py`: fill missing or blank provider model/URL settings from existing defaults in wheel and checkout setup (#193), preserving custom values, secrets and explicit feature opt-outs; add idempotence regression coverage.
- `tests/test_warden_verifier_interactions.py`: validate Warden/verifier isolation and combination over changed, unchanged, unavailable and persisted-review sources with sync-disabled restart; report warnings, refusals and register evidence separately across 128 recall/context decisions. Reuse existing offline stores and document remaining qualification in the existing owner/index; production behavior/defaults unchanged.
- `domain/warden.py`, `services/assertion_pipeline.py`: pass recorded unresolved conflict groups to the optional contradiction guard; refuse current assertions and label historical recall without changing ranking or defaults. Expand real-producer tests and the offline matrix to 108 decisions, including resolved conflicts and unchanged retained scores; update the existing owner/index and option contract.
- `tests/test_warden_safety_matrix.py`: compare three Warden profiles across seven offline recall controls and independent optional guard runs; report harmful returns separately from warnings, useful refusals and unknown provenance in JUnit JSON. Document measured fixture limits in the existing verifier owner/index; no policy or default changes.
- `domain/recall.py`, `services/recall_service.py`, `services/recall_pipeline.py`, `services/recall_support.py`: distinguish configured versus applied Warden checks, pending bypasses and metadata failures; expose detailed receipts only on request.
- `core/backend_protocol.py`, `core/backend_runtime_data_ops.py`, `core/backend_client_ops.py`, `api/routes.py`, `api/routes_support.py`, `mcp/tools/recall/`: carry `include_warden_status=false` across canonical recall/context boundaries; preserve brief incomplete-check notices.
- `services/context_builder.py`, `explorer/recall_lab.py`, `explorer/static/recall-lab.js`: budget optional diagnostics and mandatory notices with context; show execution receipts in Recall Lab details.
- `tests/test_warden_execution_status.py`, `tests/test_api_routes.py`, `tests/test_backend_roundtrip.py`, `tests/test_recall_lab.py`: cover visibility, fallback/error/metadata paths, concurrency, context budgets and protocol forwarding; update owner/index and data/API/default-off docs.
- Verifier sync preserves last successful register evidence while recording separate probe attempts; incomplete or unconfirmed register links cannot claim a new success.
- Ranked recall, compact/full MCP, REST and context expose same-namespace register observations, successful verification times and latest probe statuses, without changing ranking or defaults.
- Freshness regression coverage includes restart, unavailable/failed probes, missing first success, duplicate links, atomic context budgets and disposable graph projections; update the existing #173 owner and reference docs.
- `src/menhir/infrastructure/verifier_repository.py`, `src/menhir/services/verifier_sync.py`: scope bindings/register writes and review traversals by namespace; reject invalid observations and avoid writes after a failed register read.
- `src/menhir/domain/recall.py`, `src/menhir/infrastructure/cypher.py`, `src/menhir/services/recall_pipeline.py`, `src/menhir/services/scoring_service.py`: carry existing verifier review flags through ranked recall without changing content or scores.
- `src/menhir/services/context_builder.py`, `src/menhir/mcp/formatters.py`, `src/menhir/api/routes.py`, `src/menhir/api/routes_support.py`: preserve review warnings in context, compact/full MCP and REST.
- `tests/test_verifier_currentness.py`, `tests/test_verifier_sync.py`, `tests/test_context_builder.py`, `tests/test_scoring_service.py`, `tests/test_api_routes.py`: add drift/restart, malformed-source, budget, protocol and disposable graph namespace controls.
- `tests/test_milestone_three_contract.py`: extend the strict optional-field contract and assert inert defaults for review metadata.
- `.agent/plans/backlog/graph-verifiers.md`, `.agent/plans/backlog/README.md`, `.agent/data_models.md`, `.agent/endpoints.md`, `.agent/default-off-features.md`: reconcile the stale unwired-consumer claim and document the bounded advisory contract and remaining qualification.

- `src/menhir/services/context_builder.py`, `src/menhir/core/bootstrap.py`, `src/menhir/cli/bootstrap.py`: pass the configured retrieval profile through runtime and direct-hook context recall, including Warden admission settings.
- `tests/test_context_builder.py`, `tests/test_services_pipeline.py`: cover strict and conversational profiles, superseded and unknown-support controls, disabled-gate compatibility, and both production constructors.
- `.agent/default-off-features.md`: explain strict versus conversational profiles and the limits of the scored-candidate gate; no defaults change.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-10-04 - Report Phase-3 lane completion for benchmark acceptance

- `src/menhir/api/routes_handlers.py`, `src/menhir/api/routes_support.py`: report both counter gates, scalar-only model calls, and whether evidence remains beyond the event watermark.
- `src/menhir/services/scheduler_tasks.py`: attribute scalar calls before the shared event phase.
- `tests/test_api_routes.py`, `tests/test_consolidate_personal_memory.py`: cover counter gates, event completion and partial processing, and scalar call attribution.
- `.agent/endpoints.md`: document the additive response fields.

## 2026-09-30 - Keep merge lineage out of Graphiti prompts

- `src/menhir/infrastructure/graphiti_llm_adapter.py`: the provider wrapper removes `merge_audit`, `merged_from` and `last_merge_op_id` from tagged JSON blocks before a Graphiti request is sent. The fork serializes entity attributes into its dedup and summary contexts, so the merge audit trail rode along and grew with every merge (64% of the dedup candidate block in AMA runs; one 131k-char NodeResolutions prompt). In a 60-call replay, dropping it matched the control consensus as often as a fresh control run and cut dedup input about 79%. Prompts without lineage are sent unchanged.
- `tests/infrastructure/test_merge_lineage_prompt_policy.py`: renders the real fork dedup and summary prompts with lineage-carrying nodes; only lineage is removed and the rest of the prompt is byte-identical.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Give verdict LLM calls room to answer on reasoning models

- `src/menhir/infrastructure/llm.py`: the identity judge, contradiction check and shadow tie-break use a shared `_VERDICT_MAX_TOKENS` (1024) instead of 64/64/128. On reasoning models `max_tokens` also covers hidden reasoning, so most identity-judge calls ended empty and became `None` votes that route merges to conflict (#203). `_chat_text` now warns when a completion comes back empty, naming the operation and budget.
- `tests/infrastructure/test_verdict_budget_203.py`: a simulated reasoning model that answers only above a reasoning budget, the empty-completion warning, and the unchanged fail-safe `None` for a real empty answer.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Read multi-word numbers whole in scalar counts and frequencies

- `src/menhir/services/typed_scalar_rules.py`: counts read thirteen to nineteen, the tens, and tens compounds ("twenty-five", "twenty five") as one number, so "twenty-five postcards" is 25, not 5. A count, range bound or frequency count joined to another number word, digit or scale word ("two hundred", "2 thousand") now gives no value instead of a piece of the number, and a frequency with a multi-word number before "every" no longer defaults to a count of 1 (#200).
- `tests/test_scalar_compound_number_words.py`: compound, scaled, range, delta and frequency cases, plus unchanged single-word readings.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
