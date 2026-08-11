# File Index

Compact inventory of the main `.agent` docs.

## Routing

- `README.md` -> minimal bot entry router
- `maintenance.md` -> maintenance and changelog policy
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

## Research & Forward Planning

- `research/menhir-research-execution-ladder.md` -> research → code → bench build order (start here for "what's next"); read-side rungs R*, active write-side arc in Track W
- `plans/aggregation-as-consolidation.md` -> write-time consolidation thesis + D0 retrieval-entropy + the locked build order
- `plans/quantstate-agent-counter.md` -> D1 QuantState primitive (built; 3 increments)
- `plans/event-fold-view-architecture.md` -> Event → Fold → View frame; ViewKind SSOT; the stateful-fold gap
- `../docs/roadmap/README.md` -> build-sequencing and strategic notes, grouped by altitude
- `plans/backlog/deferred-verification.md` -> LIVING checklist of tests/benches owed once a real env is available (remote sessions can't run them)
- `plans/backlog/r1-hybrid-candidate-generation.md` -> R1 design note (hybrid candidate generation + source-aware floor)
- `plans/backlog/r2-facet-candidate-generation.md` -> R2 design note (bench-first facet retrieval; no production change until it beats baselines)
- `../docs/research/README.md` -> forward research index (positioning, retrieval pipeline, belief, vision)
- `../docs/research/positioning/positioning.md` -> canonical product/category positioning (CIP)
- `memory-roadmap.md` -> shipped v1 milestones (history)
- `post-v1-todo.md` -> living TODO on the shipped system
- `memory-view-kinds-frontier-transfer.md` -> ranked design exploration of additional View kinds (Setpoint, Deadband, Error, Integral, Remanence, Cascade, Disturbance + two counterexamples), transferred from process control
