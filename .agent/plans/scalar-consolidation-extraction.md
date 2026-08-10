# Scalar Consolidation Extraction

## Why

- `scheduler_tasks.consolidate_personal_memory` currently contains both counter consolidation and the
  complete typed-scalar backfill/repair pipeline.
- Scalar paging, activation, repair, and reconciliation change independently from scheduler job
  registration and counter perception, making the 800-line task module a risky ownership boundary.

## Scope

- Move scalar dirty-target selection and the full scalar consolidation pass into a focused service
  module.
- Keep `consolidate_personal_memory` as the stable scheduler entry point and preserve its result shape.
- Do not change scalar activation, cursor semantics, counter/scalar reconciliation, repair ordering,
  feature flags, defaults, or persistence behavior.

## Proposed Design

- `services/scalar_consolidation.py` owns scalar target selection, paged typed perception, cursor
  advancement, duplicate-counter retirement, and all scalar repair passes.
- A small immutable config groups scalar-only version, batch, repair, lineage, and embedding settings.
- The scheduler selects the counter targets, creates one call-counting LLM wrapper, runs the counter
  pass, then gives that same wrapper and the remaining budget to the scalar runner.
- Scalar results remain additive only when `enable_scalar_state=True`; flag-off output stays identical.

## Alternatives Considered

- Move the entire personal-memory job at once: deferred because counter consolidation is a separate,
  higher-risk responsibility and the requested seam is scalar-only.
- Give scalar consolidation its own call counter: rejected because it would silently double the
  configured per-run budget.

## Risks

- Reordering dirty-target selection could alter which concurrent episodes enter a run.
- Cursor advancement or repair ordering drift could skip work or repeatedly process one page.
- Moving lazy imports could change activation behavior or test patch points.

## Invariants

- Scalar targets are selected before the blocking counter/scalar worker begins, as today.
- Counter and scalar phases share one invocation counter and one `call_budget`.
- Scalar activation failure disables only scalar work and leaves namespaces dirty.
- Cursor advancement, retirement, pending-binding, deletion, lineage, and orphan repair order is
  unchanged.
- Existing scheduler API, defaults, audit shape, and result keys remain unchanged.

## Validation

- Existing personal-memory and scalar cursor suites.
- New architecture checks proving scheduler tasks delegate scalar behavior.
- Full offline test suite.

## Docs To Update

- `.agent/architecture.md`
- `CHANGELOG.md`

## Result

- Added `services/scalar_consolidation.py` as the owner of typed-scalar target selection,
  activation, paging, cursor advancement, counter retirement, lineage, and repair work.
- Reduced `scheduler_tasks.py` from 815 to 659 lines while preserving its public API, result keys,
  target-snapshot timing, and the shared counter/scalar LLM budget.
- Added a scheduler boundary test and a behavior test proving the scalar phase receives the counter
  phase's call counter.
- Focused scalar validation: 390 passed.
- Full offline validation: 3,954 passed, 2 skipped, 152 deselected.
