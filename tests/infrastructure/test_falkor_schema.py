"""Native schema shape and startup refusal contracts."""

import pytest

from menhir.infrastructure import falkor_schema as schema
from menhir.infrastructure.falkor import FalkorLiteRepository
from menhir.infrastructure.schema import (
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
)


def _indexes():
    rows = {}
    for label, properties, kind in schema.INDEXES.values():
        row = rows.setdefault(label, {"label": label, "entitytype": "NODE", "status": "OPERATIONAL", "types": {}})
        for prop in properties:
            row["types"].setdefault(prop, []).append(kind)
    return list(rows.values())


def _constraints():
    return [
        {
            "label": label,
            "properties": list(properties),
            "type": "UNIQUE",
            "entitytype": "NODE",
            "status": "OPERATIONAL",
        }
        for label, properties in schema.UNIQUE_NODES
    ]


def test_native_mapping_covers_the_existing_phase_one_contract():
    assert set(schema.INDEXES) | {row[0] for row in PHASE_ONE_REQUIRED_CONSTRAINTS} == set(PHASE_ONE_REQUIRED_INDEXES)
    assert schema._indexes_ready(_indexes(), schema._requirements(True))
    assert schema._constraints_ready(_constraints())


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "PENDING"),
        ("status", "FAILED"),
        ("entitytype", "RELATIONSHIP"),
        ("type", "RANGE"),
        ("properties", ["wrong_property"]),
        ("label", "WrongLabel"),
    ],
)
def test_constraint_requires_enforcement_and_exact_semantic_shape(field, value):
    rows = _constraints()
    rows[0][field] = value
    assert not schema._constraints_ready(rows)


def test_range_does_not_substitute_for_graphiti_fulltext():
    rows = _indexes()
    episodic = next(row for row in rows if row["label"] == "Episodic")
    episodic["types"]["content"] = ["RANGE"]
    assert not schema._indexes_ready(rows, schema._requirements(True))
    assert schema._indexes_ready(rows, schema._requirements(False))


def test_pending_index_is_not_ready():
    rows = _indexes()
    rows[0]["status"] = "UNDER CONSTRUCTION"
    assert not schema._indexes_ready(rows, schema._requirements(False))


def test_composite_uniqueness_property_order_is_not_a_different_constraint():
    rows = _constraints()
    for row in rows:
        row["properties"] = list(reversed(row["properties"]))
    assert schema._constraints_ready(rows)


def test_completed_ddl_does_not_hide_incomplete_data_backfill(tmp_path, monkeypatch):
    repo = FalkorLiteRepository(tmp_path / "graph.rdb")

    def execute(query, *args, **kwargs):
        if query == "CALL db.indexes()":
            return _indexes()
        if query == "CALL db.constraints()":
            return _constraints()
        return [{"stale": 1}]

    monkeypatch.setattr(repo, "execute", execute)
    assert not schema.phase_one_schema_ready(repo)


@pytest.mark.online
def test_duplicate_existing_data_refuses_native_bootstrap(tmp_path):
    pytest.importorskip("redislite.falkordb_client")
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    repo = FalkorLiteRepository(tmp_path / "duplicate.rdb")
    adapter = MemoryGraphAdapter(repo)
    try:
        repo.execute("CREATE (:Episodic {ingest_key:'duplicate'}),(:Episodic {ingest_key:'duplicate'})")
        result = adapter.bootstrap_phase_one()
        assert not result.success and result.failures
        assert not adapter.phase_one_schema_ready(require_graphiti=False)
        assert repo.execute("MATCH (n:Episodic) RETURN count(n) AS count,count(n._menhir_schema_v) AS stamped") == [
            {"count": 2, "stamped": 0}
        ]
    finally:
        repo.close()
