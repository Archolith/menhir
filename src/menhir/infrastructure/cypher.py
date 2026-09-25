"""Lightweight Cypher query builder and shared field constants.

Eliminates duplication of RETURN-field lists and SET blocks across
repository modules without pulling in a full OGM.
"""

from __future__ import annotations

from .cypher_fields import (
    ENTITY_METADATA_FIELDS,
    EPISODE_CLAIM_FIELDS,
    EPISODE_PROCESSING_FIELDS,
    EPISODE_RETRY_FIELDS,
    FACT_TEMPORAL_FIELDS,
    LLM_RESET_SET,
    MEMORY_RETURN_FIELDS,
    SHADOW_CANDIDATE_FACT_EDGE_FIELDS,
)

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
    "non_derived_view_cypher",
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


def non_derived_view_cypher(variable: str = "n") -> str:
    """Return a Cypher predicate that is true only for nodes that are NOT derived Views.

    Derived Views are stored as ``:Entity``: recallable Views carry ``is_view``/``view_kind``, and
    counter Views additionally ``is_quantstate``. The predicate is true when NONE of the three is
    present, i.e. the node is an ordinary, bindable, recallable memory -- the inverse of what a
    bind/merge/flag gate must exclude.

    Shared here rather than at each call site because the rule previously existed as five
    hand-written spellings across four modules (`episode_lifecycle`, `correlation_queries`,
    `verifier_repository`). Two of them tested only ``is_view`` and thereby admitted legacy
    counters (`is_view=false`, `is_quantstate=true`, `view_kind=null`), and one even inverted the
    form (`coalesce(x.is_view, false) = false`). One shared predicate means a gate that forgets
    ``is_quantstate`` or ``view_kind`` is the failure mode designed out.
    """
    return (
        f"NOT coalesce({variable}.is_view, false) "
        f"AND NOT coalesce({variable}.is_quantstate, false) "
        f"AND {variable}.view_kind IS NULL"
    )
