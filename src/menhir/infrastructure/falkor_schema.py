"""Native phase-one schema owner; no Neo4j DDL/introspection emulation."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from menhir.infrastructure.falkor import FalkorLiteRepository, encode_instant
from menhir.infrastructure.schema import (
    _SCHEMA_V,
    EDGE_LABELS,
    GRAPHITI_OWNED_INDEXES,
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
    PhaseOneSchemaResult,
    _edge_defaults_queries,
    _node_defaults_queries,
)

# Native indexes are per label, with property-specific kinds rather than Neo4j names.
# Keep coverage checked against the existing phase-one contract so additions fail closed.
INDEXES = {
    "entity_type_idx": ("Entity", ("type",), "RANGE"),
    "entity_scope_idx": ("Entity", ("scope",), "RANGE"),
    "episodic_type_idx": ("Episodic", ("type",), "RANGE"),
    "episodic_scope_idx": ("Episodic", ("scope",), "RANGE"),
    "episodic_processing_state_idx": ("Episodic", ("processing_state",), "RANGE"),
    "entity_bootstrap_scope_idx": ("Entity", ("bootstrap_scope",), "RANGE"),
    "episodic_bootstrap_scope_idx": ("Episodic", ("bootstrap_scope",), "RANGE"),
    "episode_uuid": ("Episodic", ("uuid",), "RANGE"),
    "episode_group_id": ("Episodic", ("group_id",), "RANGE"),
    "episode_content": ("Episodic", ("content", "source", "source_description", "group_id"), "FULLTEXT"),
    "entity_artifact_id_idx": ("Entity", ("artifact_id",), "RANGE"),
    "entity_is_artifact_idx": ("Entity", ("is_artifact",), "RANGE"),
    "entity_artifact_status_idx": ("Entity", ("artifact_status",), "RANGE"),
    "evidence_artifact_id_idx": ("Evidence", ("artifact_id",), "RANGE"),
    "evidence_uuid_idx": ("Evidence", ("uuid",), "RANGE"),
    "entity_view_key_idx": ("Entity", ("view_key",), "RANGE"),
    "entity_qs_key_idx": ("Entity", ("qs_key",), "RANGE"),
    "entity_view_kind_idx": ("Entity", ("view_kind",), "RANGE"),
    "entity_view_current_idx": ("Entity", ("view_current",), "RANGE"),
    "entity_view_subject_uuid_idx": ("Entity", ("view_subject_uuid",), "RANGE"),
    "metric_view_key_idx": ("Metric", ("view_key",), "RANGE"),
    "metric_qs_key_idx": ("Metric", ("qs_key",), "RANGE"),
    "metric_view_kind_idx": ("Metric", ("view_kind",), "RANGE"),
    "metric_view_current_idx": ("Metric", ("view_current",), "RANGE"),
    "metric_source_idx": ("Metric", ("source",), "RANGE"),
    "metric_receipt_op_idx": ("Metric", ("metric_last_receipt_op_id",), "RANGE"),
}
UNIQUE_NODES = tuple((labels[0], props) for _, _, _, labels, props in PHASE_ONE_REQUIRED_CONSTRAINTS) + (
    ("Metric", ("uuid",)),
    ("TurnEvidence", ("turn_key",)),
    ("WorkArtifact", ("artifact_uuid",)),
    ("ArtifactSource", ("source_uuid",)),
    ("ArtifactSource", ("current_locator_key",)),
    ("ArtifactReconciliationCursor", ("repository",)),
)


def _requirements(require_graphiti: bool) -> list[tuple[str, tuple[str, ...], str]]:
    constraint_names = {row[0] for row in PHASE_ONE_REQUIRED_CONSTRAINTS}
    if set(INDEXES) != set(PHASE_ONE_REQUIRED_INDEXES) - constraint_names:
        raise RuntimeError("Falkor phase-one index mapping does not cover the schema contract")
    return [spec for name, spec in INDEXES.items() if require_graphiti or name not in GRAPHITI_OWNED_INDEXES]


def _indexes_ready(rows: list[dict[str, Any]], required: list[tuple[str, tuple[str, ...], str]]) -> bool:
    return all(
        any(
            row.get("entitytype") == "NODE"
            and row.get("label") == label
            and row.get("status") == "OPERATIONAL"
            and all(kind in row.get("types", {}).get(prop, []) for prop in properties)
            for row in rows
        )
        for label, properties, kind in required
    )


def _constraints_ready(rows: list[dict[str, Any]]) -> bool:
    return all(
        any(
            row.get("type") == "UNIQUE"
            and row.get("entitytype") == "NODE"
            and row.get("label") == label
            and set(row.get("properties", [])) == set(properties)
            and row.get("status") == "OPERATIONAL"
            for row in rows
        )
        for label, properties in UNIQUE_NODES
    )


def phase_one_schema_ready(repo: FalkorLiteRepository, *, require_graphiti: bool = True) -> bool:
    if not _schema_ready(repo, require_graphiti=require_graphiti):
        return False
    # Reuse the existing version stamp so a failed/partial data backfill is retried on next
    # startup even if all native DDL is already operational. No new schema marker is needed.
    params = {"version": _SCHEMA_V, "edge_labels": list(EDGE_LABELS)}
    return not any(
        repo.execute(query, params, timeout_s=3)
        for query in (
            (
                "MATCH (n) WHERE (n:Entity OR n:Episodic) "
                "AND (n._menhir_schema_v IS NULL OR n._menhir_schema_v < $version) RETURN 1 AS stale LIMIT 1"
            ),
            (
                "MATCH ()-[r]->() WHERE type(r) IN $edge_labels "
                "AND (r._menhir_schema_v IS NULL OR r._menhir_schema_v < $version) RETURN 1 AS stale LIMIT 1"
            ),
            "MATCH (n) WHERE n.conflict_group_id IS NOT NULL AND n.conflict_status IS NULL RETURN 1 AS stale LIMIT 1",
        )
    )


def _schema_ready(repo: FalkorLiteRepository, *, require_graphiti: bool) -> bool:
    required = _requirements(require_graphiti)
    return _indexes_ready(repo.execute("CALL db.indexes()", timeout_s=3), required) and _constraints_ready(
        repo.execute("CALL db.constraints()", timeout_s=3)
    )


def bootstrap_phase_one(repo: FalkorLiteRepository, *, require_graphiti: bool = False) -> PhaseOneSchemaResult:
    """Install native requirements; wait for enforcement before changing data defaults."""
    executed = 0
    try:
        required = _requirements(False)
        indexes = repo.execute("CALL db.indexes()", timeout_s=3)
        graph = repo._get_db().select_graph(repo.database)
        ranges: dict[str, set[str]] = {}
        for label, properties, _kind in required:
            ranges.setdefault(label, set()).update(properties)
        for label, properties in UNIQUE_NODES:
            ranges.setdefault(label, set()).update(properties)
        for label, properties in ranges.items():
            existing = {
                prop
                for row in indexes
                if row.get("label") == label and row.get("entitytype") == "NODE"
                for prop, kinds in row.get("types", {}).items()
                if "RANGE" in kinds
            }
            missing = properties - existing
            if missing:
                graph.create_node_range_index(label, *sorted(missing))
                executed += 1
        constraints = repo.execute("CALL db.constraints()", timeout_s=3)
        for label, properties in UNIQUE_NODES:
            if any(
                row.get("type") == "UNIQUE"
                and row.get("entitytype") == "NODE"
                and row.get("label") == label
                and set(row.get("properties", [])) == set(properties)
                for row in constraints
            ):
                continue
            repo._get_db().client.execute_command(
                "GRAPH.CONSTRAINT",
                "CREATE",
                repo.database,
                "UNIQUE",
                "NODE",
                label,
                "PROPERTIES",
                len(properties),
                *properties,
            )
            executed += 1
        deadline = time.monotonic() + 10
        while not _schema_ready(repo, require_graphiti=require_graphiti):
            if any(row.get("status") == "FAILED" for row in repo.execute("CALL db.constraints()", timeout_s=3)):
                raise RuntimeError("Falkor constraint validation failed; existing data requires repair")
            if time.monotonic() >= deadline:
                raise RuntimeError("Falkor schema did not become operational before the startup deadline")
            time.sleep(0.02)
        params = {"schema_now": encode_instant(datetime.now(UTC))}
        for query in _node_defaults_queries(now_expression="$schema_now") + _edge_defaults_queries(
            now_expression="$schema_now"
        ):
            repo.execute(query, params, timeout_s=30)
            executed += 1
    except Exception as exc:  # noqa: BLE001 - bootstrap reports failure to the existing startup gate
        return PhaseOneSchemaResult(False, executed, [str(exc)])
    return PhaseOneSchemaResult(True, executed, [])
