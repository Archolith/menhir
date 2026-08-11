# Operational research index

Status: current routing index, audited 2026-08-10.

This directory contains Menhir-specific execution research, audits, and transfer inputs. It is
distinct from [`docs/research/`](../../docs/research/README.md), which owns the broader forward
research corpus and its themed clusters. This index routes all 11 Markdown documents and the one
PDF currently stored here.

## Current execution authority

| Document | Status | Use it for |
|---|---|---|
| [`menhir-research-execution-ladder.md`](menhir-research-execution-ladder.md) | Active | The dependency-ordered research → code → bench sequence. Read-side rungs are closed; Track W owns the live write-side arc and W6 is the remaining rung. |

## Research and reference designs

| Document | Status | Use it for |
|---|---|---|
| [`crossdating-relative-chronologies.md`](crossdating-relative-chronologies.md) | Frontier research; post-MVP | Relative chronology when event order is known more reliably than absolute dates. |
| [`crystallization-control-consolidation.md`](crystallization-control-consolidation.md) | Frontier research | Canonicalization pressure, nucleation, growth, merge, quarantine, and refinement in write-time consolidation. |
| [`menhir-cross-domain-representation-research-2026-07-02.md`](menhir-cross-domain-representation-research-2026-07-02.md) | Frontier architecture review | Cross-domain mechanisms that simplify Event → Fold → View representation; use its falsifying experiments before promoting an idea. |
| [`write-time-aggregation-hardening-addendum.md`](write-time-aggregation-hardening-addendum.md) | Reference addendum | Safety qualifications and evidence requirements for the current write-time aggregation design. |

## Audits, reviews, and transfer inputs

| Document | Disposition | Use it for |
|---|---|---|
| [`frontier-transfer-context-brief.md`](frontier-transfer-context-brief.md) | Reusable transfer input | Architecture-and-symptom brief intentionally stripped of prior research hypotheses for independent reviews. |
| [`l4-artifact-graphify-lens-review.md`](l4-artifact-graphify-lens-review.md) | Completed implementation review | Graph-boundary review of the L4 artifact implementation and the fixed status-clamping hole. |
| [`menhir-beacon-architecture-review.md`](menhir-beacon-architecture-review.md) | Historical architecture review | External review of Beacon/Menhir boundaries; compare with current accepted Beacon decisions before reuse. |
| [`menhir-frontier-transfer-forensic-admissibility.md`](menhir-frontier-transfer-forensic-admissibility.md) | Transfer review | Evidence-law mechanisms for foundation-typed admission, temporal ordering, and write-time exclusion. |
| [`menhir-projection-coverage-audit.md`](menhir-projection-coverage-audit.md) | Research proposal; revision required | Fold-aware projection coverage and repair requirements. |
| [`menhir-realization-coverage.md`](menhir-realization-coverage.md) | Research proposal; design required | Observation-ledger approach to measuring independent perception support without violating assertion identity. |

## Unverified binary artifact

| Document | Status | Rule |
|---|---|---|
| [`Research Note- Evidence Admission for Agent Memory.pdf`](<Research Note- Evidence Admission for Agent Memory.pdf>) | Content classification unverified | The filename suggests an admission/evidence topic, but curator rules prohibit final classification from the filename alone. Read and classify only when that work is explicitly approved. |

## Maintenance rule

New operational research must name its status, current owner or consumer, and promotion condition.
Completed implementation plans and accepted reviews move to `.agent/archive/`; reusable research
inputs remain here while this index states why they are still useful.
