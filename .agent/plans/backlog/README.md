# Backlog index

Status: current routing index, audited 2026-08-10.

This directory holds gated, partial, and future work. It is not a single execution queue. Read the
source document's gate before acting, and use the
[research execution ladder](../../research/menhir-research-execution-ladder.md) when a backlog item
belongs to a dependency-ordered rung. This index routes all 25 Markdown records exactly once.

## Living, active, partial, or evidence-owning

| Document | Source status | Use it for |
|---|---|---|
| [`anecdotal-recall-oracle-ladder.md`](anecdotal-recall-oracle-ladder.md) | Living reference; do not archive | Read-side experiment history and honest benchmark verdicts. |
| [`deferred-verification.md`](deferred-verification.md) | Living checklist | Tests and benches owed when a suitable environment becomes available. |
| [`fold-algebra.md`](fold-algebra.md) | Implemented; current design record | Fold laws, batch/incremental equivalence, replay, ordering, and anchor-plus-delta behavior. |
| [`graph-verifiers.md`](graph-verifiers.md) | Partial / active | Graph-native verification already shipped and the remaining verifier work. |
| [`ingest-primitive-family.md`](ingest-primitive-family.md) | Inventory and build order | Existing write-time primitives and the consolidation work they imply. |
| [`menhir-frontier-undone-work-chunks.md`](menhir-frontier-undone-work-chunks.md) | Active historical snapshot; reconcile with ladder | Executable grouping of frontier-era unfinished work; never use without checking current Track W/R-rung verdicts. |
| [`menhir-memory-supersession-and-dedup-plan.md`](menhir-memory-supersession-and-dedup-plan.md) | Active; owner decision pending | Memory supersession chains and redundancy deduplication. |
| [`menhir-rung1-temporal-intent-reconciliation.md`](menhir-rung1-temporal-intent-reconciliation.md) | Active / not wired | Temporal-intent reconciliation implementation. |
| [`perception-consolidation-prod-wiring.md`](perception-consolidation-prod-wiring.md) | Wiring built; retained for open Law-3 work | Production perception/consolidation wiring and the context needed by W6. |
| [`perception-law3-bias-coverage-and-crosscheck-independence.md`](perception-law3-bias-coverage-and-crosscheck-independence.md) | Part 2 done; Part 1 remains | Track W6’s live-LLM corroboration-independence experiment. |
| [`r1-hybrid-candidate-generation.md`](r1-hybrid-candidate-generation.md) | In progress; measured non-graduation | R1 hybrid candidate path, source-aware floor, and benchmark verdict. |
| [`r2-facet-production-integration.md`](r2-facet-production-integration.md) | Phases 1–3 shipped; activation parked | Observe-only facet integration and Recall Lab experiment seam; production activation lacks evidence. |

## Proposed or unstarted

| Document | Source status | Use it for |
|---|---|---|
| [`admission-capability-separation-plan.md`](admission-capability-separation-plan.md) | Backlog | Separating user-tier write capabilities. |
| [`cessation-tombstone-primitive-plan.md`](cessation-tombstone-primitive-plan.md) | Backlog | Cessation/tombstone semantics and the `Ceased` verb. |
| [`foundation-typed-admission-plan.md`](foundation-typed-admission-plan.md) | Backlog | Foundation-typed write-time admission. |
| [`identity-keying-layer-plan.md`](identity-keying-layer-plan.md) | Backlog | Union-find identity view and twin-probe merge guard. |
| [`kappa-replay-perceiver-versioning-plan.md`](kappa-replay-perceiver-versioning-plan.md) | Backlog | Perceiver versioning and deterministic re-fold insurance. |
| [`l3l4-semantic-overlay-sequencing-plan.md`](l3l4-semantic-overlay-sequencing-plan.md) | Backlog; activation owner-reserved | L3/L4 semantic overlay, task-oracle runtime, and Cold Start Brief sequencing. |
| [`menhir-hyperedge-ready-storage.md`](menhir-hyperedge-ready-storage.md) | Design proposal | Hyperedge-ready storage without committing to a hypergraph backend. |
| [`menhir-temporal-bulk-ingest.md`](menhir-temporal-bulk-ingest.md) | Nice-to-have proposal | Preserving temporal truth during bulk ingest. |
| [`menhir-temporal-chronostratum-plan.md`](menhir-temporal-chronostratum-plan.md) | Capability plan | Chronostratum temporal-memory design. |
| [`r2-facet-candidate-generation.md`](r2-facet-candidate-generation.md) | Planned; bench-first | Facet candidate generation, gated on beating baselines before production changes. |
| [`retrieval-reachability-receipts-and-bundle-honesty.md`](retrieval-reachability-receipts-and-bundle-honesty.md) | Planned | Reachability receipts and honest retrieval bundles. |
| [`retrieval-recency-split-and-view-injection.md`](retrieval-recency-split-and-view-injection.md) | Planned; decision-gated | Recency split, View injection, and lens routing. |
| [`view-summary-substitution-plan.md`](view-summary-substitution-plan.md) | Backlog | Letting Views shadow source episodes at recall through summary substitution. |

## Maintenance rule

Move an item into the top-level plan directory only when it becomes the current executable owner.
Archive it after implementation only when current ownership and residual work have been routed
elsewhere. Partial mechanisms and negative benchmark records stay indexed here while they still
constrain future decisions.
