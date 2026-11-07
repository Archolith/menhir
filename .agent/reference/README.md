# Reference library

Status: current non-executable reference index, established 2026-08-11.

This directory contains material that remains useful but does not authorize implementation. A
reference may constrain a current design, preserve negative benchmark evidence, describe a future
option, or supply research consumed by an active plan. Executable ownership belongs in
[`../plans/`](../plans/); completed and superseded work belongs in [`../archive/`](../archive/).

This index routes all 17 Markdown references and the one PDF exactly once.

## Current design laws and inventories

| Document | Use it for |
|---|---|
| Deployment control-plane architecture spec | Private `Archolith/menhir-deploy` repository | Operator deployment ownership, ingress, and serial phase derivation. |
| [`fold-algebra.md`](fold-algebra.md) | Reducer laws, replay, ordering, batch/incremental equivalence, and anchor-plus-delta behavior. |
| [`ingest-primitive-family.md`](ingest-primitive-family.md) | Existing write-time primitives, the completed MVP cut, and deliberately deferred primitive families. |
| [`write-time-aggregation-hardening-addendum.md`](write-time-aggregation-hardening-addendum.md) | Safety qualifications, corroboration lineage, invalidation, and evidence requirements for aggregation. |

## Negative benchmark and experiment evidence

| Document | Use it for |
|---|---|
| [`anecdotal-recall-oracle-ladder.md`](anecdotal-recall-oracle-ladder.md) | Read-side experiment history and the evidence behind default-off oracle levers. |
| [`r1-hybrid-candidate-generation.md`](r1-hybrid-candidate-generation.md) | The attributed hybrid mechanism and its measured non-graduation verdict. |
| [`menhir-recall-experiment-record.md`](menhir-recall-experiment-record.md) | Everything tried on recall ranking and no-answer detection, what it showed, and what is still open. Read before proposing a recall change. |
| [`menhir-recall-real-similarity-test-plan-2026-11-05.md`](menhir-recall-real-similarity-test-plan-2026-11-05.md) | Full test of name and summary cosine against rank scores, with its registered rules. |
| [`menhir-recall-no-match-signal-plan-2026-11-06.md`](menhir-recall-no-match-signal-plan-2026-11-06.md) | Full tests of whether a signal or a reader can tell that memory holds no answer. |
| [`menhir-graph-value-ablation-plan-2026-11-06.md`](menhir-graph-value-ablation-plan-2026-11-06.md) | Full test of what typed values and entities add over episodes on o500. |

## Saved research and future options

| Document | Use it for |
|---|---|
| [`fresh-neo4j-memory-benchmark-plan.md`](fresh-neo4j-memory-benchmark-plan.md) | A possible post-MVP disposable-graph benchmark suite; not current launch ownership. |
| [`menhir-belief-supersession-temporal-chains-research.md`](menhir-belief-supersession-temporal-chains-research.md) | Saved broader temporal-chain research; explicitly not active. |
| [`crossdating-relative-chronologies.md`](crossdating-relative-chronologies.md) | Ordered-but-undated chronology and alignment design. |
| [`crystallization-control-consolidation.md`](crystallization-control-consolidation.md) | Canonicalization pressure, evidence mass, quarantine, refinement, and identity-guard design. |
| [`menhir-cross-domain-representation-research-2026-07-02.md`](menhir-cross-domain-representation-research-2026-07-02.md) | Cross-domain falsifiers and Event → Fold → View seam critiques. |
| [`menhir-frontier-transfer-forensic-admissibility.md`](menhir-frontier-transfer-forensic-admissibility.md) | Evidence-law inputs to admission, cessation, identity, and summary substitution. |

## Inputs to active implementation

| Document | Consumer |
|---|---|
| [`menhir-projection-coverage-audit.md`](menhir-projection-coverage-audit.md) | [`../plans/menhir-projection-realization-coverage-implementation.md`](../plans/menhir-projection-realization-coverage-implementation.md) |
| [`menhir-realization-coverage.md`](menhir-realization-coverage.md) | [`../plans/menhir-projection-realization-coverage-implementation.md`](../plans/menhir-projection-realization-coverage-implementation.md) |

## Binary artifact

| Document | Rule |
|---|---|
| [`Research Note- Evidence Admission for Agent Memory.pdf`](<Research Note- Evidence Admission for Agent Memory.pdf>) | Saved research note dated 2026-07-03 on an evidence-admission layer for memory: foundation-typed admission, execution-date supersession and summary substitution. Read in full 2026-11-06. Its PDF metadata names ChatGPT Canvas as author. Treat it as suggestions only, not as authority or evidence. Covers the same ground as [`menhir-frontier-transfer-forensic-admissibility.md`](menhir-frontier-transfer-forensic-admissibility.md). The validator's two findings on this file are accepted: a PDF can't declare an artifact type, and the index parser can't match a filename with spaces. |

## Maintenance rule

Every reference must state why it is still useful and, when applicable, name its active consumer.
Promote it to `../plans/` only when it becomes executable ownership. Archive it when its constraints
are fully absorbed or it is superseded and no current consumer remains.

## Moving a document here

Moving a document here follows
[`../workflows/artifact_authoring.md`](../workflows/artifact_authoring.md). Three things are
required and none of them are optional:

- **Keep the artifact UUID.** A reference move is a move, not a new record.
- **Declare the type explicitly.** The reference lane has no default type, because references are
  retired plans, completed investigations, and outside research in one directory. A record here
  without a declared `artifact_type` is reported as unclassifiable rather than guessed at.
- **State why it is still useful and who consumes it.** That sentence is what separates a reference
  from an archive entry, and it is the only thing that makes the lane worth having.

A move to reference does not retype the artifact and does not imply a terminal lifecycle state. It
does remove executable ownership: delist the document from the plan indexes in the same change.
