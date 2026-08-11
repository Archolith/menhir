# Plan index

Status: current routing index, audited 2026-08-10.

This directory holds executable plans, staged implementation records, and a small number of
research/handoff documents that still need an explicit disposition. This index routes every
top-level Markdown file exactly once. A row summarizes the source document; it does not override
that document's status or authorize implementation.

For dependency-ordered research work, start with the
[research execution ladder](../research/menhir-research-execution-ladder.md). For lower-priority and
gated work, use the [backlog index](backlog/README.md). Completed records live under
[`../archive/plans/`](../archive/plans/).

## Active, staged, or investigation-owning

| Document | Source status | Use it for |
|---|---|---|
| [`menhir-compositional-scalar-identity-2026-08-05.md`](menhir-compositional-scalar-identity-2026-08-05.md) | Phases 1–4 implemented; population evidence next | Current evidence and next measurement for compositional scalar identity. |
| [`menhir-console-dashboard-and-privacy-plan.md`](menhir-console-dashboard-and-privacy-plan.md) | In progress | Rich console dashboard and memory-content privacy redaction. |
| [`menhir-context-composition-production-integration.md`](menhir-context-composition-production-integration.md) | Stage 1 implemented; Stages 2–4 not started | Production integration sequence for extraction-context composition. |
| [`menhir-extraction-context-ablation-handoff.md`](menhir-extraction-context-ablation-handoff.md) | Active | Current extraction-context ablation and ingest-quality investigation. |
| [`menhir-resolve-suburbs-extraction-failure-handoff.md`](menhir-resolve-suburbs-extraction-failure-handoff.md) | New-session scope; revalidate before executing | Root-cause investigation for the production “suburbs” extraction failure. |
| [`typed-recall-packet-prototype.md`](typed-recall-packet-prototype.md) | Structured packet retained; recall-side intent inference removed | Inspection-only scalar/event packet presentation and its ingest-owned intent boundary. |

## Approved, ready, or proposed

These documents describe uncompleted work. Their own approval and gate language remains binding.

| Document | Source status | Use it for |
|---|---|---|
| [`menhir-belief-supersession-code-mapped-plan.md`](menhir-belief-supersession-code-mapped-plan.md) | Ready for Phase 1 | Code-mapped implementation sequence for belief supersession and temporal chains. |
| [`menhir-conflict-detection-signal-2026-08-09.md`](menhir-conflict-detection-signal-2026-08-09.md) | Planned | Correcting the conflict-detection signal. |
| [`menhir-conflict-suggestion-remediation-2026-08-09.md`](menhir-conflict-suggestion-remediation-2026-08-09.md) | Planned | Remediating conflict suggestions after signal correction. |
| [`menhir-deterministic-first-event-scalar-2026-07-30.md`](menhir-deterministic-first-event-scalar-2026-07-30.md) | Reviewer-approved | Deterministic-first event-scalar extraction; categorical history is owned by the archived event-history design. |
| [`menhir-explorer-mount-lifecycle-plan.md`](menhir-explorer-mount-lifecycle-plan.md) | Draft; awaiting approval | Mounting Explorer into the main application lifecycle. |
| [`menhir-intent-state-view-2026-08-08.md`](menhir-intent-state-view-2026-08-08.md) | Planned | Ingest-owned intent-state projection and authority. |
| [`menhir-namespace-contract-2026-08-09.md`](menhir-namespace-contract-2026-08-09.md) | Planned | Namespace invariants and contamination cleanup. |
| [`menhir-projection-realization-coverage-implementation.md`](menhir-projection-realization-coverage-implementation.md) | Ready for implementation | Projection and realization coverage implementation sequence. |
| [`menhir-structure-graph-coverage-2026-08-09.md`](menhir-structure-graph-coverage-2026-08-09.md) | Planned | Structure-graph coverage gaps and repairs. |
| [`menhir-todo-declared-links.md`](menhir-todo-declared-links.md) | Approved for implementation | Author-declared links that make todos first-class referents. |
| [`menhir-umbrella-repo-edges-2026-08-09.md`](menhir-umbrella-repo-edges-2026-08-09.md) | Design note | Small containment-edge addition for umbrella repositories. |
| [`menhir-unbounded-graph-writes-2026-08-09.md`](menhir-unbounded-graph-writes-2026-08-09.md) | Planned | Bounding graph writes and their operational blast radius. |

## Saved research and optional future work

| Document | Source status | Use it for |
|---|---|---|
| [`fresh-neo4j-memory-benchmark-plan.md`](fresh-neo4j-memory-benchmark-plan.md) | Post-MVP option; superseded for MVP | The heavier native fresh-graph IR benchmark, if post-MVP evidence requires it. |
| [`menhir-belief-supersession-temporal-chains-research.md`](menhir-belief-supersession-temporal-chains-research.md) | Saved research; not active | Research/prototype basis for belief supersession and temporal belief chains. |
| [`menhir-extraction-prompt-recency-recall-research.md`](menhir-extraction-prompt-recency-recall-research.md) | Saved Recall Labs research; not active | Distant-update extraction-prompt failure research. |

## Implemented reference retained here

| Document | Why it remains outside the archive |
|---|---|
| [`menhir-artifact-semantic-model.md`](menhir-artifact-semantic-model.md) | The artifact model shipped, but `ABOUT` subject cardinality remains a live design question. Keep this as the design owner until that question moves to another current document. |

## Closeout status must be reconciled

Do not archive these from filenames or apparent code presence. Confirm landed commits, verification,
and residual work first, then add an explicit status/result block.

| Document | Why reconciliation is needed |
|---|---|
| [`menhir-llm-token-telemetry-2026-08-06.md`](menhir-llm-token-telemetry-2026-08-06.md) | Specifies implementation and validation but records no final result. |
| [`menhir-merge-provenance-correctness-remediation-2026-07-28.md`](menhir-merge-provenance-correctness-remediation-2026-07-28.md) | Execution contract has completion criteria but no closeout report. |
| [`menhir-recall-lab-benchmark-explorer-2026-07-30.md`](menhir-recall-lab-benchmark-explorer-2026-07-30.md) | Detailed built-shape plan lacks an authoritative implementation status. |
| [`recall-lab-dashboard.md`](recall-lab-dashboard.md) | Dashboard contract lacks a result or completion marker. |
| [`ssot-remediation-2026-07-11.md`](ssot-remediation-2026-07-11.md) | Individual findings carry mixed updates, but the parent plan has no reconciled final status. |

## Maintenance rule

When a plan finishes, move durable runtime ownership into architecture, policy, backlog, or roadmap
docs; add a concise archive banner; repair inbound links; then move the plan with `git mv`. Keep a
completed plan here only when it still owns an explicit live question, as the artifact model does.
