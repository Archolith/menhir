# Current plan index

Status: current execution routing index, audited 2026-11-02.

This directory contains executable ownership: active plans, partially implemented plans with a
named residual, and owner decisions that still control whether work exists. Lower-priority work is
routed through the [backlog index](backlog/README.md). Useful non-executable material lives in the
[reference library](../reference/README.md); completed and superseded records live under
[`../archive/`](../archive/).

This index routes the current execution owners listed below exactly once.

## Active execution authority

| Document | Current ownership |
|---|---|
| Deployment control-plane architecture reset (no plan file) | The private `Archolith/menhir-deploy` repository is live and production runs Menhir 0.3.2 (released 2026-11-02). No plan in this directory governs it; deployment work is tracked in that repository. |
| [`menhir-168-phase0-baseline-contract.md`](menhir-168-phase0-baseline-contract.md) | Pinned baseline, ratified limits and evidence contract for the #168 new-install-defaults campaign (tracker #168 still open); every paid or live run needs a fresh owner go-ahead. |
| [`menhir-feature-flag-registry.md`](menhir-feature-flag-registry.md) | P1–P3 candidate implements a 98-entry metadata inventory, consistency tests and commented environment backfill; merge/CI and optional P4–P5 remain. |
| [`menhir-core-promotion-restack-2026-08-31.md`](menhir-core-promotion-restack-2026-08-31.md) | Restack core-promotion PRs #11–#42 from the bottom branch, preserve tranche boundaries, and repair definition-scoped bounded recovery in tranches 5 and 9. |
| [`menhir-research-execution-ladder.md`](menhir-research-execution-ladder.md) | Dependency-ordered research → code → bench sequence. Read-side rungs are closed; Track W6 is the remaining write-side rung. |
| [`menhir-work-artifact-reconciliation-2026-08-11.md`](menhir-work-artifact-reconciliation-2026-08-11.md) | Add read-only corpus parity auditing, hash/Git-backed source reconciliation, bounded move detectors, and a separately approved live-graph repair. |
| [`menhir-conflict-detection-signal-2026-08-09.md`](menhir-conflict-detection-signal-2026-08-09.md) | Separate fused-retrieval score semantics from cosine conflict thresholds. |
| [`menhir-conflict-suggestion-remediation-2026-08-09.md`](menhir-conflict-suggestion-remediation-2026-08-09.md) | Repair replacement suggestions and dead resolved-status semantics. |
| [`menhir-intent-state-view-2026-08-08.md`](menhir-intent-state-view-2026-08-08.md) | Ingest-owned tentative/plan state and its materialized View. |
| [`menhir-namespace-contract-2026-08-09.md`](menhir-namespace-contract-2026-08-09.md) | Namespace enumeration, centralized validation, naming decision, and selective cleanup. |
| [`menhir-projection-realization-coverage-implementation.md`](menhir-projection-realization-coverage-implementation.md) | Projection parity and realization-observation coverage implementation. |
| [`menhir-unbounded-graph-writes-2026-08-09.md`](menhir-unbounded-graph-writes-2026-08-09.md) | Bound raw graph-write payload retention rather than only enrichment input. |
| [`menhir-conflict-similarity-floor-plan-2026-11-05.md`](menhir-conflict-similarity-floor-plan-2026-11-05.md) | Owner-approved similarity floor and belief-shaped fixture for conflict detection; implementation is open PR #392. Replaces Phases 1-2 of the conflict detection signal plan. |
| [`menhir-574-vector-indexes-2026-11-09.md`](menhir-574-vector-indexes-2026-11-09.md) | Default vector indexes and index-backed similarity search with a scan fallback (#574); implementation in progress. |
| [`menhir-427-saga-journal-truth-plan-2026-11-12.md`](menhir-427-saga-journal-truth-plan-2026-11-12.md) | Close stranded saga rows on a provable server rollback, escalate commits refused after graph work, per-claim owner tokens, fail-closed mark API, and a `menhir saga` operator group (#427, #404, #406); implementation in progress. |
| [`menhir-view-evidence-lifecycle-2026-08-28.md`](menhir-view-evidence-lifecycle-2026-08-28.md) | Keep current View contributors alive under automatic lifecycle work, invalidate dependent Views on explicit erasure, and exclude stale/internal/orphaned Views from recall. |

## Partially implemented

| Document | Implemented | Remaining owner work |
|---|---|---|
| [`menhir-mcp-snapshot-ingest-2026-09-16.md`](menhir-mcp-snapshot-ingest-2026-09-16.md) | P0 code half and P1: wire contract, selection policy, bundler, `menhir sync --check`, 118 tests. | P0's transport measurement, which gates P2; then P2-P6. Every `SnapshotLimits` value is provisional until an owner sets it (open decision 1). |
| [`menhir-compositional-scalar-identity-2026-08-05.md`](menhir-compositional-scalar-identity-2026-08-05.md) | Phases 1–4 and bounded panels. | Preregistered larger-population evidence and any promotion decision. |
| [`menhir-deterministic-first-event-scalar-2026-07-30.md`](menhir-deterministic-first-event-scalar-2026-07-30.md) | Phase 1/2A and bounded smoke. | Population gates, frozen evaluation, and class-level promotion decisions. |
| [`menhir-scalar-default-enablement-plan.md`](menhir-scalar-default-enablement-plan.md) | Steps 1–5 (agent-quoted evidence, value-in-quote, decimal equality, evidence-only input, lenient defaults; PRs #195–#199). | Steps 6–7 are marked not started in the plan and the perceiver-version rule for existing graphs is open; later #168 freeze commits (e.g. `cc931186`) changed defaults, so the plan's status line is unconfirmed against main. |
| [`menhir-snapshot-p3-extraction-design-2026-09-17.md`](menhir-snapshot-p3-extraction-design-2026-09-17.md) | P3 threat model and mechanism (APPROVED 2026-09-17); the archive validator is built. | The writing half of extraction is not built. Child of the snapshot-ingest plan above. |
| [`menhir-source-memory-lane-plan.md`](menhir-source-memory-lane-plan.md) | Source-memory recall lane step 1, merged (#206, default on in #208). | None identified; executor prompt kept as history. Archive candidate once the owner confirms (not confirmed here). |
| [`menhir-recall-history-plan.md`](menhir-recall-history-plan.md) | `recall_history` drill-down tool, merged (#207); pools later defaulted off (#396) and removed (#399). | None identified; archive candidate (not confirmed here). |
| [`menhir-recall-timeline-plan.md`](menhir-recall-timeline-plan.md) | `recall_timeline` on-demand time navigation, merged (#213) with follow-ups (e.g. CF-268). | None identified; archive candidate (not confirmed here). |
| [`menhir-recall-timeline-phaseA-fixes.md`](menhir-recall-timeline-phaseA-fixes.md) | Binding review fixes F1+ for `recall_timeline` phase A (timeline Cypher ordering, `subject` as a thread filter). | Companion to the timeline plan; no separate residual confirmed. Archive with it. |
| [`menhir-scalar-counting-plan-2026-10-15.md`](menhir-scalar-counting-plan-2026-10-15.md) | Counting units 1, 3, 3a, R, 6 and 4R, all default off. | Unit 5, distinct items and unit 7 (#370); paid A/B per flag (#361, #362). |
| [`menhir-two-tier-self-identity-plan-2026-10-21.md`](menhir-two-tier-self-identity-plan-2026-10-21.md) | Phases 1-3 and 5 (#355), default off; phase 4 report only. | The journaled fold of approved user forks (#371). |
| [`typed-recall-packet-prototype.md`](typed-recall-packet-prototype.md) | Scalar/event inspection packet. | Admitted intent-state integration after the Intent State View exists. |

## Owner decision required

Context-composition Stages 2–4 were deferred by the owner on 2026-10-23; see
[`../archive/plans/menhir-context-composition-production-integration.md`](../archive/plans/menhir-context-composition-production-integration.md).

| Document | Decision needed |
|---|---|
| [`menhir-cf17-admission-gate-remediation-deepseek.md`](menhir-cf17-admission-gate-remediation-deepseek.md) | CF-17 (token-overlap admission gate admits contradicting claims) was CONFIRMED High with remediation pending as of 2026-08-18; whether and how to execute it is not decided here, and its current state on main is unverified. |
| [`menhir-post-install-and-agent-defaults-plan.md`](menhir-post-install-and-agent-defaults-plan.md) | Plan says work sits on an unmerged branch: merge it or close it. `menhir setup` has since landed in `src/menhir/cli/setup.py`, so the plan may be superseded; unconfirmed. |
| [`menhir-snapshot-multi-tenant-architecture-2026-09-20.md`](menhir-snapshot-multi-tenant-architecture-2026-09-20.md) | PROPOSED, no implementation: ownership for the snapshot graph-write path before wide deployment. |
| [`menhir-snapshot-p4-graph-write-design-2026-09-17.md`](menhir-snapshot-p4-graph-write-design-2026-09-17.md) | PROPOSED, not approved: how a partial P4 graph write stays survivable. |
| [`menhir-snapshot-wiring-to-working-sync-2026-09-20.md`](menhir-snapshot-wiring-to-working-sync-2026-09-20.md) | PROPOSED, no implementation: connect the built post-upload sync stages end to end (tenancy position in issue #127). |

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
