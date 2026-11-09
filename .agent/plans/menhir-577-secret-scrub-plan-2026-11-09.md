---
artifact_schema: 1
artifact_uuid: 9d3b7c52-4e18-4a6f-b0a1-7f2c8e5d3a64
artifact_type: plan
artifact_status: PROPOSED
---

# Secret scrub: detect and redact credentials on write, backstop on recall (#577)

Date: 2026-11-09. Issue: Archolith/menhir#577. Base: `58d39ced`. Fork counterpart: Archolith/graphiti#24 (out of scope here).
Owner decisions (2026-11-09): use the `detect-secrets` library; scan at intake, not in the queued worker.

## Why

Nothing checks memory content for credentials. A stored key goes to the embedder and the extraction LLM at enrichment, and any client that can read the namespace gets it back on recall.

## Safety property

**A secret that the detector recognises in text a client submits must not reach durable storage, an embedder, an LLM or telemetry from a Menhir write path, and must not be returned by the ranked recall path.** It is defence in depth. A detector misses things, and the document must say so as `privacy.py` does for itself.

## Scope

In: the detector wrapper, the single intake handler, every Menhir writer in the inventory, a worker-side idempotent scrub, `SecretWarden` for ranked recall results, an output scrub for `source_memories` and `temporal_facts`, and a read-only namespace scan.
Out (reported, not done): scrubbing LLM-extracted facts and summaries (needs the fork hook, graphiti#24); redact-in-place of existing data; `recall_history`, `recall_timeline`, `get_provenance`, `get_scalar_view_items` and `authority_layer` output scrubbing (follow-up, listed under Remaining exposure); entropy-based detection.

## Design

1. **Detector** `domain/secret_scrub.py`, pure, no network. Wraps `detect-secrets==1.5.0` through `scan_line` under `transient_settings` with the keyword, provider-key, JWT and private-key plugins and no entropy plugins and no filters (probe on 2026-11-09: 8 of 8 planted kinds found, 0 of 6 lookalikes flagged, p99 64 ms at 48,000 chars). `detect_secrets(text) -> list[SecretHit(kind, value)]`. The library returns values, not spans, so `redact(text)` replaces each hit value with `[REDACTED:<kind>]`.
2. **One handler.** `prepare_memory_text(raw, *, policy) -> ScrubbedText`. `ScrubbedText` is a `str` subclass whose constructor is private to this module. Policy `MENHIR_SECRET_POLICY` is `redact` (default) or `refuse` (raises `ValueError`, same path as the size check). The result carries kinds and a count, never values.
3. **Writers take `ScrubbedText`** at the repository boundary: queue node (`create_pending_episode`), `create_evidence_projection`, `create_temporal`, `record_turn_evidence`, `create_todo`, `create_candidate`, `write_document` excerpt, `create_raw_capture_entity`. A runtime `isinstance` check there raises on a plain `str`.
4. **Intake** `services/ingest_intake.py:100`: scrub `episode`, `diff` and `user_statement` right after `validate_memory_payload` and before the idempotency digest, the `record_turn_evidence` call and `create_pending_episode`. Record `secret_redactions` (kinds, count) on the episode.
5. **Worker fallback.** Scrub the composed body in `compose_episode_body` (`enrichment_steps.py:158`) and before `embed_episode_content`. Idempotent. Covers rows queued before deploy.
6. **SecretWarden** in `domain/warden.py`: `WardenContext` gains `candidate_text`, filled at `assertion_pipeline.py:127` from the candidate's `content`, `summary` and `name`. REFUSE when it matches. Add it to the default chain in `assertion_pipeline.py:89-91` and to `configured_guards` in `recall_pipeline.py:483` in the same change.
7. **Fail closed.** The frontier gate at `recall_support.py:697-710` returns candidates unchecked when it throws. For the secret guard, an exception drops text-bearing results or marks the receipt `failed` with a notice. Decide in Phase 3 and test it.
8. **Output scrub** for `source_memories` (`recall_pipeline.py:200-272`) and `temporal_facts` (`recall_pipeline.py:1864-1889`), both of which bypass the warden chain.
9. **Scan command** (`menhir secrets scan --namespace`): counts matches by kind and memory type, prints no values.

## Alternatives considered

- Scan in the queued worker: rejected by the owner. Raw text would already sit in the queue node and telemetry.
- Hand-written patterns: more code and more false positives than the library. gitleaks' rule list is the fallback source.
- gitleaks or TruffleHog as a subprocess per write: too heavy; TruffleHog's live-service checks break the no-network rule.

## Whole state machine and counterexamples

Actors: MCP, REST, hook scripts, client ops, CLI; queue node; ingest worker; scheduler (typed assertions, scalar Views, personal-memory LLM); Graphiti `add_episode` (fork); recall service; wardens.
- Rows queued before deploy hold plaintext: the worker fallback (item 5) and the scan (item 9) cover them.
- `user_statement` is recorded as `:TurnEvidence` before any scrub today (`ingest_intake.py:149`); item 4 moves the scrub ahead of it.
- Hook scripts post straight to `/api/turn-evidence` with no validator; the scrub belongs in the repository writer, not the route.
- A hot-restarted process on the old version can still write plaintext during a rolling deploy: known gap, covered by the scan.
- Idempotent replay: the digest must be computed on scrubbed text, otherwise a retry of a redacted write does not match its first attempt.
- The LLM can restate a secret in a fact or summary in different words. Graphiti output is not scrubbed by this plan (fork issue #24).

## Phases (one executor each, files fenced, focused tests only)

| # | Work | Files |
|---|---|---|
| 1 | Detector, `ScrubbedText`, `prepare_memory_text`, setting, dependency pin. Unit tests with the true and false positives from the probe, UUIDs, SHAs, SHA-256, prose. | `domain/secret_scrub.py`, `config/settings_model.py`, `pyproject.toml`, `uv.lock`, new tests |
| 2 | Wire intake, TEMPORAL, turn evidence, todo, candidate, document excerpt, raw-capture; worker fallback; guard test failing on a plain `str` reaching a writer. | the writers named in item 3 and `ingest_worker.py`, `enrichment_steps.py`, plus tests |
| 3 | `SecretWarden`, both guard lists, fail-closed, output scrub for `source_memories` and `temporal_facts`; update the two exact-guard-list asserts in `tests/test_warden_execution_status.py` and any matrix test that pins the list. | `domain/warden.py`, `assertion_pipeline.py`, `recall_pipeline.py`, `recall_support.py`, tests |
| 4 | Namespace scan command, count-only. | CLI module, new tests |

## Validation

Focused tests per phase; no full local suite (`AGENTS.md`: CI runs it). Through-the-surface tests: a write via MCP and via REST stores redacted text; a pre-seeded secret-bearing Entity is refused by `RecallService.recall`; an oracle failure does not admit it (#571); a canary secret never reaches a fake embedder or a fake LLM from the worker.

## Remaining exposure after this plan

LLM-extracted facts and summaries (fork #24); `recall_history`, `recall_timeline`, `get_provenance`, `get_scalar_view_items`, `authority_layer`; plaintext already stored; secrets the detector does not recognise; operator Cypher.

## Docs to update

`CHANGELOG.md`, `.agent/` ingest and warden docs, `docs/security-posture.md`, and the settings reference for `MENHIR_SECRET_POLICY`.
