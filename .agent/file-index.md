# File Index

Compact inventory of the main `.agent` docs.

## Routing

- `README.md` -> minimal bot entry router
- `maintenance.md` -> maintenance and changelog policy
- `workflows/artifact_authoring.md` -> canonical contract for creating, moving, archiving,
  and reclassifying tracked artifacts; owns the metadata field definitions
- `concept-ids.md` -> compact id router
- `concept-ids.yaml` -> full machine-readable id registry
- `concept-tree-design.md` -> tree/document authoring model

## Task Routers

- `tasks-debugging.md`
- `tasks-ingest.md`
- `tasks-mcp.md`
- `scripts-index.md` -> every durable script in menhir + archolith-bench, by question answered

## Compact Companions

- `memory-foundations.md`
- `memory-policy.md`
- `memory-ingest-queries.md`
- `memory-futures.md`
- `memory-backlog.md`
- `glossary.md`

## Domain Truth Package

`src/menhir/domain/truth/` — single source of truth for provenance and trust vocabulary.
All source-kind strings, confidence constants, label enums, and the `TruthAttestation` object live here.

| Sub-module | Contents |
|---|---|
| `attestation.py` | `ReviewState`, `TruthClaim`, `TruthAttestation`, `review_state_from_confidence` |
| `kinds.py`       | `ANCHOR_KINDS`, `SELF_SOURCE_KINDS`, `SOURCE_LABEL_TO_KIND`, `KIND_TO_SIGNAL`, `DIVERSITY_FAMILY`, `SOURCE_CONFIDENCE_*` |
| `labels.py`      | `WardenLabel` enum (`HISTORICAL`, `CONFLICT`, `UNCERTAIN`, `SUSPICIOUS_CHAIN`) |
| `__init__.py`    | Re-exports all public symbols |

Consumers import from `menhir.domain.truth` (or via `menhir.domain` which re-exports the full set).

## Heavy References

- `architecture.md`
- `data_models.md`
- `endpoints.md`
- `memory-design.md`
- `memory-roadmap.md`

## Machine-Readable Helpers

- `processing-states.yaml`
- `retry-policy.yaml`
- `mcp-tools.yaml`

## Utilities

- `tools/benchmark_doc_tokens.py`
- `workflows/run_and_test.md`
- `workflows/troubleshoot_enrichment_stalls.md`
- `workflows/scalar_state_measurement.md`
- `workflows/code_conventions.md`

## Plans, References & Forward Planning

- `plans/README.md` -> current execution router; active, partial, and owner-decision plans only
- `plans/backlog/README.md` -> lower-priority or gated executable work
- `plans/menhir-research-execution-ladder.md` -> research → code → bench build order (start here for "what's next"); read-side rungs R*, active write-side arc in Track W
- `reference/README.md` -> useful non-executable design laws, research, negative evidence, future options, and the unverified PDF
- `memory-aggregation-under-uncertainty.md` -> current design reference for precise write-time aggregates, veto gates, and anchor+delta safety
- `architecture.md` + `data_models.md` -> live Event → Fold → disposable View/projection architecture
- `reference/fold-algebra.md` -> fold laws, batch vs incremental evaluation, and implementation record
- `archive/plans/{aggregation-as-consolidation,quantstate-agent-counter,event-fold-view-architecture}.md` -> historical pivot, D1, and architecture decision records (not active plans)
- `../docs/roadmap/README.md` -> build-sequencing and strategic notes, grouped by altitude
- `plans/backlog/deferred-verification.md` -> LIVING checklist of tests/benches owed once a real env is available (remote sessions can't run them)
- `reference/r1-hybrid-candidate-generation.md` -> R1 design and measured non-graduation evidence
- `archive/plans/r2-facet-{candidate-generation,production-integration}.md` -> completed R2 design, integration, and PARK verdict
- `../docs/research/README.md` -> forward research index (positioning, retrieval pipeline, belief, vision)
- `../docs/research/positioning/positioning.md` -> canonical product/category positioning (CIP)
- `memory-roadmap.md` -> shipped v1 milestones (history)
- `post-v1-todo.md` -> living TODO on the shipped system
- `memory-view-kinds-frontier-transfer.md` -> ranked design exploration of additional View kinds (Setpoint, Deadband, Error, Integral, Remanence, Cascade, Disturbance + two counterexamples), transferred from process control
