# Backlog index

Status: current backlog routing index, audited 2026-08-11.

This directory contains executable but lower-priority or gated work. It is not a single queue. Read
the source document's gate before acting and consult the
[research execution ladder](../menhir-research-execution-ladder.md) when a backlog item belongs to a
dependency-ordered rung. Reference-only records no longer live here; use the
[reference library](../../reference/README.md).

This index routes all 15 backlog records exactly once.

## Active or proposed

| Document | Current ownership |
|---|---|
| [`deferred-verification.md`](deferred-verification.md) | Living checklist of tests and benches owed when the required environment is available. |
| [`menhir-hyperedge-ready-storage.md`](menhir-hyperedge-ready-storage.md) | Unscheduled hyperedge-ready logical/storage seam. |
| [`menhir-rung1-temporal-intent-reconciliation.md`](menhir-rung1-temporal-intent-reconciliation.md) | Wire the temporal-intent classifier and canonical fact filter into recall. |
| [`menhir-temporal-bulk-ingest.md`](menhir-temporal-bulk-ingest.md) | Temporally correct bulk-ingest optimization. |
| [`retrieval-recency-split-and-view-injection.md`](retrieval-recency-split-and-view-injection.md) | Recency split, View injection, and lens routing after the receipt gate. |
| [`view-summary-substitution-plan.md`](view-summary-substitution-plan.md) | Let Views shadow covered source episodes during recall. |

## Partially implemented

| Document | Implemented | Remaining owner work |
|---|---|---|
| [`cessation-tombstone-primitive-plan.md`](cessation-tombstone-primitive-plan.md) | Scalar `expire` operation. | Generic cessation event, explicit closed lifecycle, and reason semantics. |
| [`foundation-typed-admission-plan.md`](foundation-typed-admission-plan.md) | Foundation machinery in typed-scalar paths. | General main-ingest foundation boundary. |
| [`graph-verifiers.md`](graph-verifiers.md) | Core executor, scheduler wiring, and seeding. | Additional executor kinds and recall-side payoff. |
| [`identity-keying-layer-plan.md`](identity-keying-layer-plan.md) | Scalar identity composition and conservative binding. | Generic identity view/union-find and twin-probe merge guard. |
| [`l3l4-semantic-overlay-sequencing-plan.md`](l3l4-semantic-overlay-sequencing-plan.md) | L4 artifact storage, governance, and read path. | L3 semantic types and proposer/runtime work. |
| [`menhir-temporal-chronostratum-plan.md`](menhir-temporal-chronostratum-plan.md) | Pure-domain temporal rungs and bench evidence. | Gated production graph/recall wiring. |
| [`perception-law3-bias-coverage-and-crosscheck-independence.md`](perception-law3-bias-coverage-and-crosscheck-independence.md) | Law-3 bias coverage code. | Track W6 live corroboration-independence experiment. |
| [`retrieval-reachability-receipts-and-bundle-honesty.md`](retrieval-reachability-receipts-and-bundle-honesty.md) | Reachability telemetry, verdict markers, notes, and explicit empty results. | Bench aggregation helper and traced decision run. |

## Owner decision required

| Document | Decision |
|---|---|
| [`menhir-memory-supersession-and-dedup-plan.md`](menhir-memory-supersession-and-dedup-plan.md) | Decide whether generic `SUPERSEDED_BY` memory lineage is still wanted after temporal expiry and judge-gated merge shipped. |

## Maintenance rule

Promote an item to the top-level plan directory when it becomes current execution authority. Move
non-executable but still useful findings to `../../reference/`. Archive it after implementation or
supersession once residual ownership is routed elsewhere.
