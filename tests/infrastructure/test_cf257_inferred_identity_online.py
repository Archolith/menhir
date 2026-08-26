"""CF-257 inferred-project identity allocation against a real Neo4j.

The unit lane can prove that both writers emit the constrained key, but only Neo4j can prove
that the composite uniqueness constraint serializes concurrent MERGEs into one target node.
Run with ``pytest --run-online``; the shared fixture refuses production and uses the disposable
test graph on port 7688.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from menhir.infrastructure.project_scanner import CrossProjectRef
from menhir.infrastructure.schema import get_phase1_bootstrap_queries
from menhir.infrastructure.structure_queries import (
    StructureGraphWriter,
    _inferred_project_id,
)

pytestmark = [pytest.mark.online]

_WORKERS = 6


@pytest.fixture
def graph(test_neo4j_repo):
    """Empty graph with the shipped composite constraint, cleaned again after each proof."""
    repo = test_neo4j_repo
    repo.execute("MATCH (n) DETACH DELETE n")
    ddl = next(
        statement
        for statement in get_phase1_bootstrap_queries()
        if statement.startswith("CREATE CONSTRAINT structure_project_path_unique")
    )
    repo.execute(ddl)
    rows = repo.execute(
        "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
        "WHERE name = 'structure_project_path_unique' "
        "RETURN type, entityType, labelsOrTypes, properties"
    )
    assert rows == [
        {
            "type": "UNIQUENESS",
            "entityType": "NODE",
            "labelsOrTypes": ["Entity"],
            "properties": ["structure_project_id", "structure_path"],
        }
    ]
    try:
        yield repo
    finally:
        repo.execute("MATCH (n) DETACH DELETE n")


def _seed_projects(repo: Any, names: list[str]) -> None:
    rows = [
        {
            "name": name,
            "id": f"cf257-online-source-{uuid.uuid4()}",
            "uuid": str(uuid.uuid4()),
        }
        for name in names
    ]
    repo.execute(
        "UNWIND $rows AS row "
        "CREATE (:Entity {uuid: row.uuid, structure_project: row.name, "
        "structure_project_id: row.id, structure_path: '.', structure_role: 'project', "
        "name: row.name, identity_source: 'direct'})",
        {"rows": rows},
    )


def _run_together(jobs: list[Callable[[], None]]) -> None:
    gate = threading.Barrier(len(jobs))

    def gated(job: Callable[[], None]) -> None:
        gate.wait(timeout=10)
        job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(gated, job) for job in jobs]
        for future in futures:
            future.result(timeout=30)


def _assert_one_stable_target(repo: Any, target_name: str) -> None:
    rows = repo.execute(
        "MATCH (target:Entity {structure_project: $target, structure_path: '.', "
        "structure_role: 'project'}) "
        "RETURN count(target) AS nodes, collect(target.structure_project_id) AS ids",
        {"target": target_name},
    )
    assert rows == [
        {
            "nodes": 1,
            "ids": [_inferred_project_id(target_name)],
        }
    ]


def test_concurrent_calls_writers_share_one_constrained_inferred_identity(
    graph,
) -> None:
    token = uuid.uuid4().hex
    target = f"cf257-inferred-calls-{token}"
    sources = [f"cf257-caller-{i}-{token}" for i in range(_WORKERS)]
    _seed_projects(graph, sources)
    writer = StructureGraphWriter(neo4j=graph)

    jobs = []
    for i, source in enumerate(sources):
        ref = CrossProjectRef(
            target_project=target,
            mechanism=f"mechanism-{i}",
            evidence=f"evidence-{i}",
        )
        jobs.append(
            lambda source=source, ref=ref: writer._write_calls_edge(
                source,
                ref,
                session_id="cf257-online",
                user_id="cf257-online",
                now="2026-08-25T00:00:00+00:00",
            )
        )

    _run_together(jobs)

    _assert_one_stable_target(graph, target)
    edges = graph.execute(
        "MATCH (source:Entity)-[:CALLS]->(target:Entity {structure_project: $target}) "
        "RETURN collect(source.structure_project) AS sources, count(*) AS edges",
        {"target": target},
    )[0]
    assert edges["edges"] == _WORKERS
    assert sorted(edges["sources"]) == sorted(sources)


def test_concurrent_contains_repo_writers_share_one_constrained_inferred_identity(
    graph,
) -> None:
    token = uuid.uuid4().hex
    target = f"cf257-inferred-nested-{token}"
    sources = [f"cf257-umbrella-{i}-{token}" for i in range(_WORKERS)]
    _seed_projects(graph, sources)
    writer = StructureGraphWriter(neo4j=graph)

    jobs = []
    expected_paths: dict[str, str] = {}
    for i, source in enumerate(sources):
        rel_path = f"packages/nested-{i}"
        expected_paths[source] = rel_path
        nested = SimpleNamespace(name=target, rel_path=rel_path)
        jobs.append(
            lambda source=source, nested=nested: writer._write_contains_repo_edge(
                source,
                nested,
                session_id="cf257-online",
                user_id="cf257-online",
                now="2026-08-25T00:00:00+00:00",
            )
        )

    _run_together(jobs)

    _assert_one_stable_target(graph, target)
    edges = graph.execute(
        "MATCH (source:Entity)-[edge:CONTAINS_REPO]->"
        "(target:Entity {structure_project: $target}) "
        "RETURN source.structure_project AS source, edge.rel_path AS rel_path",
        {"target": target},
    )
    assert {row["source"]: row["rel_path"] for row in edges} == expected_paths


def test_inferred_edges_reuse_a_target_whose_direct_scan_replaced_the_placeholder(
    graph,
) -> None:
    """A later direct scan owns its settled id; inferred writers must not recreate UUID5."""
    token = uuid.uuid4().hex
    target = f"cf257-direct-target-{token}"
    caller = f"cf257-direct-caller-{token}"
    umbrella = f"cf257-direct-umbrella-{token}"
    settled_id = f"cf257-settled-{uuid.uuid4()}"
    _seed_projects(graph, [caller, umbrella])
    graph.execute(
        "CREATE (:Entity {uuid: $uuid, structure_project: $target, "
        "structure_project_id: $id, structure_path: '.', structure_role: 'project', "
        "name: $target, identity_source: 'direct', root_path: '/srv/direct'})",
        {"uuid": str(uuid.uuid4()), "target": target, "id": settled_id},
    )
    writer = StructureGraphWriter(neo4j=graph)

    writer._write_calls_edge(
        caller,
        CrossProjectRef(target_project=target, mechanism="http", evidence="direct"),
        session_id="cf257-online",
        user_id="cf257-online",
        now="2026-08-25T00:00:00+00:00",
    )
    writer._write_contains_repo_edge(
        umbrella,
        SimpleNamespace(name=target, rel_path="packages/direct"),
        session_id="cf257-online",
        user_id="cf257-online",
        now="2026-08-25T00:00:00+00:00",
    )

    rows = graph.execute(
        "MATCH (target:Entity {structure_project: $target, structure_path: '.', "
        "structure_role: 'project'}) "
        "RETURN count(target) AS nodes, collect(target.structure_project_id) AS ids, "
        "count { (target)<-[:CALLS]-() } AS calls, "
        "count { (target)<-[:CONTAINS_REPO]-() } AS contains",
        {"target": target},
    )
    assert rows == [{"nodes": 1, "ids": [settled_id], "calls": 1, "contains": 1}]
