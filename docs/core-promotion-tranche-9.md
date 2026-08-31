# Core promotion tranche 9: live scalar reconciliation

Tranche 9 turns the scalar projection lifecycle from a materialization capability into a recoverable
live reconciliation path.

## Invariant

After a bound typed-scalar assertion mutation is durable, every affected lifecycle slot is either
certified at the current work generation or remains durably pending for recovery. A failed or crashed
materialization must never look fresh.

## Boundary

This tranche introduces the exact-slot reconciliation coordinator over the T5 lifecycle repository and
T8 scalar materializer. It intentionally keeps the legacy entity-wide scalar rebuild available during
cutover and does not perform physical default-namespace migration.

The safe live sequence is:

1. persist the assertion (which already sets the assertion-level projection recovery marker),
2. dirty the exact scalar lifecycle target,
3. fence/materialize/certify that generation,
4. only then may the caller clear its assertion-level projection recovery marker.

A crash between 2 and 3 leaves a `ProjectionWorkState` generation pending. A failure during 3 rolls
back the materialization/certificate transaction and leaves that same generation pending. Recovery
uses `ScalarProjectionReconciler.drain_pending()` and does not advance the generation again.

## Non-goals

- deleting the compatibility entity-wide rebuild path;
- migrating legacy physical namespace aliases;
- changing scalar fold semantics;
- changing assertion persistence identity or authority;
- hiding lifecycle failures by clearing `projection_pending` early.
