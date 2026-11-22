"""#742: every uniqueness constraint phase one creates is required for writer startup."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from menhir.config import MemorySettings
from menhir.core import BuildArtifacts, prepare_memory_runtime
from menhir.infrastructure import (
    MemoryGraphAdapter,
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
    get_phase1_bootstrap_queries,
)

_CONSTRAINT_DDL = re.compile(
    r"CREATE CONSTRAINT (\w+) IF NOT EXISTS FOR \(\w+:(\w+)\) REQUIRE (.+) IS UNIQUE"
)


def _created_constraints() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    created = {}
    for query in get_phase1_bootstrap_queries():
        if "CREATE CONSTRAINT" not in query:
            continue
        match = _CONSTRAINT_DDL.search(" ".join(query.split()))
        assert match, f"unparsed constraint DDL: {query}"
        name, label, props = match.groups()
        props = props.strip("()")
        created[name] = ((label,), tuple(p.strip().split(".", 1)[1] for p in props.split(",")))
    return created


def test_every_phase_one_constraint_is_required_with_its_shape() -> None:
    required = {
        name: (labels, properties)
        for name, kind, entity, labels, properties in PHASE_ONE_REQUIRED_CONSTRAINTS
        if kind == "UNIQUENESS" and entity == "NODE"
    }
    created = _created_constraints()
    assert created, "census found no constraint DDL"
    for name, shape in created.items():
        assert required.get(name) == shape, f"{name} is created by phase one but not required"
        assert name in PHASE_ONE_REQUIRED_INDEXES, f"{name} backing index not required"


def test_work_artifact_uuid_constraint_is_required() -> None:
    assert (
        "work_artifact_uuid_unique", "UNIQUENESS", "NODE", ("WorkArtifact",), ("artifact_uuid",),
    ) in PHASE_ONE_REQUIRED_CONSTRAINTS


class _IndexRepo:
    """Every required index online; constraints listed from `constraints`."""

    def __init__(self, constraints: list[tuple]) -> None:
        self.constraints = constraints

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        if "SHOW INDEXES" in query:
            return [{"names": list(PHASE_ONE_REQUIRED_INDEXES)}]
        if "SHOW CONSTRAINTS" in query:
            return [
                {"name": n, "type": t, "entityType": e, "labelsOrTypes": list(l), "properties": list(p)}
                for n, t, e, l, p in self.constraints
            ]
        return []


def test_missing_work_artifact_constraint_is_not_ready() -> None:
    # The backing index name can be online (e.g. an ordinary index) while the constraint failed.
    without = [c for c in PHASE_ONE_REQUIRED_CONSTRAINTS if c[0] != "work_artifact_uuid_unique"]
    assert MemoryGraphAdapter(neo4j=_IndexRepo(without)).phase_one_schema_ready() is False
    assert MemoryGraphAdapter(
        neo4j=_IndexRepo(list(PHASE_ONE_REQUIRED_CONSTRAINTS))
    ).phase_one_schema_ready() is True


class _DuplicateRepo:
    def __init__(self, by_label: dict[str, int]) -> None:
        self.by_label = by_label
        self.queries: list[str] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
        for label, count in self.by_label.items():
            if f"(n:`{label}`)" in query:
                return [{"duplicates": count}]
        return [{"duplicates": 0}]


def test_duplicate_probe_counts_per_constraint() -> None:
    repo = _DuplicateRepo({"WorkArtifact": 2})
    counts = MemoryGraphAdapter(neo4j=repo).phase_one_constraint_duplicates()
    assert counts == {"work_artifact_uuid_unique": 2}
    assert len(repo.queries) == len(PHASE_ONE_REQUIRED_CONSTRAINTS)
    # Composite keys group on every property.
    root = next(q for q in repo.queries if "`root_key`" in q)
    assert "n.`bound_host` AS k0" in root and "n.`root_key` AS k1" in root


def _built(adapter: SimpleNamespace) -> BuildArtifacts:
    return BuildArtifacts(
        settings=MemorySettings.from_env(),
        capabilities=SimpleNamespace(graphiti_ready=False),
        neo4j=object(),
        graphiti_client=None,
        llm=object(),
        graph_adapter=adapter,
        ingest_service=object(),
        recall_service=object(),
        scoring_service=object(),
        lifecycle_service=object(),
        context_builder=object(),
        candidate_service=object(),
    )


def _refusing_adapter(**extra) -> SimpleNamespace:
    return SimpleNamespace(
        retire_obsolete_indexes=lambda: [],
        ensure_vector_indexes=lambda: {},
        ensure_evidence_lifecycle_schema=lambda: {},
        phase_one_schema_ready=lambda **_: False,
        bootstrap_phase_one=lambda: SimpleNamespace(
            success=False, queries_executed=1, failures=["constraint creation failed"],
        ),
        sync_edge_counts=lambda: None,
        **extra,
    )


@pytest.mark.asyncio
async def test_startup_refusal_names_duplicate_counts() -> None:
    adapter = _refusing_adapter(
        phase_one_constraint_duplicates=lambda: {"work_artifact_uuid_unique": 3},
    )
    with pytest.raises(RuntimeError, match="refusing writer startup") as exc:
        await prepare_memory_runtime(_built(adapter))
    assert "work_artifact_uuid_unique=3" in str(exc.value)


@pytest.mark.online
@pytest.mark.timeout(120)
def test_duplicate_work_artifact_uuids_leave_schema_not_ready(test_neo4j_repo) -> None:
    graph = test_neo4j_repo
    adapter = MemoryGraphAdapter(neo4j=graph)
    graph.execute("DROP CONSTRAINT work_artifact_uuid_unique IF EXISTS")
    graph.execute("MATCH (n:WorkArtifact {artifact_uuid: 'dup-742'}) DETACH DELETE n")
    graph.execute("CREATE (:WorkArtifact {artifact_uuid: 'dup-742'}), (:WorkArtifact {artifact_uuid: 'dup-742'})")
    try:
        result = adapter.bootstrap_phase_one()
        assert any("work_artifact_uuid_unique" in f or "dup-742" in f for f in result.failures)
        assert adapter.phase_one_schema_ready(require_graphiti=False) is False
        assert adapter.phase_one_constraint_duplicates().get("work_artifact_uuid_unique") == 1
    finally:
        graph.execute("MATCH (n:WorkArtifact {artifact_uuid: 'dup-742'}) DETACH DELETE n")
    assert adapter.bootstrap_phase_one().failures == []
    assert adapter.phase_one_schema_ready(require_graphiti=False) is True


@pytest.mark.asyncio
async def test_failed_duplicate_probe_keeps_the_refusal() -> None:
    def broken() -> dict:
        raise ConnectionError("gone")

    adapter = _refusing_adapter(phase_one_constraint_duplicates=broken)
    with pytest.raises(RuntimeError, match="refusing writer startup") as exc:
        await prepare_memory_runtime(_built(adapter))
    assert "Duplicate-value check failed: ConnectionError" in str(exc.value)
