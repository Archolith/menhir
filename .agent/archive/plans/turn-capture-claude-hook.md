# Plan: Claude-first `:TurnEvidence` producer (MVP)

> **ARCHIVED 2026-07-11 (ctharvey-approved).** Fully shipped and live: `POST /api/turn-evidence`
> (`api/routes.py:503`), `:TurnEvidence` selective capture with `triage_reason`/`triage_version`,
> `turn_evidence_repository.py`, schema constraint `turn_evidence_key_unique` (`schema.py:115`),
> and hook producers `scripts/hooks/menhir_turn_evidence.py` (+ codex/opencode). Out-of-scope
> items (assistant/tool capture, retention/redaction, backfill) were explicit non-goals, not
> unfinished work. Archived per owner rule (a) fully implemented/shipped.

**Status: DONE 2026-07-07.** Implements ADR 0001 Option B's first producer.

**REVISED 2026-07-07 to SELECTIVE capture:** the hook observes every user prompt but stores only ones
that pass deterministic, LLM-free triage as `:TurnEvidence {role:'user'}` (renamed from `:Turn`).
Boring prompts are dropped. Endpoint is `POST /api/turn-evidence`; records carry `triage_reason[]` +
`triage_version`. Everything below that says `:Turn`/`/turns`/"every prompt" is superseded by the
selective design. NOT the full ecosystem (no assistant/tool capture, no retention/redaction engine,
no backfill, no full-transcript mode).

## Architecture
```
Claude Code UserPromptSubmit hook  (raw evidence producer; LLM gets no vote)
  -> POST /api/turns  (agent tier)
  -> :Turn {role:'user', declarant:'user', text, session_id, namespace, source_kind:'claude_code_hook', ...}
  -> Phase 3 prefers role=user Turns over the legacy `user:` prefix path
  -> perception -> folds -> Views (raw Turns never enter normal recall)
```

## Components (commit order)
1. **Schema** (`schema.py`): `_turn_index_queries()` — unique constraint on `turn_key` (idempotency)
   + indexes on namespace/role/recorded_at; added to `get_phase1_bootstrap_queries` (NOT to the
   readiness-gating required set).
2. **`infrastructure/turn_repository.py`**: `TurnRepository` — `record_turn` (MERGE on turn_key,
   set turn_id/recorded_at on create; metadata JSON-serialized), `list_dirty_turn_namespaces`,
   `load_user_turns`, `turns_exist`, `turn_stats`.
3. **Phase 3 consumption** (`memory_graph_adapter.py`): adapter owns a `TurnRepository`; its
   `list_dirty_namespaces` / `load_user_episodes` PREFER Turns when `turns_exist()`, else legacy
   `PersonalMemoryRepository`. `consolidate_personal_memory` prefix-strip made tolerant (Turn text has
   no `user:` prefix). Selection: `role='user' AND declarant='user' AND text non-empty`.
4. **Debug report** (`perception_report.py`): `probe_capture_metadata` adds `turn_capture` stats
   (table_exists, totals by role/source_kind, latest, phase3 user turns). Conclusion flips: if
   user_turns>0 it stops saying "no user evidence"; if 0 it says a producer is required.
5. **API** (`routes.py`): `POST /api/turns` (agent tier) -> `graph_adapter.record_turn` via
   `asyncio.to_thread`; 400 on missing role/text; namespace via body/header.
6. **Hook** (`scripts/hooks/menhir_record_turn.py`): stdlib-only adapter; reshapes Claude hook JSON
   -> POST /api/turns with the agent bearer; non-blocking (Menhir down => log + exit 0); silent
   stdout. Importable `build_turn_payload(hook_input)` for tests.
7. **Tests**: repository create/idempotency, role/text required, hook payload mapping, missing-prompt
   ignored, offline-non-blocking, Phase 3 selects user Turns / ignores assistant+tool, report turn
   stats, legacy Episodic not reclassified.

## Safety (ADR 0001 + handoff)
Local-only endpoint; agent-tier auth; opt-in via project-local `.claude/settings.local.json` (NOT
committed global config); raw Turns never in default recall; namespace-scoped; failure log avoids
dumping full prompt. Do not enable globally without explicit user intent.

## Acceptance
Simulate the exact Claude `UserPromptSubmit` JSON through the hook -> live `POST /api/turns` ->
verify `:Turn(role=user)` written -> Phase 3 dirty/select picks it -> stated measure
`watchlist_item_count=25` derivable. The only manual step left to the user is enabling the hook in
their Claude settings (a capture-consent decision).
