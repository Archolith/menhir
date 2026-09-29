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

## 2026-09-28 - Restore default-silo scalar owner lookup

- `src/menhir/infrastructure/episode_lifecycle.py`: use the shared tenant-scope
  predicate for exact fallback entity lookup, including both persisted default
  namespace spellings while keeping named tenants scoped.
- `tests/test_episode_lifecycle.py`: cover default and named namespace query
  parameters and a disposable-graph regression for current, legacy, named,
  and derived-View entities.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Schedule scalar-only personal memory

- `src/menhir/core/runtime.py`, `src/menhir/services/maintenance_scheduler.py`:
  create the chat dependency and register the shared background job when scalar
  state alone is enabled, while keeping counter and event lanes independent.
- `tests/test_settings_event_history_runtime.py`, `tests/test_api_routes.py`:
  cover all eight lane combinations, the unavailable-provider case, and scalar
  parity with API-triggered consolidation.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Ground scalar clock times with dotted meridiems

- `src/menhir/services/typed_scalar_rules.py`: normalize dotted AM/PM source
  times correctly and refuse unsupported suffixes instead of accepting a bare
  time prefix that could overwrite a correct model value.
- `tests/test_typed_scalar_perception.py`: cover source spelling, noon and
  midnight, malformed times, and full-row grounding.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Refuse ambiguous scalar subject fallback

- `src/menhir/services/typed_scalar_rules.py`: distinguish absent, ambiguous,
  invalid, and unique subject matches so a conflicting local owner cannot fall
  through to a looser spelling or namespace lookup.
- `tests/test_typed_scalar_self_binding.py`,
  `tests/test_typed_scalar_bind_persist.py`: cover exact and variant ambiguity,
  blank UUIDs, both namespace forms, persisted advisories, and repair.
- `CHANGELOG-archive.md`: retain the oldest former current entry.

## 2026-09-28 - Reconcile MVP preflight evidence before RC freeze

- `.agent/plans/menhir-local-stdio-mvp-release-2026-09-16.md`: record the completed issue inventory, five-item Oracle harness preflight, and merged Opus fixes while keeping exact-RC gates open.
- `CHANGELOG-archive.md`: retain the oldest former current entry.
