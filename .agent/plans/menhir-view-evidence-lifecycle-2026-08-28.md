---
artifact_schema: 1
artifact_uuid: 0775e69f-5551-4bc7-bf7a-8522710d1eac
artifact_type: plan
artifact_status: IMPLEMENTING
---

# View evidence lifecycle invariants

## Why

- Production recall surfaced current and superseded Views whose declared source UUIDs no longer
  resolve to graph evidence.
- Current FACT Views are materialized claims. Their contributing evidence must not disappear through
  normal lifecycle work while the claim remains current.
- Explicit erasure must still win, but it must invalidate dependent projections and remove the erased
  UUID from every retained View instead of leaving an apparently supported claim behind.
- Bootstrap/recent recall currently bypasses the currentness and candidate guards used by scored
  recall, allowing retired, internal, and unapproved Views to surface.

## Scope

In scope:

- FACT View provenance from both `:Episodic` and `:TurnEvidence` inputs.
- Automatic lifecycle retention for evidence contributing to a current FACT View.
- Atomic invalidation/scrubbing of dependent Views during explicit memory or namespace erasure.
- Current/approved/user-facing filters on every generic recall and bootstrap surface.
- A production census and staged reconciliation of existing orphaned Views.

Out of scope:

- Preventing an operator from explicitly erasing a memory or namespace.
- Retaining internal `:Metric` records in semantic recall.
- Re-enabling the currently disabled automatic GONE transition.
- Inventing replacement evidence for existing orphaned Views.

## Proposed Design

### Invariants

1. Every current, recall-eligible FACT View with declared contributors must have live graph evidence
   for every contributor UUID.
2. Every evidence node contributing to a current FACT View is exempt from automatic compression,
   cleanup, and deletion while that View remains current.
3. Every explicit erasure of contributing evidence atomically retires each dependent current View,
   removes the erased UUID from all retained View provenance, and records rebuild work before the
   evidence disappears.
4. Every generic recall surface excludes candidates, superseded/retired Views, internal operational
   Views, and FACT Views that fail live-provenance validation.

Authority: Neo4j graph state (`view_current`, View class/audience, live provenance relationships, and
the explicit erasure transaction). Refusal/repair outcomes are respectively "not lifecycle-eligible"
and "dependent View retired and rebuild queued".

### Evidence anchors

- Extend the existing FACT provenance linker to resolve both `:Episodic` and `:TurnEvidence` UUIDs.
- Keep `episode_uuids` as a deterministic contributor receipt, but derive
  `supporting_event_count` from contributors that resolved at the authoritative write.
- A new current user-facing FACT View must fail closed when a declared contributor is absent. Views
  with no contributors are allowed only when explicitly classified as internal/non-recallable.
- Use graph relationships as the automatic-lifecycle protection authority; do not copy a mutable
  `protected=true` flag onto evidence nodes.

### Erasure and rebuild

- Extend the existing scalar-cascading deletion transaction. Before deleting an evidence node,
  capture dependent current FACT Views, retire them, scrub the erased UUID from every retained
  version, and create durable projection-repair receipts in the same Neo4j transaction.
- Rebuild workers consume those receipts from surviving authoritative inputs. An empty result leaves
  the View retired; it never resurrects the prior value from its own materialized summary.
- Namespace erasure performs the same cross-reference scrub for Views outside the namespace before
  deleting the partition.

### Recall

- Put shared visibility predicates in the repository query layer so MCP, REST, and resources cannot
  drift.
- User-facing bootstrap/recent results require approved scope, current View state, user-facing
  classification, and live evidence when the View declares contributors.
- Scored recall retains its existing defense-in-depth filters and adopts the same shared predicate.

## Alternatives Considered

- **Treat durable UUID strings as sufficient evidence.** Rejected: they prove only that an identifier
  was once copied, not that the source still exists or supports the current claim.
- **Delete all historical Views.** Rejected: supersession history remains useful, provided explicit
  erasure scrubs deleted identities and ordinary recall cannot surface historical versions.
- **Flag every contributing memory permanently.** Rejected: copied flags become stale when Views are
  superseded. A relationship to a current View is the live authority.
- **Fix only `fetch_recent_memories`.** Rejected: that stops disclosure but leaves lifecycle and
  provenance internally inconsistent.

## Risks

- Legacy Views may be hidden or retired during reconciliation because their source evidence is gone.
- TurnEvidence and Episodic inputs currently use different storage/lifecycle paths; a partial rollout
  could protect one but not the other.
- Explicit erasure must remain privacy-correct and crash-safe; protection can never become a refusal
  of an authorized erasure.
- Mixed deployed versions could continue creating UUID-only provenance until every writer is drained.

## Validation

- Structural census test for every graph path that deletes `:Episodic`, `:TurnEvidence`, or a whole
  namespace.
- Unit tests for current-View lifecycle exemption and noncurrent-View non-exemption.
- Repository tests proving recent/bootstrap queries reject candidate, retired, internal, and
  orphaned Views.
- Live Neo4j tests for Episodic and TurnEvidence provenance, explicit deletion, namespace deletion,
  empty rebuild, and concurrent View write versus deletion.
- Read-only production census before backfill; staged reconciliation with before-image export and no
  automatic invention of evidence.

## Implementation Status (2026-08-28)

Implemented and verified locally (not deployed):

- Shared fail-closed visibility on recent, flagged/bootstrap, scope, type, and scored recall paths;
  direct UUID inspection remains unfiltered for audit.
- Atomic FACT View writes resolve every contributor before superseding/creating and link both
  `:Episodic` and `:TurnEvidence` evidence in the same statement.
- Generic memory decay/compression/deletion excludes all derived View shapes, leaving View lifetime
  to fold/projection logic.
- Memory, namespace, and direct TurnEvidence erasure retire current dependents, scrub retained
  receipts, and reset counter/scalar/event watermarks in the evidence-deletion transaction.
- Focused invariant and regression coverage for missing evidence, recall filtering, automatic
  lifecycle exclusion, and every exposed erasure seam. The focused unit suite passes 305 tests;
  disposable Neo4j runs pass 6 FACT-provenance and 7 scalar/namespace-erasure tests.

Still required before this plan is terminal:

- Provide and activate the HMAC replay-tombstone key ring and producer checks; current tombstone DDL
  is not operational enforcement.
- Provide Graphiti's complete created-artifact manifest and runtime-inject the publication-intent
  reconciler; current publication recovery is fail-closed scaffolding, not an always-on protocol.
- Register the generic View repair dispatcher under an independent always-on runtime lease.
- Run the remaining live concurrent View-write versus evidence-erasure matrix.
- Activate the optional schema only as part of a coordinated writer migration; it has not run.
- Deploy all writers together, then hold the invalid-current-View census at zero through a full
  scheduler cycle.
- Export and reconcile existing production orphaned Views; no source evidence may be invented.

Production behavior is unchanged because no deployment, schema activation, census mutation,
reconciliation, or backfill was performed.

## Rollout

1. Expand readers and writers to understand both evidence labels and shared visibility predicates.
2. Deploy all writers; hold the missing-contributor count and UUID-only-write count at zero through a
   scheduler cycle.
3. Reconcile existing Views: relink resolvable evidence, retire orphaned current Views, scrub known
   erased UUIDs, and preserve a before-image.
4. Enforce fail-closed current FACT creation and atomic erasure invalidation.
5. Remove legacy permissive behavior after telemetry confirms it is unused.

## Docs To Update

- `.agent/data_models.md`
- `.agent/memory-policy.md`
- `.agent/architecture.md`
- `.agent/endpoints.md`
- `CHANGELOG.md`
