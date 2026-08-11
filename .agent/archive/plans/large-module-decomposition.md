# Large Module Decomposition

> **ARCHIVED 2026-08-10.** All five decomposition slices shipped and completed final validation.
> This document is retained as the implementation record, not an active decomposition queue.

## Why

- Several production modules combine unrelated policies, persistence operations, orchestration, and
  compatibility patches; the largest recall path is 2,532 lines.
- These ownership concentrations make review, testing, and safe changes harder than necessary.
- The previous structural waves established transport/core boundaries; this wave applies the same
  focused-ownership rule inside services and infrastructure.

## Scope

- Split typed-scalar extraction/gating, binding/persistence, and service coordination.
- Decompose recall candidate acquisition, authority/frontier enrichment, and result assembly while
  retaining `RecallService` as the public coordinator.
- Split the SQLite telemetry store by schema/event family and Graphiti patches by compatibility area.
- Split View kinds/query families and typed-assertion activation, reconciliation, and repair storage.
- Split ingest queue/worker processing from intake and lifecycle consolidation/decay/conflict work.
- Preserve public service constructors, wire payloads, persistence queries, feature defaults, logging,
  call ordering, and scheduler behavior.

## Proposed Design

- Keep thin stable facade modules/classes and move implementation into sibling modules named for one
  responsibility.
- Use explicit helper objects or mixins only where repository/service state must remain shared; avoid
  new global state and avoid cross-layer imports.
- Add AST boundary/size tests so the decomposed facades cannot silently absorb implementation again.
- Deliver five stacked draft PRs: scalar perception; recall; telemetry/Graphiti; repositories; then
  ingest/lifecycle.

## Alternatives Considered

- One repository-wide PR: rejected because it would be too large to review or bisect.
- Pure renames or compatibility wrappers around unchanged large implementations: rejected because
  they do not improve ownership.
- Rewrite the pipelines: rejected because this work is structural and must not change behavior.

## Risks

- Existing tests patch private names in facade modules; moving lookup sites can change those seams.
- Moving class methods through mixins can change dataclass field discovery or method resolution.
- SQLite schema initialization and Graphiti monkey-patch ordering are import-order sensitive.
- Recall, ingest, and lifecycle contain time/order-sensitive asynchronous behavior.

## Invariants

- `RecallService`, `IngestService`, `LifecycleService`, `McpTelemetryStore`, `ViewRepository`, and
  `TypedAssertionRepository` keep their current public constructors and return shapes.
- Typed-scalar source keys, temporal disposition, binding, repair, and activation stay identical.
- Graphiti patches remain idempotent and install in the existing order.
- Telemetry remains best-effort for callers and uses the same SQLite schema/data.
- No graph migration, new node/edge type, API/MCP surface, or feature-default change is introduced.

## Validation

- Focused suites for each stack layer before its commit.
- New structural tests for facade responsibilities and module dependency direction.
- Full offline suite at the final stack tip.
- Draft PR bases verified so every review contains only its intended layer.

## Docs To Update

- `.agent/architecture.md`
- `CHANGELOG.md`

## Result

- Delivered the decomposition as five reviewable commits while retaining the existing public
  service and repository entry points.
- Added AST boundary tests that keep the scalar, Graphiti, repository, ingest, and lifecycle
  facades thin and preserve the recall/telemetry ownership limits.
- Final offline validation: `3962 passed, 2 skipped, 152 deselected`.
