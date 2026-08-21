"""Phase-1 schema and bootstrap queries for memory graph metadata.

Milestone 1 requires the Graphiti-backed store to include policy fields for node and
edge scoring, lifecycle, and conflict governance. This module keeps those migration
queries centralised for easier reuse and testability.
"""

from __future__ import annotations


MEMORY_NODE_LABELS = ("Entity", "Episodic")
EDGE_LABELS = ("RELATES_TO", "NEXT_EPISODE", "HAS_MEMBER", "HAS_EPISODE", "MENTIONS")

# L4 institutional artifacts (domain/artifacts.py; plan .agent/plans/l4-artifact-loop-v0.md).
# Artifacts are :Entity nodes carrying artifact_* fields; Evidence is a FIRST-CLASS node
# linked by SUPPORTED_BY. Evidence is deliberately NOT in MEMORY_NODE_LABELS — it must not
# inherit the full Entity backfill (type/scope/freshness/...); it only needs its own indexes.
ARTIFACT_NODE_LABELS = ("Evidence",)
ARTIFACT_EDGE_LABELS = ("SUPPORTED_BY", "SUPERSEDES")

# Bump this when adding new fields to _node_defaults_queries or _edge_defaults_queries.
# Nodes/edges stamped with an older version will be re-backfilled on next startup.
_SCHEMA_V = 1

ARTIFACT_RECONCILIATION_REQUIRED_CONSTRAINTS = (
    "work_artifact_uuid_unique",
    "artifact_source_uuid_unique",
    "artifact_source_locator_unique",
    "artifact_reconcile_cursor_repository_unique",
)


def get_artifact_reconciliation_schema_queries() -> list[str]:
    """DDL activated only after the source-v2 preparation preflight passes."""
    return [
        "DROP INDEX work_artifact_uuid_idx IF EXISTS",
        "CREATE CONSTRAINT work_artifact_uuid_unique IF NOT EXISTS "
        "FOR (n:WorkArtifact) REQUIRE n.artifact_uuid IS UNIQUE",
        "CREATE CONSTRAINT artifact_source_uuid_unique IF NOT EXISTS "
        "FOR (n:ArtifactSource) REQUIRE n.source_uuid IS UNIQUE",
        "CREATE CONSTRAINT artifact_source_locator_unique IF NOT EXISTS "
        "FOR (n:ArtifactSource) REQUIRE n.current_locator_key IS UNIQUE",
        "CREATE CONSTRAINT artifact_reconcile_cursor_repository_unique IF NOT EXISTS "
        "FOR (n:ArtifactReconciliationCursor) REQUIRE n.repository IS UNIQUE",
    ]

PHASE_ONE_REQUIRED_INDEXES = (
    "entity_type_idx",
    "entity_scope_idx",
    "episodic_type_idx",
    "episodic_scope_idx",
    "episodic_processing_state_idx",
    "entity_bootstrap_scope_idx",
    "episodic_bootstrap_scope_idx",
    "episode_uuid",
    "episode_group_id",
    "episode_content",
    # L4 artifact indexes — included so an existing install (core indexes already online)
    # still reports schema_not_ready until these exist and gets them bootstrapped.
    "entity_artifact_id_idx",
    "entity_is_artifact_idx",
    "entity_artifact_status_idx",
    "evidence_artifact_id_idx",
    "evidence_uuid_idx",
    # View primitive indexes — supersession lookup + current-version filters.
    "entity_view_key_idx",
    # CF-112: the supersession lookup is a DISJUNCTION over (view_key OR qs_key). A
    # disjunction needs EVERY branch indexed or the planner unions the seek with a full
    # label scan -- measured at 61,005 dbHits vs 4 on 20,500 :Entity nodes, with no early
    # exit from LIMIT 1. Required, not merely created, so an existing install reports
    # schema_not_ready until the index that makes the write path cheap actually exists.
    "entity_qs_key_idx",
    "entity_view_kind_idx",
    "entity_view_current_idx",
    # Entity-anchored scalar_state identity (ScalarStateView Piece B) — required so an existing
    # install (older view indexes already online) still reports schema_not_ready until it exists.
    "entity_view_subject_uuid_idx",
    # Metric class indexes — instrumentation Views under the :Metric label (Metric plan A5).
    "metric_view_key_idx",
    "metric_qs_key_idx",
    "metric_view_kind_idx",
    "metric_view_current_idx",
    "metric_source_idx",
    "metric_receipt_op_idx",
)


def _node_index_queries() -> list[str]:
    queries: list[str] = []
    for node_label in MEMORY_NODE_LABELS:
        label_suffix = node_label.lower()
        queries.extend(
            [
                f"CREATE INDEX {label_suffix}_type_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.type)",
                f"CREATE INDEX {label_suffix}_scope_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.scope)",
                f"CREATE INDEX {label_suffix}_namespace_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.namespace)",
                f"CREATE INDEX {label_suffix}_freshness_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.freshness)",
                f"CREATE INDEX {label_suffix}_source_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.source)",
                f"CREATE INDEX {label_suffix}_source_conf_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.source_confidence)",
                f"CREATE INDEX {label_suffix}_user_flagged_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.user_flagged)",
                f"CREATE INDEX {label_suffix}_bootstrap_scope_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.bootstrap_scope)",
                f"CREATE INDEX {label_suffix}_created_at_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.created_at)",
                f"CREATE INDEX {label_suffix}_last_accessed_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.last_accessed)",
                f"CREATE INDEX {label_suffix}_user_id_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.user_id)",
                f"CREATE INDEX {label_suffix}_session_id_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.session_id)",
                f"CREATE INDEX {label_suffix}_sharpness_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.sharpness)",
                f"CREATE INDEX {label_suffix}_edge_count_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.edge_count)",
                f"CREATE INDEX {label_suffix}_conflict_group_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.conflict_group_id)",
                f"CREATE INDEX {label_suffix}_conflict_status_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.conflict_status)",
                f"CREATE INDEX {label_suffix}_conflict_created_idx IF NOT EXISTS FOR (n:{node_label}) ON (n.conflict_created_at)",
                f"CREATE INDEX {label_suffix}_menhir_schema_v_idx IF NOT EXISTS FOR (n:{node_label}) ON (n._menhir_schema_v)",
            ]
        )
    queries.extend(
        [
            "CREATE INDEX episodic_processing_state_idx IF NOT EXISTS FOR (n:Episodic) ON (n.processing_state)",
            "CREATE INDEX episodic_processing_owner_idx IF NOT EXISTS FOR (n:Episodic) ON (n.processing_owner)",
            "CREATE INDEX episodic_processing_lease_expires_idx IF NOT EXISTS FOR (n:Episodic) ON (n.processing_lease_expires_at)",
            "CREATE INDEX episodic_queued_at_idx IF NOT EXISTS FOR (n:Episodic) ON (n.queued_at)",
            "CREATE INDEX episodic_resolved_episode_uuid_idx IF NOT EXISTS FOR (n:Episodic) ON (n.resolved_episode_uuid)",
            # Dirty-namespace detection scans :Episodic with a content STARTS WITH predicate on
            # every consolidation request and scheduler cycle; a plain index backs prefix search.
            "CREATE INDEX episodic_content_idx IF NOT EXISTS FOR (n:Episodic) ON (n.content)",
            # Raw captures are MERGEd by this property once per exhausted episode during the
            # terminal-failure sweep. Without the index that MERGE is a full :Entity label scan
            # per episode.
            "CREATE INDEX entity_raw_capture_for_idx IF NOT EXISTS FOR (n:Entity) ON (n.raw_capture_for)",
            # Hook Center stale-anchor labelling runs on EVERY recall and file edit, filtering
            # :Entity by role/path/project. Without these each is a full :Entity label scan.
            # The composite matches project AND path together and is the one structural lookups use.
            "CREATE INDEX entity_structure_role_idx IF NOT EXISTS FOR (n:Entity) ON (n.structure_role)",
            "CREATE INDEX entity_structure_path_idx IF NOT EXISTS FOR (n:Entity) ON (n.structure_path)",
            "CREATE INDEX entity_structure_project_path_idx IF NOT EXISTS FOR (n:Entity) ON (n.structure_project, n.structure_path)",
            # CF-176(a): `resolve_structural_entities` matches structure_path as a DISJUNCTION --
            # `= candidate` OR `ENDS WITH '/' + candidate`. The RANGE index above serves only the
            # equality branch; a disjunction needs EVERY branch indexed or the planner falls back to
            # pulling every structural node and filtering. ENDS WITH needs a TEXT index, so both
            # index kinds coexist on this one property and the planner picks per branch.
            # Measured: 4,006 -> 5 dbHits (2,000 structural nodes among 22,003 :Entity).
            "CREATE TEXT INDEX entity_structure_path_text_idx IF NOT EXISTS FOR (n:Entity) ON (n.structure_path)",
            # CF-176(b): `list_verifiers` is `MATCH (v:Entity {is_verifier: true})` with no LIMIT.
            # Unindexed that is a full :Entity label scan on the highest-cardinality label to find a
            # handful of verifiers. Measured: 44,013 -> 7 dbHits for 3 verifiers among 22,003.
            "CREATE INDEX entity_is_verifier_idx IF NOT EXISTS FOR (n:Entity) ON (n.is_verifier)",
        ]
    )
    # :Todo and its owned :TodoLocation value objects. Before these, :Todo had no
    # index at all and every todo read was a label scan. The path index is the one
    # blast radius depends on: PROFILE shows NodeIndexSeek on TodoLocation(path)
    # rather than a scan. The composite (project, path) is kept for project-scoped
    # lookups, but the planner prefers the single-property index for the
    # blast-radius shape -- measured, not assumed.
    queries.extend(
        [
            "CREATE INDEX todo_namespace_status_idx IF NOT EXISTS FOR (n:Todo) ON (n.namespace, n.status)",
            "CREATE INDEX todo_uuid_idx IF NOT EXISTS FOR (n:Todo) ON (n.uuid)",
            "CREATE INDEX todo_location_path_idx IF NOT EXISTS FOR (n:TodoLocation) ON (n.path)",
            "CREATE INDEX todo_location_project_path_idx IF NOT EXISTS FOR (n:TodoLocation) ON (n.project, n.path)",
        ]
    )
    # :WorkArtifact and its owned subordinates. Same reasoning as :Todo above --
    # without these every artifact read is a label scan.
    queries.extend(
        [
            "CREATE INDEX work_artifact_type_status_idx IF NOT EXISTS FOR (n:WorkArtifact) ON (n.artifact_type, n.status)",
            "CREATE INDEX work_artifact_namespace_idx IF NOT EXISTS FOR (n:WorkArtifact) ON (n.namespace)",
            "CREATE INDEX artifact_location_path_idx IF NOT EXISTS FOR (n:ArtifactLocation) ON (n.path)",
            "CREATE INDEX artifact_source_medium_idx IF NOT EXISTS FOR (n:ArtifactSource) ON (n.medium)",
            # Reconciliation identity. artifact_uuid and source_uuid are unique
            # because a duplicate of either would let one document wear two
            # identities; current_locator_key is unique because two artifacts
            # claiming one path is the state that makes "which plan lives here?"
            # unanswerable. The key is nullable by design -- an unresolved source
            # keeps its last known locator and does not block the destination.
            # The UUID uniqueness constraint owns the range index for artifact_uuid.
            # Retire the pre-reconciliation plain index first so existing installs
            # can migrate; Neo4j will not create a constraint over the same schema.
            *get_artifact_reconciliation_schema_queries(),
            "CREATE INDEX artifact_source_lane_idx IF NOT EXISTS FOR (n:ArtifactSource) ON (n.corpus_lane)",
            "CREATE INDEX artifact_source_resolution_idx IF NOT EXISTS "
            "FOR (n:ArtifactSource) ON (n.resolution_status)",
            "CREATE INDEX open_question_uuid_idx IF NOT EXISTS FOR (n:OpenQuestion) ON (n.question_uuid)",
            "CREATE INDEX artifact_declaration_uuid_idx IF NOT EXISTS FOR (n:ArtifactDeclaration) ON (n.declaration_uuid)",
            # Resolution sweeps filter on status across every declaration in the
            # graph, so this one carries the retry pass rather than a point read.
            "CREATE INDEX artifact_declaration_status_idx IF NOT EXISTS FOR (n:ArtifactDeclaration) ON (n.resolution_status)",
        ]
    )
    return queries


def _artifact_index_queries() -> list[str]:
    """Indexes backing the L4 artifact loop: the artifact_id idempotency/lookup key, the
    is_artifact filter used by find_artifacts, the status filter, and the first-class
    :Evidence node keys (artifact_id is the SUPPORTED_BY dedup key, uuid the identity)."""
    return [
        "CREATE INDEX entity_artifact_id_idx IF NOT EXISTS FOR (n:Entity) ON (n.artifact_id)",
        "CREATE INDEX entity_is_artifact_idx IF NOT EXISTS FOR (n:Entity) ON (n.is_artifact)",
        "CREATE INDEX entity_artifact_status_idx IF NOT EXISTS FOR (n:Entity) ON (n.artifact_status)",
        "CREATE INDEX evidence_artifact_id_idx IF NOT EXISTS FOR (n:Evidence) ON (n.artifact_id)",
        "CREATE INDEX evidence_uuid_idx IF NOT EXISTS FOR (n:Evidence) ON (n.uuid)",
    ]


def _view_index_queries() -> list[str]:
    """Indexes backing the View primitives (Event -> Fold -> View). view_key is the
    supersession lookup in ViewRepository._current_by_key; view_kind + view_current back
    the _fetch_current / list_views current-version filters. Indexes only — View nodes
    already carry these props, so no node backfill (no _SCHEMA_V bump)."""
    return [
        "CREATE INDEX entity_view_key_idx IF NOT EXISTS FOR (n:Entity) ON (n.view_key)",
        # CF-112: qs_key is the backward-compatibility branch of the supersession
        # disjunction in ViewRepository._current_by_key. Unindexed, it forced a full
        # :Entity scan on EVERY View write.
        "CREATE INDEX entity_qs_key_idx IF NOT EXISTS FOR (n:Entity) ON (n.qs_key)",
        "CREATE INDEX entity_view_kind_idx IF NOT EXISTS FOR (n:Entity) ON (n.view_kind)",
        "CREATE INDEX entity_view_current_idx IF NOT EXISTS FOR (n:Entity) ON (n.view_current)",
        # entity-anchored scalar_state Views key on the resolved UUID; index it so per-entity
        # lookups and the recall overlap proof are not scans (ScalarStateView plan, Piece B).
        "CREATE INDEX entity_view_subject_uuid_idx IF NOT EXISTS FOR (n:Entity) ON (n.view_subject_uuid)",
    ]


def _metric_index_queries() -> list[str]:
    """Indexes/constraint backing the :Metric class (instrumentation Views, Metric plan A5).

    Metrics share the View machinery but live under a distinct label so they are excluded from
    semantic recall by construction. These mirror the :Entity view indexes plus a source index
    (operator filtering) and a receipt-op index (join to metric_receipts). Additive: :Metric
    nodes are new, so no backfill and no _SCHEMA_V bump."""
    return [
        "CREATE CONSTRAINT metric_uuid_unique IF NOT EXISTS FOR (n:Metric) REQUIRE n.uuid IS UNIQUE",
        "CREATE INDEX metric_view_key_idx IF NOT EXISTS FOR (n:Metric) ON (n.view_key)",
        "CREATE INDEX metric_qs_key_idx IF NOT EXISTS FOR (n:Metric) ON (n.qs_key)",
        "CREATE INDEX metric_view_kind_idx IF NOT EXISTS FOR (n:Metric) ON (n.view_kind)",
        "CREATE INDEX metric_view_current_idx IF NOT EXISTS FOR (n:Metric) ON (n.view_current)",
        "CREATE INDEX metric_source_idx IF NOT EXISTS FOR (n:Metric) ON (n.source)",
        "CREATE INDEX metric_receipt_op_idx IF NOT EXISTS FOR (n:Metric) ON (n.metric_last_receipt_op_id)",
    ]


def _turn_evidence_index_queries() -> list[str]:
    """Indexes/constraint backing selective :TurnEvidence capture (ADR 0001). turn_key is the
    idempotency merge key; namespace/role/recorded_at back the Phase 3 dirty query and loader; turn_id
    backs the G14 grounding anchor lookup (every assertion write OPTIONAL MATCHes a :TurnEvidence by
    turn_id, so it must not label-scan).
    Additive — TurnEvidence nodes are new, so no backfill and no _SCHEMA_V bump."""
    return [
        "CREATE CONSTRAINT turn_evidence_key_unique IF NOT EXISTS FOR (t:TurnEvidence) REQUIRE t.turn_key IS UNIQUE",
        "CREATE INDEX turn_evidence_turn_id_idx IF NOT EXISTS FOR (t:TurnEvidence) ON (t.turn_id)",
        "CREATE INDEX turn_evidence_namespace_idx IF NOT EXISTS FOR (t:TurnEvidence) ON (t.namespace)",
        "CREATE INDEX turn_evidence_role_idx IF NOT EXISTS FOR (t:TurnEvidence) ON (t.role)",
        "CREATE INDEX turn_evidence_recorded_at_idx IF NOT EXISTS FOR (t:TurnEvidence) ON (t.recorded_at)",
        "CREATE INDEX turn_evidence_session_idx IF NOT EXISTS FOR (t:TurnEvidence) ON (t.session_id)",
    ]


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


def _edge_index_queries() -> list[str]:
    queries: list[str] = []
    for edge_label in EDGE_LABELS:
        edge_suffix = edge_label.lower()
        queries.extend(
            [
                f"CREATE INDEX {edge_suffix}_type_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.type)",
                f"CREATE INDEX {edge_suffix}_weight_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.weight)",
                f"CREATE INDEX {edge_suffix}_source_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.source)",
                f"CREATE INDEX {edge_suffix}_scope_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.scope)",
                f"CREATE INDEX {edge_suffix}_created_at_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.created_at)",
                f"CREATE INDEX {edge_suffix}_last_traversed_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r.last_traversed)",
                f"CREATE INDEX {edge_suffix}_menhir_schema_v_idx IF NOT EXISTS FOR ()-[r:{edge_label}]-() ON (r._menhir_schema_v)",
            ]
        )
    return queries


def _node_defaults_queries() -> list[str]:
    return [
        f"""
        MATCH (n:Entity)
        WHERE n._menhir_schema_v IS NULL OR n._menhir_schema_v < {_SCHEMA_V}
        SET n.type = coalesce(n.type, 'SEMANTIC'),
            n.scope = coalesce(n.scope, 'SESSION'),
            n.freshness = CASE
                WHEN coalesce(n.scope, 'SESSION') = 'SESSION' THEN n.freshness
                ELSE coalesce(n.freshness, 'ACTIVE')
            END,
            n.source = coalesce(n.source, 'system-inferred'),
            n.source_confidence = coalesce(toFloat(n.source_confidence), 0.5),
            n.user_flagged = coalesce(n.user_flagged, false),
            n.session_id = coalesce(n.session_id, randomUuid()),
            n.user_id = coalesce(n.user_id, 'default'),
            n.created_at = coalesce(n.created_at, datetime()),
            n.last_accessed = coalesce(n.last_accessed, n.created_at),
            n.sharpness = coalesce(toFloat(n.sharpness), 0.0),
            n.edge_count = coalesce(toInteger(n.edge_count), 0),
            n.rehydration_count = coalesce(toInteger(n.rehydration_count), 0),
            n.emotions = coalesce(n.emotions, []),
            n.conflict_created_at = CASE
                WHEN n.conflict_created_at IS NULL AND n.conflict_group_id IS NOT NULL
                THEN datetime()
                ELSE n.conflict_created_at
            END,
            n._menhir_schema_v = {_SCHEMA_V}
        """,
        f"""
        MATCH (n:Episodic)
        WHERE n._menhir_schema_v IS NULL OR n._menhir_schema_v < {_SCHEMA_V}
        SET n.type = coalesce(n.type, 'EPISODIC'),
            n.scope = coalesce(n.scope, 'SESSION'),
            n.freshness = CASE
                WHEN coalesce(n.scope, 'SESSION') = 'SESSION' THEN n.freshness
                ELSE coalesce(n.freshness, 'ACTIVE')
            END,
            n.source = coalesce(n.source, 'system-inferred'),
            n.source_confidence = coalesce(toFloat(n.source_confidence), 0.5),
            n.user_flagged = coalesce(n.user_flagged, false),
            n.session_id = coalesce(n.session_id, randomUuid()),
            n.user_id = coalesce(n.user_id, 'default'),
            n.created_at = coalesce(n.created_at, datetime()),
            n.last_accessed = coalesce(n.last_accessed, n.created_at),
            n.sharpness = coalesce(toFloat(n.sharpness), 0.0),
            n.edge_count = coalesce(toInteger(n.edge_count), 0),
            n.processing_state = coalesce(n.processing_state, 'READY'),
            n.processing_stage = coalesce(n.processing_stage, CASE
                WHEN n.processing_state = 'PENDING' THEN 'queued'
                WHEN n.processing_state = 'ENRICHING' THEN 'processing'
                WHEN n.processing_state = 'READY' THEN 'ready'
                WHEN n.processing_state = 'FAILED' THEN 'failed'
                ELSE 'unknown'
            END),
            n.processing_progress = coalesce(toFloat(n.processing_progress), CASE
                WHEN n.processing_state IN ['READY', 'FAILED'] THEN 100.0
                ELSE 0.0
            END),
            n.processing_steps_total = coalesce(toInteger(n.processing_steps_total), 5),
            n.processing_steps_completed = coalesce(toInteger(n.processing_steps_completed), CASE
                WHEN n.processing_state IN ['READY', 'FAILED'] THEN coalesce(toInteger(n.processing_steps_total), 5)
                ELSE 0
            END),
            n.processing_llm_tasks_attempt = coalesce(toInteger(n.processing_llm_tasks_attempt), 0),
            n.processing_llm_last_task_at = n.processing_llm_last_task_at,
            n.processing_llm_tasks_total = coalesce(toInteger(n.processing_llm_tasks_total), 0),
            n.processing_attempts = coalesce(toInteger(n.processing_attempts), 0),
            n.processing_owner = n.processing_owner,
            n.processing_lease_expires_at = n.processing_lease_expires_at,
            n.queued_at = coalesce(n.queued_at, n.created_at),
            n.processing_heartbeat_at = coalesce(n.processing_heartbeat_at, n.queued_at, n.created_at),
            n.enrichment_priority = coalesce(n.enrichment_priority, 'P1'),
            n.enriched_nodes_touched = coalesce(toInteger(n.enriched_nodes_touched), 0),
            n.enriched_edges_touched = coalesce(toInteger(n.enriched_edges_touched), 0),
            n.emotions = coalesce(n.emotions, []),
            n.conflict_created_at = CASE
                WHEN n.conflict_created_at IS NULL AND n.conflict_group_id IS NOT NULL
                THEN datetime()
                ELSE n.conflict_created_at
            END,
            n._menhir_schema_v = {_SCHEMA_V}
        """,
        """
        MATCH (n)
        WHERE n.conflict_group_id IS NOT NULL AND n.conflict_status IS NULL
        SET n.conflict_status = 'unresolved'
        """,
    ]


def _edge_defaults_queries() -> list[str]:
    queries: list[str] = []
    for edge_label in EDGE_LABELS:
        queries.append(
            f"""
            MATCH ()-[r:{edge_label}]-()
            WHERE r._menhir_schema_v IS NULL OR r._menhir_schema_v < {_SCHEMA_V}
            SET r.weight = coalesce(toFloat(r.weight), 1.0),
                r.created_at = coalesce(r.created_at, datetime()),
                r.last_traversed = coalesce(r.last_traversed, r.created_at),
                r.source = coalesce(r.source, 'system-derived'),
                r.scope = coalesce(r.scope, 'PERSISTENT'),
                r._menhir_schema_v = {_SCHEMA_V}
            """
        )
    return queries


def get_phase1_bootstrap_queries() -> list[str]:
    """Return idempotent DDL and backfill queries for phase-1 memory shape."""
    return (
        [query.strip() for query in _node_index_queries()]
        + [query.strip() for query in _artifact_index_queries()]
        + [query.strip() for query in _view_index_queries()]
        + [query.strip() for query in _metric_index_queries()]
        + [query.strip() for query in _turn_evidence_index_queries()]
        # NOTE: typed-assertion / scalar-state DDL is intentionally NOT here. It is created only by
        # the gated activation path (get_scalar_state_activation_queries via
        # MemoryGraphAdapter.activate_scalar_state) so the source_key-anchored identity space is
        # never silently established over a legacy store. See that function's docstring.
        + [query.strip() for query in _edge_index_queries()]
        + [query.strip() for query in _node_defaults_queries()]
        + [query.strip() for query in _edge_defaults_queries()]
    )
