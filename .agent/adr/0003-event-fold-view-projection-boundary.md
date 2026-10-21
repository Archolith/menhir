# ADR 0003 — Event → Fold → View Projection Boundary

- **Status:** ACCEPTED retrospectively (2026-09-21). This records architecture already shipped and
  treated as current across Menhir.
- **Date:** 2026-09-21
- **Deciders:** ctharvey
- **Related:** `.agent/architecture.md`, `.agent/data_models.md`,
  `.agent/reference/fold-algebra.md`, `.agent/memory-aggregation-under-uncertainty.md`,
  `.agent/archive/plans/event-fold-view-architecture.md`

## Context

Menhir originally explored whether counters, timelines, current values, scalar state, and event
history should become separate memory types. At the same time, repeated read-time experiments showed
that fuzzy retrieval could not reliably assemble complete, deduplicated evidence for aggregation.
Asking an LLM to count or reconcile retrieved prose moved deterministic work into the least
reliable part of the system.

The shipped architecture converged on a durable boundary:

```text
immutable, provenance-bearing evidence or events
  -> deterministic fold or reconciliation
  -> disposable, query-sufficient View/projection
  -> recall or a separate authority lane
```

The July Event → Fold → View plan, the fold algebra, and multiple projection implementations now
encode this rule, but the decision remained split across an archived plan and live reference docs.

## Decision drivers

- Derived state must be reproducible from inspectable evidence.
- Arithmetic, ordering, deduplication, and supersession must not vary with model output.
- Current-state lookup should not require reassembling an event set on every read.
- Adding a query class should reuse projection infrastructure rather than create an unrelated
  storage and recall path.
- Erasure, correction, and replay must be able to rebuild or retire dependent state.

## Decision

Menhir uses **Event → Fold → View** as the durable write-side projection boundary.

1. **Evidence and durable typed events are the source of truth.** They retain identity, time, and
   provenance sufficient to audit or replay a projection.
2. **Probabilistic interpretation stops at perception.** An LLM may turn language into a grounded
   typed proposal or event. It does not perform fold arithmetic, event ordering, latest/predecessor
   selection, deduplication, or supersession.
3. **Folds are deterministic.** Batch rebuild and incremental reconciliation are evaluation modes
   of the same declared laws. Non-idempotent reducers re-fold or deduplicate by durable event
   identity; latest/current reducers compare event time rather than arrival order.
4. **Views are additive, rebuildable products.** They are query-sufficient current state or history
   optimized for recall. They never replace the evidence or event log that produced them.
5. **Projection infrastructure is shared; kind contracts may differ.** New capabilities should
   reuse the event/projection boundary, write/query lifecycle, provenance, and supersession
   machinery. They are not required to share one physical Neo4j label or one value slot when their
   domain contracts genuinely differ.
6. **Relative truth is derived at read time.** A value whose meaning changes with the calendar,
   such as “last month,” is resolved from bucketed or timeline state rather than materialized as a
   View that silently becomes false.

## Considered alternatives

### Recompute state during recall

Rejected. Retrieval does not guarantee completeness or deduplication, and repeated model reasoning
adds latency and variance to an operation that can be deterministic.

### Let the LLM perform the fold

Rejected. The model is useful at the language-to-event boundary, not as an arithmetic, replay, or
ordering authority.

### Create a new memory node type and repository for every capability

Rejected as the default. It duplicates recall, supersession, provenance, and lifecycle behavior.
A distinct physical type is justified only when the domain contract cannot honestly fit an
existing projection kind.

### Replace raw evidence with the compact projection

Rejected. It makes correction, audit, alternate query classes, and erasure repair impossible or
unverifiable.

## Consequences

- Recall can answer supported current-state questions from compact Views.
- Projection writers must declare ordering, replay, identity, and completeness behavior.
- Evidence retention and erasure affect projection lifecycle; orphaned current Views fail closed.
- New View kinds carry a higher bar than a new fold over an existing event substrate.
- Model quality affects perception precision, but cannot change deterministic fold semantics.

## Evidence in the repository

- `src/menhir/domain/fold_algebra.py`
- `src/menhir/infrastructure/view_kind_registry.py`
- `src/menhir/infrastructure/view_write_repository.py`
- `src/menhir/services/scalar_state_service.py`
- `src/menhir/services/event_history_service.py`
- `tests/test_view_repository_lww.py`
- `tests/infrastructure/test_view_kind_registration.py`

## Non-goals

This ADR does not approve every proposed View kind, require one universal physical node shape, or
activate a default-off perception/authority path. Those remain separate implementation and rollout
decisions.
