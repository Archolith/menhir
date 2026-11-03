---
artifact_schema: 1
artifact_uuid: 8787a0c8-ce2a-4ec4-88b2-1af91f60e53d
artifact_type: plan
artifact_status: IMPLEMENTING
---

# Reconcile local-main MVP safety fixes

## Why

The canonical local `main` diverged from GitHub `main` by six commits. Source retention is already merged, but three independent safety fixes remain absent: claim-fenced transient retry refunds, namespace-scoped conflict resolution, and shared memory-write bounds. Resetting the local branch or freezing an MVP candidate before disposition would lose these protections.

## Scope

Port the three fixes and the deterministic conflict-result ordering change onto a fresh worktree from GitHub `main`. Adapt to current contracts and add focused regression coverage. Preserve local-only JWKS diagnostics in the verified history bundle for later remote-auth review. Do not change the canonical checkout, private deployment, release candidate, or production system.

## Proposed design

- Make transient refund accounting part of the same claim-fenced state transition, using claim start identity in addition to worker owner.
- Pass namespace through conflict resolution from public entry points to the final mutation, readback, and edge bridge. Use the existing tenant-scope predicates and preserve the unscoped internal path.
- Validate memory text and diff at the intake and runtime write boundaries with the previously reviewed shared limits; keep API behavior consistent.
- Sort returned conflict-member UUIDs for stable results.

## Alternatives considered

Cherry-picking all old commits would also replay unrelated changelog and test-fixture edits against 94 newer commits. Porting the specific behavior against current code keeps review and regression evidence tied to current contracts. Deferring all fixes until after release would leave known write and tenant-scope gaps in the MVP.

## Risks and invariants

Claim comparisons must reject stale workers without losing a genuine retry, and should not change non-transient transitions. Scoped conflict resolution must never mutate or reveal another namespace, including mixed legacy groups. Size validation must run before write-side effects on each supported path and must not affect valid payloads. No live Neo4j or paid service is needed for the focused offline checks.

## Validation

Run direct lifecycle, conflict namespace, and ingest write-bound tests plus affected neighboring tests and matching lint/static checks. Let required CI exercise the full suite on the exact pushed SHA before any publication. Document any unavailable live integration coverage.

## Docs to update

Update this plan and index, `CHANGELOG.md`, and contract docs only where behavior changes. Record what remains for the canonical checkout and branch cleanup separately.
