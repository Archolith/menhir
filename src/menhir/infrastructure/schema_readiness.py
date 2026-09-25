"""Readiness evidence sets for the phase-one memory schema.

Startup readiness checks (``memory_graph_adapter_schema`` and the first-run
preflight) compare the live index/constraint names against these sets. Re-exported
from :mod:`menhir.infrastructure.schema` so the original import path keeps working
unchanged.
"""

from __future__ import annotations

#: The three phase-one indexes GRAPHITI creates, not Menhir. Nothing in
#: `get_phase1_bootstrap_queries` emits them -- they come from
#: `graphiti_client.build_indices_and_constraints`, which `prepare_memory_runtime` skips when
#: there is no usable LLM/embedder.
#:
#: Named separately because requiring them unconditionally made the documented "start against
#: Neo4j alone" path impossible: the call that creates them was skipped, the readiness check
#: demanded them anyway, and startup was refused. A fresh install with no AI provider could not
#: start. They remain required whenever Graphiti IS available -- see
#: `phase_one_schema_ready(require_graphiti=...)`.
GRAPHITI_OWNED_INDEXES = frozenset({"episode_uuid", "episode_group_id", "episode_content"})

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
    # CF-257: SHOW INDEXES also reports the backing RANGE indexes for uniqueness
    # constraints under the constraint names. Keep all three constraints in the
    # readiness set, otherwise an upgraded installation with every legacy index online
    # skips bootstrap_phase_one() and never executes the new identity/structure DDL.
    "project_identity_id_unique",
    "project_identity_root_unique",
    "structure_project_path_unique",
)

# Name, constraint type, entity type, labels/types, ordered properties. Index names alone are not
# sufficient readiness evidence: Neo4j permits an ordinary RANGE index to use the prospective
# constraint name, and SHOW INDEXES would then make startup skip the uniqueness DDL.
PHASE_ONE_REQUIRED_CONSTRAINTS = (
    (
        "project_identity_id_unique",
        "UNIQUENESS",
        "NODE",
        ("ProjectIdentity",),
        ("project_id",),
    ),
    (
        "project_identity_root_unique",
        "UNIQUENESS",
        "NODE",
        ("ProjectIdentity",),
        ("bound_host", "root_key"),
    ),
    (
        "structure_project_path_unique",
        "UNIQUENESS",
        "NODE",
        ("Entity",),
        ("structure_project_id", "structure_path"),
    ),
)
