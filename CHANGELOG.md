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

## 2026-09-29 - Default the scalar agreement gate to lenient

- `src/menhir/config/settings_model.py`: `personal_memory_scalar_threshold` defaults to `2/3` and attribute/scope/subject reconciliation to on; scalar state itself stays off.
- `src/menhir/services/maintenance_scheduler.py`, `scheduler_tasks.py`, `scalar_consolidation.py`, `typed_scalar_service.py`: align the same knobs' fallback defaults.
- `.env.example`: document the new defaults and that the gain is not yet re-measured on current Menhir.
- `tests/test_scalar_lenient_defaults.py`: defaults, overrides and the k=3 two-vote commit; `tests/test_scalar_threshold_setting.py`, `tests/test_gate_relaxations.py`: update the pinned old defaults.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Read scalar input only from TurnEvidence

- `src/menhir/infrastructure/memory_graph_adapter.py`, `src/menhir/infrastructure/personal_memory_queries.py`: the typed-scalar lane always discovers and loads work from `:TurnEvidence`; the legacy `user:`-prefix Episodic selectors are removed (#95). The shared scalar cursor writer stays.
- `src/menhir/services/scalar_consolidation.py`: warn once per process when scalar consolidation runs with no user-role evidence, naming `add_memory(user_statement=...)` and TurnEvidence hooks.
- `tests/test_scalar_discovery_evidence_only.py`: evidence-only discovery and the warning; `tests/test_scalar_consolidation_cursor.py`: drop the two tests of the removed selectors.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Accept agent-quoted user statements for scalar perception

- `src/menhir/mcp/tools/ingest/add_memory.py`, `src/menhir/core/backend_*.py`, `src/menhir/api/routes.py`, `src/menhir/api/routes_support.py`: new optional `user_statement` (verbatim user words) on `add_memory`, exclusive with `turn_evidence_uuid`, carried through MCP, in-process and REST and sent only when set. `add_memory_and_track` keeps its MVP signature.
- `src/menhir/services/ingest_intake.py`: record it as `:TurnEvidence` with `source_kind='agent_quoted'` and link it provenance-only; failures never fail the ingest.
- `src/menhir/domain/truth/admission_gate.py`, `src/menhir/infrastructure/scalar_view_repository.py`, `src/menhir/infrastructure/turn_evidence_repository.py`: agent-quoted evidence never grants user tier, never counts as user foundation, and never feeds the counter or event lanes; the scalar lane keeps it.
- `tests/test_agent_quoted_evidence.py`, `tests/test_api_routes.py`: gate, foundation, lane, intake and REST coverage.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Require money and measurement quotes to state their value

- `src/menhir/services/typed_scalar_rules.py`: a model-supplied money or measurement value drops with `value_not_in_span` unless its grounded quote states it as digits (thousands separators, decimals, k/million scale) or English number words; the check runs last so earlier drop reasons are unchanged, and counts, durations, frequencies and clock times keep their span-derived values (#89).
- `tests/test_scalar_value_in_quote.py`: stated and missing money forms, measurement, scaled amounts, and unchanged count correction and multi-number abstention.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-29 - Treat equal Decimal amounts as one scalar value

- `src/menhir/domain/typed_assertion.py`: canonical numeric form for voting and identity strips insignificant Decimal scale and signs zero (10, 10.0, 10.00 -> "10"; -0.00 -> "0"), without float (#152).
- `src/menhir/infrastructure/typed_assertion_repair_repository.py`: stored `value_json` keeps the exact Decimal scale ("1200.50") instead of the canonical form.
- `tests/test_scalar_decimal_equality.py`: gate, assertion-key and normalization regressions; `tests/test_money_currency_canonicalization.py`, `tests/test_typed_assertion_repository.py`: update the pinned normalized strings.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
