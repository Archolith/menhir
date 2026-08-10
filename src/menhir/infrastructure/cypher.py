"""Lightweight Cypher query builder and shared field constants.

Eliminates duplication of RETURN-field lists and SET blocks across
repository modules without pulling in a full OGM.
"""

from __future__ import annotations

__all__ = [
    "Cypher",
    "MEMORY_RETURN_FIELDS",
    "ENTITY_METADATA_FIELDS",
    "EPISODE_CLAIM_FIELDS",
    "EPISODE_PROCESSING_FIELDS",
    "EPISODE_RETRY_FIELDS",
    "FACT_TEMPORAL_FIELDS",
    "SHADOW_CANDIDATE_FACT_EDGE_FIELDS",
    "LLM_RESET_SET",
    "build_reset_or_fail_query",
]


class Cypher:
    """Declarative Cypher query builder.

    Methods collect clause data; ``build()`` assembles the final query,
    merging consecutive WHERE conditions into a single block, consecutive
    SET assignments into a single block, etc.

    Usage::

        query = (Cypher()
            .match("(n:Entity)")
            .where("n.scope = 'SESSION'")
            .where_if(session_id is not None, "n.session_id = $session_id")
            .return_fields(ENTITY_METADATA_FIELDS)
            .order_by("n.name")
            .limit()
            .build())
    """

    # Consecutive ops of these types are merged into one clause.
    _MERGEABLE = frozenset({
        "WHERE", "SET", "ON_CREATE_SET", "ON_MATCH_SET", "RETURN",
    })

    def __init__(self) -> None:
        self._ops: list[tuple[str, str | list[str]]] = []

    # -- Read clauses --

    def match(self, pattern: str) -> Cypher:
        self._ops.append(("MATCH", pattern))
        return self

    def optional_match(self, pattern: str) -> Cypher:
        self._ops.append(("OPTIONAL_MATCH", pattern))
        return self

    def where(self, *conditions: str) -> Cypher:
        if conditions:
            self._ops.append(("WHERE", list(conditions)))
        return self

    def where_if(self, condition: bool, clause: str) -> Cypher:
        """Add a WHERE condition only when *condition* is truthy."""
        if condition:
            self._ops.append(("WHERE", [clause]))
        return self

    def with_clause(self, expr: str) -> Cypher:
        self._ops.append(("WITH", expr))
        return self

    # -- Write clauses --

    def create(self, pattern: str) -> Cypher:
        self._ops.append(("CREATE", pattern))
        return self

    def merge(self, pattern: str) -> Cypher:
        self._ops.append(("MERGE", pattern))
        return self

    def on_create_set(self, fields: tuple[str, ...] | list[str] | str) -> Cypher:
        if isinstance(fields, str):
            self._ops.append(("ON_CREATE_SET", [fields]))
        else:
            self._ops.append(("ON_CREATE_SET", list(fields)))
        return self

    def on_match_set(self, fields: tuple[str, ...] | list[str] | str) -> Cypher:
        if isinstance(fields, str):
            self._ops.append(("ON_MATCH_SET", [fields]))
        else:
            self._ops.append(("ON_MATCH_SET", list(fields)))
        return self

    def set(self, fields: tuple[str, ...] | list[str] | str) -> Cypher:
        if isinstance(fields, str):
            self._ops.append(("SET", [fields]))
        else:
            self._ops.append(("SET", list(fields)))
        return self

    def delete(self, expr: str) -> Cypher:
        self._ops.append(("DELETE", expr))
        return self

    def detach_delete(self, expr: str) -> Cypher:
        self._ops.append(("DETACH_DELETE", expr))
        return self

    # -- Result clauses --

    def return_fields(self, fields: tuple[str, ...], *extra: str) -> Cypher:
        self._ops.append(("RETURN", list(fields) + list(extra)))
        return self

    def return_raw(self, expr: str) -> Cypher:
        self._ops.append(("RETURN", [expr]))
        return self

    def order_by(self, expr: str) -> Cypher:
        self._ops.append(("ORDER_BY", expr))
        return self

    def skip(self, param: str = "$skip") -> Cypher:
        self._ops.append(("SKIP", param))
        return self

    def limit(self, param: str = "$limit") -> Cypher:
        self._ops.append(("LIMIT", param))
        return self

    # -- Structural --

    def unwind(self, expr: str) -> Cypher:
        self._ops.append(("UNWIND", expr))
        return self

    def raw(self, cypher: str) -> Cypher:
        """Append raw Cypher for complex blocks (CASE, CALL, etc.)."""
        self._ops.append(("RAW", cypher))
        return self

    # -- Assembly --

    def build(self) -> str:
        """Assemble the final Cypher query string.

        Consecutive mergeable ops (WHERE, SET, ON CREATE/MATCH SET, RETURN)
        are combined into a single clause.
        """
        merged = self._merge_consecutive_ops()

        parts: list[str] = []
        for op_type, value in merged:
            rendered = self._render_op(op_type, value)
            if rendered is not None:
                parts.append(rendered)
        return "\n".join(parts)

    def _merge_consecutive_ops(self) -> list[tuple[str, str | list[str]]]:
        merged: list[tuple[str, str | list[str]]] = []
        for op_type, value in self._ops:
            if (merged
                    and merged[-1][0] == op_type
                    and op_type in self._MERGEABLE):
                prev = merged[-1][1]
                if not isinstance(prev, list) or not isinstance(value, list):
                    raise TypeError(f"Mergeable op {op_type!r} requires list values")
                merged[-1] = (op_type, prev + value)
            else:
                merged.append((op_type, value))
        return merged

    @staticmethod
    def _render_op(op_type: str, value: str | list[str]) -> str | None:
        if op_type == "MATCH":
            return f"MATCH {value}"
        if op_type == "OPTIONAL_MATCH":
            return f"OPTIONAL MATCH {value}"
        if op_type == "WHERE":
            if not isinstance(value, list):
                raise TypeError("WHERE clause requires a list of conditions")
            return ("WHERE " + "\n  AND ".join(value)) if value else None
        if op_type == "WITH":
            return f"WITH {value}"
        if op_type == "CREATE":
            return f"CREATE {value}"
        if op_type == "MERGE":
            return f"MERGE {value}"
        if op_type == "ON_CREATE_SET":
            if not isinstance(value, list):
                raise TypeError("ON_CREATE_SET clause requires a list of assignments")
            return ("ON CREATE SET " + ",\n    ".join(value)) if value else None
        if op_type == "ON_MATCH_SET":
            if not isinstance(value, list):
                raise TypeError("ON_MATCH_SET clause requires a list of assignments")
            return ("ON MATCH SET " + ",\n    ".join(value)) if value else None
        if op_type == "SET":
            if not isinstance(value, list):
                raise TypeError("SET clause requires a list of assignments")
            return ("SET " + ",\n    ".join(value)) if value else None
        if op_type == "DELETE":
            return f"DELETE {value}"
        if op_type == "DETACH_DELETE":
            return f"DETACH DELETE {value}"
        if op_type == "UNWIND":
            return f"UNWIND {value}"
        if op_type == "RETURN":
            if not isinstance(value, list):
                raise TypeError("RETURN clause requires a list of expressions")
            return ("RETURN " + ",\n       ".join(value)) if value else None
        if op_type == "ORDER_BY":
            return f"ORDER BY {value}"
        if op_type == "SKIP":
            return f"SKIP {value}"
        if op_type == "LIMIT":
            return f"LIMIT {value}"
        if op_type == "RAW":
            return str(value)
        return None


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
    "coalesce(toInteger(n.rehydration_count), 0) AS rehydration_count",
    "n.conflict_group_id AS conflict_group_id",
    "n.conflict_status AS conflict_status",
    "CASE WHEN n.target_date IS NOT NULL AND date(n.target_date) < date() THEN true ELSE false END AS target_date_passed",
    "coalesce(n.namespace, 'default') AS namespace",
    # View(kind) supersession: view_current is false only on superseded View versions; unset on
    # every normal memory. Recall uses it to keep stale Views from competing with current state.
    "n.view_current AS view_current",
    "n.view_kind AS view_kind",
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


# ---------------------------------------------------------------------------
# Reset-or-fail query template
# ---------------------------------------------------------------------------

def build_reset_or_fail_query(
    *,
    match: str = "(n:Episodic)",
    where: list[str],
    exhausted_substage: str,
    reset_substage: str,
    exhausted_error: str,
    reset_error: str,
    return_alias: str = "reset",
) -> str:
    """Build a reset-or-fail query that bifurcates on attempt exhaustion.

    Computes ``exhausted`` once via WITH, then uses compact CASE expressions
    for each field that differs between the FAILED and PENDING paths.

    Substage and error parameters accept raw Cypher expressions.
    For simple literals, wrap in single quotes: ``"'my_substage'"``.
    For complex expressions, pass the full Cypher: ``"CASE WHEN ... END"``.
    """
    return (Cypher()
        .match(match)
        .where(*where)
        .with_clause(
            "n, coalesce(toInteger(n.processing_attempts), 0)"
            " >= $max_attempts AS exhausted"
        )
        .set((
            "n.processing_state = CASE WHEN exhausted THEN 'FAILED' ELSE 'PENDING' END",
            "n.processing_stage = CASE WHEN exhausted THEN 'failed' ELSE 'queued' END",
            f"n.processing_substage = CASE WHEN exhausted"
            f" THEN {exhausted_substage} ELSE {reset_substage} END",
            "n.processing_substage_started_at = datetime()",
            "n.processing_progress = CASE WHEN exhausted"
            " THEN coalesce(n.processing_progress, 100.0) ELSE 0.0 END",
            "n.processing_steps_completed = CASE WHEN exhausted"
            " THEN coalesce(toInteger(n.processing_steps_completed),"
            " coalesce(toInteger(n.processing_steps_total), 5))"
            " ELSE 0 END",
            *LLM_RESET_SET,
            "n.processing_owner = null",
            "n.processing_lease_expires_at = null",
            "n.processing_heartbeat_at = datetime()",
            "n.processing_started_at = null",
            "n.processing_completed_at = CASE WHEN exhausted THEN datetime() ELSE null END",
            f"n.processing_error = CASE WHEN exhausted"
            f" THEN {exhausted_error} ELSE {reset_error} END",
        ))
        .return_raw(f"count(n) AS {return_alias}")
        .build())
