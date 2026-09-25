"""Shared Cypher RETURN field sets and SET fragments for repository queries.

Extracted verbatim from ``menhir.infrastructure.cypher``; every public name in
this module is re-exported from there, so existing import sites keep working.
"""

from __future__ import annotations

from menhir.domain.recall_visibility import view_live_provenance_cypher

# ---------------------------------------------------------------------------
# Shared RETURN field sets
# ---------------------------------------------------------------------------

# Processing-detail fields shared by every view that reports episode/memory
# processing state (SSOT-11: MEMORY_RETURN_FIELDS used to omit
# processing_substage/processing_substage_started_at and the active LLM
# task/kind/model/endpoint fields that EPISODE_PROCESSING_FIELDS already had,
# so the two projections silently drifted). `processing_attempts` is
# deliberately excluded here -- MEMORY_RETURN_FIELDS returns it raw while
# EPISODE_PROCESSING_FIELDS coalesces it to an int default of 0; that's an
# existing, intentional per-view difference, not drift, so each tuple below
# still declares its own `processing_attempts` field.
_PROCESSING_DETAIL_FIELDS = (
    "n.processing_state AS processing_state",
    "n.processing_stage AS processing_stage",
    "n.processing_substage AS processing_substage",
    "n.processing_substage_started_at AS processing_substage_started_at",
    "n.processing_progress AS processing_progress",
    "n.processing_steps_total AS processing_steps_total",
    "n.processing_steps_completed AS processing_steps_completed",
    "n.processing_llm_tasks_attempt AS processing_llm_tasks_attempt",
    "n.processing_llm_tasks_total AS processing_llm_tasks_total",
    "n.processing_llm_last_task_at AS processing_llm_last_task_at",
    "n.processing_llm_active_task AS processing_llm_active_task",
    "n.processing_llm_active_kind AS processing_llm_active_kind",
    "n.processing_llm_active_model AS processing_llm_active_model",
    "n.processing_llm_active_endpoint AS processing_llm_active_endpoint",
    "n.queued_at AS queued_at",
    "n.reference_time AS reference_time",
    "n.processing_owner AS processing_owner",
    "n.processing_lease_expires_at AS processing_lease_expires_at",
    "n.processing_heartbeat_at AS processing_heartbeat_at",
    "n.processing_started_at AS processing_started_at",
    "n.processing_completed_at AS processing_completed_at",
    "n.processing_error AS processing_error",
)

# Full memory node fields — used by fetch_recent, fetch_flagged,
# fetch_by_scope, fetch_by_type, fetch_by_uuid.
MEMORY_RETURN_FIELDS = (
    "labels(n) AS labels",
    "n.uuid AS uuid",
    "n.name AS name",
    "n.type AS type",
    "n.scope AS scope",
    "n.content AS content",
    "n.summary AS summary",
    "n.source AS source",
    # The contributor list, not just the primary `source`. A merged node's `source` holds only the
    # LOWEST-tier contributor, so `project-scan` can be present in provenance while absent from that
    # single field -- and `structural_memory.infer_legacy_structure_role` reads these rows to decide
    # whether one is legacy structure. Without `sources` the Python boundary check disagrees with the
    # Cypher predicate that already reads the list, and a merged structure row surfaces in recall as
    # if it were a memory.
    "n.sources AS sources",
    "n.source_confidence AS source_confidence",
    "n.user_flagged AS user_flagged",
    "n.bootstrap_scope AS bootstrap_scope",
    "n.session_id AS session_id",
    "n.user_id AS user_id",
    "n.created_at AS created_at",
    "n.last_accessed AS last_accessed",
    "n.freshness AS freshness",
    *_PROCESSING_DETAIL_FIELDS,
    "n.processing_attempts AS processing_attempts",
    "n.resolved_episode_uuid AS resolved_episode_uuid",
    "n.enriched_nodes_touched AS enriched_nodes_touched",
    "n.enriched_edges_touched AS enriched_edges_touched",
)

# Entity metadata for scoring/recall.
ENTITY_METADATA_FIELDS = (
    "n.uuid AS uuid",
    "n.name AS name",
    "n.scope AS scope",
    "n.type AS type",
    "n.content AS content",
    "n.summary AS summary",
    "n.last_accessed AS last_accessed",
    "n.created_at AS created_at",
    "n.belief_commit AS belief_commit",
    "n.edge_count AS edge_count",
    "n.sharpness AS sharpness",
    "n.freshness AS freshness",
    "n.user_flagged AS user_flagged",
    "n.bootstrap_scope AS bootstrap_scope",
    # SESSION visibility is caller-relative. The recall boundary needs this owner stamp
    # alongside scope so it can reject otherwise relevant nodes from another session.
    "n.session_id AS session_id",
    "coalesce(toInteger(n.rehydration_count), 0) AS rehydration_count",
    "n.conflict_group_id AS conflict_group_id",
    "n.conflict_status AS conflict_status",
    "CASE WHEN n.target_date IS NOT NULL AND date(n.target_date) < date() THEN true ELSE false END AS target_date_passed",
    "coalesce(n.namespace, 'default') AS namespace",
    # View(kind) supersession: view_current is false only on superseded View versions; unset on
    # every normal memory. Recall uses it to keep stale Views from competing with current state.
    "n.view_current AS view_current",
    "n.view_kind AS view_kind",
    "n.view_class AS view_class",
    "n.view_subtype AS view_subtype",
    "n.view_audience AS view_audience",
    "coalesce(n.retired, false) AS retired",
    "coalesce(n.is_view, false) AS is_view",
    "n.episode_uuids AS episode_uuids",
    "CASE WHEN NOT coalesce(n.is_view, false) THEN true "
    f"ELSE {view_live_provenance_cypher('n')} "
    "END AS view_provenance_live",
    # Structural graph role (directory/file/project) on project-scan nodes; unset (null) on
    # every semantic memory. Recall uses it to drop structural nodes that leak in via BM25 token
    # collisions, mirroring fetch_recent_memories' structural exclusion.
    "n.structure_role AS structure_role",
)

# Bi-temporal fact-edge fields for recall enrichment.
FACT_TEMPORAL_FIELDS = (
    "n.uuid AS node_uuid",
    "r.fact AS fact",
    "toString(r.valid_at) AS valid_at",
    "toString(r.invalid_at) AS invalid_at",
    "toString(r.created_at) AS created_at",
    "toString(r.expired_at) AS expired_at",
)

# Fact-edge IDENTITY + both endpoints, for shadow-mode context composition (Stage 1,
# .agent/plans/menhir-context-composition-production-integration.md). FACT_TEMPORAL_FIELDS
# above deliberately omits edge/endpoint identity (it was built for recall enrichment, where
# the caller already knows which node it asked about); shadow composition must select at
# fact-edge granularity -- one entity can carry many competing fact-edges -- so it needs
# r.uuid and both endpoint uuids/names, not just the fact text and timestamps.
SHADOW_CANDIDATE_FACT_EDGE_FIELDS = (
    "r.uuid AS fact_uuid",
    "r.fact AS fact_text",
    "n.uuid AS source_uuid",
    "n.name AS source_name",
    "m.uuid AS target_uuid",
    "m.name AS target_name",
    "toString(r.valid_at) AS valid_at",
    "toString(r.invalid_at) AS invalid_at",
    "toString(r.created_at) AS created_at",
    "toString(r.expired_at) AS expired_at",
)

# Episode fields returned after a successful claim.
EPISODE_CLAIM_FIELDS = (
    "n.uuid AS uuid",
    "n.name AS name",
    "n.content AS content",
    "n.source AS source",
    "n.session_id AS session_id",
    "n.user_id AS user_id",
    "coalesce(n.namespace, 'default') AS namespace",
    "n.queued_at AS queued_at",
    "n.reference_time AS reference_time",
    "n.processing_attempts AS processing_attempts",
    "n.processing_owner AS processing_owner",
    "n.processing_lease_expires_at AS processing_lease_expires_at",
)

# Full processing detail for episode monitoring.
EPISODE_PROCESSING_FIELDS = (
    "n.uuid AS uuid",
    "n.name AS name",
    *_PROCESSING_DETAIL_FIELDS,
    "coalesce(toInteger(n.processing_attempts), 0) AS processing_attempts",
    "coalesce(n.namespace, 'default') AS namespace",
)

# Failed episode retry candidates.
EPISODE_RETRY_FIELDS = (
    "n.uuid AS uuid",
    "n.name AS name",
    "n.session_id AS session_id",
    "n.user_id AS user_id",
    "n.source AS source",
    "n.user_flagged AS user_flagged",
    "n.bootstrap_scope AS bootstrap_scope",
    "n.processing_attempts AS processing_attempts",
    "coalesce(toInteger(n.transient_retries), 0) AS transient_retries",
    "n.processing_error AS processing_error",
    "toString(coalesce(n.processing_completed_at, n.processing_started_at, n.queued_at, n.created_at)) AS processing_completed_at",
)

# ---------------------------------------------------------------------------
# Shared SET fragments
# ---------------------------------------------------------------------------

# Reset LLM tracking fields — used in multiple reset/release methods.
LLM_RESET_SET = (
    "n.processing_llm_tasks_attempt = 0",
    "n.processing_llm_active_task = null",
    "n.processing_llm_active_kind = null",
    "n.processing_llm_active_model = null",
    "n.processing_llm_active_endpoint = null",
)
