# Current plan index

Status: current execution routing index, audited 2026-08-30.

This directory contains executable ownership: active plans, partially implemented plans with a
named residual, and owner decisions that still control whether work exists. Lower-priority work is
routed through the [backlog index](backlog/README.md). Useful non-executable material lives in the
[reference library](../reference/README.md); completed and superseded records live under
[`../archive/`](../archive/).

This index routes the current execution owners listed below exactly once.

## Active execution authority

| Document | Current ownership |
|---|---|
| [`menhir-foundation-completion-2026-08-30.md`](menhir-foundation-completion-2026-08-30.md) | Route 9–13 engineer-weeks through one admission → mutation journal → lifecycle certification → exclusive writer-authority chain. |
| [`menhir-foundation-phase-1-extension-substrate-2026-08-30.md`](menhir-foundation-phase-1-extension-substrate-2026-08-30.md) | Add namespace-bound generic assertions, atomic admission decisions, source-relative currentness, an ordered mutation journal, and explicit legacy writer ownership. |
| [`menhir-foundation-phase-2-runtime-orchestration-2026-08-30.md`](menhir-foundation-phase-2-runtime-orchestration-2026-08-30.md) | Consume the journal through behavior-bound publication, fair bounded workers, temporal wakeups, typed freshness/corruption diagnostics, and read-only scalar shadow. |
| [`menhir-foundation-phase-3-hostile-domain-proofs-2026-08-30.md`](menhir-foundation-phase-3-hostile-domain-proofs-2026-08-30.md) | Prove materially different investigation and personality algebras through a frozen provisional seam, real provenance, hostile key collisions, and actual bounded recall. |
| [`menhir-foundation-phase-4-developer-surface-and-cutover-2026-08-30.md`](menhir-foundation-phase-4-developer-surface-and-cutover-2026-08-30.md) | Stabilize clean-installable author/host/test APIs and perform an attested, mutually exclusive writer cutover with durable definition retirement. |
| [`menhir-contabo-full-production-migration-2026-08-25.md`](menhir-contabo-full-production-migration-2026-08-25.md) | Move the complete OAuth/MCP/runtime/Neo4j stack to Contabo through an isolated Menhir Compose project, a transactional shared-Caddy integration, immutable releases, bounded VPS operations, verified state transfer, and rollback-safe graph/OAuth authority. |
| [`menhir-research-execution-ladder.md`](menhir-research-execution-ladder.md) | Dependency-ordered research → code → bench sequence. Read-side rungs are closed; Track W6 is the remaining write-side rung. |
| [`menhir-work-artifact-reconciliation-2026-08-11.md`](menhir-work-artifact-reconciliation-2026-08-11.md) | Add read-only corpus parity auditing, hash/Git-backed source reconciliation, bounded move detectors, and a separately approved live-graph repair. |
| [`menhir-conflict-detection-signal-2026-08-09.md`](menhir-conflict-detection-signal-2026-08-09.md) | Separate fused-retrieval score semantics from cosine conflict thresholds. |
| [`menhir-conflict-suggestion-remediation-2026-08-09.md`](menhir-conflict-suggestion-remediation-2026-08-09.md) | Repair replacement suggestions and dead resolved-status semantics. |
| [`menhir-intent-state-view-2026-08-08.md`](menhir-intent-state-view-2026-08-08.md) | Ingest-owned tentative/plan state and its materialized View. |
| [`menhir-namespace-contract-2026-08-09.md`](menhir-namespace-contract-2026-08-09.md) | Namespace enumeration, centralized validation, naming decision, and selective cleanup. |
| [`menhir-projection-realization-coverage-implementation.md`](menhir-projection-realization-coverage-implementation.md) | Projection parity and realization-observation coverage implementation. |
| [`menhir-unbounded-graph-writes-2026-08-09.md`](menhir-unbounded-graph-writes-2026-08-09.md) | Bound raw graph-write payload retention rather than only enrichment input. |
| [`menhir-view-evidence-lifecycle-2026-08-28.md`](menhir-view-evidence-lifecycle-2026-08-28.md) | Keep current View contributors alive under automatic lifecycle work, invalidate dependent Views on explicit erasure, and exclude stale/internal/orphaned Views from recall. |

Foundation Phase 1 is blocked on owner acceptance of proposed
[ADR 0002 — generic assertion currentness and mutation journal](../adr/0002-generic-assertion-currentness-and-journal.md).
The ADR resolves the design questions needed for implementation but does not make the generic
extension substrate or scheduler available.

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
