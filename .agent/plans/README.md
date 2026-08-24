# Current plan index

Status: current execution routing index, audited 2026-08-11.

This directory contains executable ownership: active plans, partially implemented plans with a
named residual, and owner decisions that still control whether work exists. Lower-priority work is
routed through the [backlog index](backlog/README.md). Useful non-executable material lives in the
[reference library](../reference/README.md); completed and superseded records live under
[`../archive/`](../archive/).

This index routes all 13 top-level plan records plus the execution ladder exactly once.

## Active execution authority

| Document | Current ownership |
|---|---|
| [`menhir-oauth-refresh-chatgpt-integration-plan.md`](menhir-oauth-refresh-chatgpt-integration-plan.md) | Complete AS-005 refresh-token wiring and prove a durable ChatGPT web OAuth connection against `/mcp-http`. |
| [`menhir-research-execution-ladder.md`](menhir-research-execution-ladder.md) | Dependency-ordered research → code → bench sequence. Read-side rungs are closed; Track W6 is the remaining write-side rung. |
| [`menhir-work-artifact-reconciliation-2026-08-11.md`](menhir-work-artifact-reconciliation-2026-08-11.md) | Add read-only corpus parity auditing, hash/Git-backed source reconciliation, bounded move detectors, and a separately approved live-graph repair. |
| [`menhir-conflict-detection-signal-2026-08-09.md`](menhir-conflict-detection-signal-2026-08-09.md) | Separate fused-retrieval score semantics from cosine conflict thresholds. |
| [`menhir-conflict-suggestion-remediation-2026-08-09.md`](menhir-conflict-suggestion-remediation-2026-08-09.md) | Repair replacement suggestions and dead resolved-status semantics. |
| [`menhir-intent-state-view-2026-08-08.md`](menhir-intent-state-view-2026-08-08.md) | Ingest-owned tentative/plan state and its materialized View. |
| [`menhir-namespace-contract-2026-08-09.md`](menhir-namespace-contract-2026-08-09.md) | Namespace enumeration, centralized validation, naming decision, and selective cleanup. |
| [`menhir-projection-realization-coverage-implementation.md`](menhir-projection-realization-coverage-implementation.md) | Projection parity and realization-observation coverage implementation. |
| [`menhir-unbounded-graph-writes-2026-08-09.md`](menhir-unbounded-graph-writes-2026-08-09.md) | Bound raw graph-write payload retention rather than only enrichment input. |

## Partially implemented

| Document | Implemented | Remaining owner work |
|---|---|---|
| [`menhir-artifact-semantic-model.md`](menhir-artifact-semantic-model.md) | WorkArtifact model, migration, relationships, open questions, and MCP surface. | `CurrentPlanView`. |
| [`menhir-compositional-scalar-identity-2026-08-05.md`](menhir-compositional-scalar-identity-2026-08-05.md) | Phases 1–4 and bounded panels. | Preregistered larger-population evidence and any promotion decision. |
| [`menhir-deterministic-first-event-scalar-2026-07-30.md`](menhir-deterministic-first-event-scalar-2026-07-30.md) | Phase 1/2A and bounded smoke. | Population gates, frozen evaluation, and class-level promotion decisions. |
| [`typed-recall-packet-prototype.md`](typed-recall-packet-prototype.md) | Scalar/event inspection packet. | Admitted intent-state integration after the Intent State View exists. |

## Owner decision required

| Document | Decision |
|---|---|
| [`menhir-context-composition-production-integration.md`](menhir-context-composition-production-integration.md) | Decide whether Stages 2–4 remain wanted after Stage 1's negative result and the later independent fix of the motivating extraction failure. |

## Maintenance rule

Creating or moving a plan follows
[`../workflows/artifact_authoring.md`](../workflows/artifact_authoring.md) — it owns the field
definitions, the lifecycle vocabulary, and the move/copy/archive rules. Plans created after
reconciliation support is activated carry the metadata block it specifies; older plans are
grandfathered until the approved backfill. Promotion from `backlog/` and archival both keep the
artifact UUID.

A plan remains here only while it owns concrete execution or a named owner decision. Move a
lower-priority but still executable item to `backlog/`. Move durable non-executable knowledge to
`../reference/`. Add a disposition banner, repair live referrers, and archive a completed or
superseded owner under `../archive/`.