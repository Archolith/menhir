# Recall/Trace Domain Cycle Break

> **ARCHIVED 2026-08-10.** The recall/trace domain cycle was removed, the obsolete module was
> deleted, and validation passed. This document is retained as the boundary-change record.

## Why

- `domain.recall` references `RetrievalTrace`, while `domain.retrieval_trace` imports
  `RelevanceBreakdown`, producing a two-module domain import cycle.
- Recall result and observability types are pure data contracts and should have one neutral owner.

## Scope

- Move `RelevanceBreakdown` and the retrieval-trace dataclasses into a neutral domain model module.
- Remove the old `menhir.domain.retrieval_trace` module and migrate all in-repo callers to the
  neutral owner; backward compatibility is explicitly out of scope.
- Migrate service and test code to the neutral owner.
- Do not change scoring, candidate filtering, ranking, trace population, or wire serialization.

## Proposed Design

- `domain/retrieval_trace_models.py` owns the shared immutable trace/value contracts and the mutable
  scoring collector.
- `domain/recall.py` imports the result types it embeds from that neutral module.
- Recall, scoring, and tests consume the owning module directly.

## Alternatives Considered

- Keep the `TYPE_CHECKING` back-edge: rejected because it still forms a source dependency cycle.
- Move only `RelevanceBreakdown`: rejected because it would break the cycle but leave closely related
  trace value contracts split across two owners without a clear boundary.

## Risks

- Dataclass field defaults, annotations, equality, and `asdict` output must remain identical.
- Out-of-repository callers using the removed module must migrate to the canonical owner.

## Invariants

- `retrieval_trace_models` is the single owner and import path for trace value contracts.
- `RecallResult.trace` remains optional and trace-off behavior remains unchanged.
- The dependency from `domain.recall` to `domain.retrieval_trace_models` remains one-way.
- Ranking and trace serialization are unchanged.

## Validation

- Architecture checks for one-way imports and single ownership.
- Dataclass field/default and serialized-shape parity checks.
- Focused recall, scoring, trace, context, and formatter tests.
- Full offline suite, with known baseline failures recorded separately.

## Docs To Update

- `.agent/architecture.md`
- `CHANGELOG.md`

## Result

- `retrieval_trace_models.py` is the single owner of `RelevanceBreakdown` and all retrieval-trace
  dataclasses; the old `retrieval_trace.py` module was removed as requested.
- All in-repository production and test imports now use the canonical owner, while the domain barrel
  continues exporting `RelevanceBreakdown` from that owner.
- Focused boundary/trace/scoring validation: 26 passed. Broader recall/context/MCP validation:
  226 passed, 1 known-baseline test deselected.
- Full offline validation: 3,944 passed, 2 skipped, 2 pre-existing failures. The failures remain the
  worktree-name assertion and the stale scalar contract expectation; this change only migrates the
  latter test's `RelevanceBreakdown` import.
