"""Activation-gated schema DDL families.

Each family here is created ONLY by its gated activation path after that path
validates and reconciles existing state — never by the unconditional phase-one
bootstrap (:func:`menhir.infrastructure.schema.get_phase1_bootstrap_queries`).
Re-exported from :mod:`menhir.infrastructure.schema` so the original import path
keeps working unchanged.
"""

from __future__ import annotations

#: constraint/index names backing the ScalarStateView typed-assertion store. Feature-scoped: these
#: are created ONLY by the gated scalar-state activation path (get_scalar_state_activation_queries,
#: run behind `assert_scalar_state_activatable`) — NOT by the unconditional bootstrap and NOT in
#: PHASE_ONE_REQUIRED_INDEXES. These constraints define the source_key-anchored identity space, so
#: creating them silently over a legacy (v1, claim_key-anchored) store would mix identity spaces;
#: activation therefore refuses any legacy node first. `scalar_state_schema_ready()` checks them when
#: the feature is enabled, so a scalar-state deploy still gates on its own DDL being online.
SCALAR_STATE_REQUIRED_INDEXES: tuple[str, ...] = (
    "typed_assertion_key_unique",
    "typed_assertion_id_unique",
    "typed_assertion_subject_uuid_idx",
    "typed_assertion_claim_key_idx",
    "typed_assertion_source_key_idx",
    "typed_assertion_episode_idx",
    "typed_assertion_head_source_key_unique",
    "assertion_rebind_key_unique",
    "assertion_rebind_op_idx",
    "scalar_reconcile_receipt_key_unique",
    "scalar_reconcile_op_idx",
    "scalar_projection_repair_key_unique",
    "scalar_projection_repair_pending_idx",
    "scalar_state_view_current_key_unique",
)


def get_scalar_state_activation_queries() -> list[str]:
    """Indexes/constraints backing the durable :TypedAssertion event log + its per-claim head
    (ScalarStateView Piece C). These are DELIBERATELY NOT in `get_phase1_bootstrap_queries()`: they
    define the source_key-anchored identity space (head unique on source_key; assertion_key built
    from source_key), so creating them over a legacy (v1, claim_key-anchored) store would silently
    mix identity spaces. They are created ONLY by the gated activation path
    (`MemoryGraphAdapter.activate_scalar_state`), which first refuses any legacy/unstamped node via
    `assert_scalar_state_activatable`. Fresh-only: after a clean activation every node carries
    `identity_version = IDENTITY_VERSION`, so the two identity spaces never coexist.

    assertion_key is the idempotency merge key; assertion_id is a unique node id;
    subject_uuid/claim_key/source_key/episode back the per-entity fold input and provenance lookups;
    the head's source_key is unique (one current per source claim). The leading DROP removes the
    superseded v1 head claim_key uniqueness constraint if a legacy deploy still has it online (the
    head is now source_key-keyed; claim_key is a non-unique historical property)."""
    return [
        # retire the superseded v1 identity constraint (head was claim_key-unique) if still online.
        "DROP CONSTRAINT typed_assertion_head_claim_key_unique IF EXISTS",
        # superseded by the namespace-keyed receipt identity (C.4.4): one op may hold one receipt PER
        # namespace, so operation_id must NOT be unique.
        "DROP CONSTRAINT scalar_reconcile_op_unique IF EXISTS",
        "CREATE CONSTRAINT typed_assertion_key_unique IF NOT EXISTS FOR (a:TypedAssertion) REQUIRE a.assertion_key IS UNIQUE",
        "CREATE CONSTRAINT typed_assertion_id_unique IF NOT EXISTS FOR (a:TypedAssertion) REQUIRE a.assertion_id IS UNIQUE",
        "CREATE INDEX typed_assertion_subject_uuid_idx IF NOT EXISTS FOR (a:TypedAssertion) ON (a.subject_uuid)",
        "CREATE INDEX typed_assertion_claim_key_idx IF NOT EXISTS FOR (a:TypedAssertion) ON (a.claim_key)",
        "CREATE INDEX typed_assertion_source_key_idx IF NOT EXISTS FOR (a:TypedAssertion) ON (a.source_key)",
        "CREATE INDEX typed_assertion_episode_idx IF NOT EXISTS FOR (a:TypedAssertion) ON (a.episode_uuid)",
        # the head's ATOMIC identity is the binding-stable source_key (DB-enforced), so two concurrent
        # first writes for one source claim — even bound through different subject_uuids after a merge
        # — converge on one head. claim_key is a non-unique historical property (index only).
        "CREATE CONSTRAINT typed_assertion_head_source_key_unique IF NOT EXISTS FOR (h:TypedAssertionHead) REQUIRE h.source_key IS UNIQUE",
        "CREATE INDEX typed_assertion_head_claim_key_idx IF NOT EXISTS FOR (h:TypedAssertionHead) ON (h.claim_key)",
        # merge-lineage journal (DB-unique per op+assertion) + reconciliation receipts (Piece C.3)
        "CREATE CONSTRAINT assertion_rebind_key_unique IF NOT EXISTS FOR (r:AssertionRebind) REQUIRE r.rebind_key IS UNIQUE",
        "CREATE INDEX assertion_rebind_op_idx IF NOT EXISTS FOR (r:AssertionRebind) ON (r.merge_op_id)",
        # Reconciliation receipts are NAMESPACE-KEYED (C.4.4): one lifecycle op can span two assertion
        # silos and is repaired independently per silo, so identity is (operation_id, kind, namespace)
        # hashed into receipt_key. operation_id is a non-unique lookup property (index only) — a
        # UNIQUE constraint on it would collapse the per-namespace receipts and let one silo's success
        # certify another silo's failure.
        "CREATE CONSTRAINT scalar_reconcile_receipt_key_unique IF NOT EXISTS FOR (rc:ScalarReconcile) REQUIRE rc.receipt_key IS UNIQUE",
        "CREATE INDEX scalar_reconcile_op_idx IF NOT EXISTS FOR (rc:ScalarReconcile) ON (rc.operation_id)",
        # Delete/time-activation projection receipts: DB-unique replay identity plus the scheduler's
        # pending FIFO access path (G19/G20).
        "CREATE CONSTRAINT scalar_projection_repair_key_unique IF NOT EXISTS FOR (rr:ScalarProjectionRepair) REQUIRE rr.repair_key IS UNIQUE",
        "CREATE INDEX scalar_projection_repair_pending_idx IF NOT EXISTS FOR (rr:ScalarProjectionRepair) ON (rr.status, rr.started_at)",
        # ONE current scalar_state View per view_key, DB-ENFORCED (C.4.4.4). The View writer reads the
        # current version then CREATEs a new one with a random uuid; under read-committed isolation two
        # independent workers rebuilding the same projection both read "no current" and each create a
        # view_current=true node -> duplicate current Views for one slot. No query-level check-then-create
        # can prevent that; only a DB constraint does. `ss_view_key_current` is set to the view_key ONLY
        # while a scalar_state node is current, and REMOVED on supersession/retire, so the uniqueness
        # boundary is exactly "one current per key". The property is NULL on every non-scalar fact, every
        # Metric, and every superseded/retired scalar node, so those never participate (the fingerprinted
        # metric saga and all other View kinds are untouched). Backfill current scalar nodes FIRST so the
        # constraint can come online over an existing single-current store.
        "MATCH (n:Entity {view_kind: 'scalar_state'}) WHERE coalesce(n.view_current, true) AND n.ss_view_key_current IS NULL SET n.ss_view_key_current = n.view_key",
        "CREATE CONSTRAINT scalar_state_view_current_key_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.ss_view_key_current IS UNIQUE",
    ]


def get_view_evidence_lifecycle_activation_queries() -> list[str]:
    """Return optional DDL for the activation-gated View evidence lifecycle.

    These constraints and indexes support the namespace serialization fence, durable publication
    intents and tombstones, and leased projection repair queue.  They are deliberately separate
    from :func:`get_phase1_bootstrap_queries`: activation must first validate and reconcile existing
    evidence/View state before uniqueness becomes authoritative.
    """
    return [
        "CREATE CONSTRAINT evidence_namespace_fence_namespace_unique IF NOT EXISTS "
        "FOR (f:EvidenceNamespaceFence) REQUIRE f.namespace_key IS UNIQUE",
        "CREATE CONSTRAINT evidence_publication_intent_key_unique IF NOT EXISTS "
        "FOR (i:EvidencePublicationIntent) REQUIRE i.intent_key IS UNIQUE",
        "CREATE INDEX evidence_publication_intent_status_idx IF NOT EXISTS "
        "FOR (i:EvidencePublicationIntent) ON (i.status)",
        "CREATE CONSTRAINT evidence_tombstone_key_unique IF NOT EXISTS "
        "FOR (t:EvidenceTombstone) REQUIRE t.tombstone_key IS UNIQUE",
        "CREATE INDEX evidence_tombstone_digest_idx IF NOT EXISTS "
        "FOR (t:EvidenceTombstone) ON (t.digest)",
        "CREATE INDEX evidence_tombstone_key_id_idx IF NOT EXISTS "
        "FOR (t:EvidenceTombstone) ON (t.key_id)",
        "CREATE CONSTRAINT view_projection_repair_key_unique IF NOT EXISTS "
        "FOR (r:ViewProjectionRepair) REQUIRE r.repair_key IS UNIQUE",
        "CREATE INDEX view_projection_repair_status_lease_idx IF NOT EXISTS "
        "FOR (r:ViewProjectionRepair) ON (r.status, r.lease_expires_at)",
    ]
