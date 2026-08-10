# LLM Token Telemetry

## Why

- Canonical benchmark runs currently retain LLM call counts but discard provider-reported token usage during ingest.
- This prevents exact ingest-plus-recall accounting after a run finishes.

## Scope

- Capture provider-reported input, output, total, cached-input, and reasoning-output tokens for every instrumented LLM call.
- Persist one terminal row per instrumented provider-client call in the existing SQLite telemetry sidecar, including run and episode correlation.
- Cover async OpenAI-compatible calls plus the synchronous scalar-perception chat and View embedding seams.
- Provide a read-only aggregate and a benchmark artifact for future LongMemEval runs.
- Do not estimate missing provider usage or retrofit historical runs.

## Proposed Design

- Extend `LLMUsageEvent` at the existing OpenAI-compatible instrumentation boundary with a stable call id, duration, operation, and normalized usage fields.
- Use the request-scoped episode callback when present; otherwise fall back to a best-effort global telemetry recorder so Phase 3 and maintenance calls are still counted.
- Add an append-only `llm_usage_events` table and aggregation methods to `McpTelemetryStore` through a focused mixin.
- Emit `ingest_llm_usage.json` from the LongMemEval buildout wrapper before recall starts.

## Alternatives Considered

- Estimate tokens from characters: rejected because provider usage is available and estimates would be unsuitable for cost claims.
- Store usage only in `episode_task_events.details_json`: rejected because Phase 3 and maintenance calls are not always episode-scoped and aggregation would be fragile.
- Add cost directly to Menhir: rejected because pricing changes independently of immutable token evidence.

## Risks

- Providers may omit or rename usage fields; missing usage is recorded explicitly and raw normalized usage is preserved.
- Telemetry must never affect the LLM call path; all persistence remains best effort.
- Caller-level retries count separately because each provider-client call receives a unique call id;
  transport retries hidden inside a provider SDK remain part of that client call.

## Invariants

- No prompt or response content is persisted in token telemetry.
- Provider-reported counts are never replaced with estimates.
- Existing telemetry databases migrate automatically and remain readable.
- Instrumentation failures never fail ingestion, recall, or maintenance.

## Validation

- Unit tests for OpenAI chat, embeddings, failures, cache/detail normalization, synchronous chat, and persistence aggregation.
- Existing focused telemetry, observability, provider, scalar, and LongMemEval validation tests.
- Verify a temporary telemetry DB can be summarized into the benchmark artifact.

## Docs To Update

- `.agent/architecture.md`
- `.agent/data_models.md`
- `CHANGELOG.md`
- `scripts/longmemeval/README.md`
