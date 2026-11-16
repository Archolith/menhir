"""#679 / #683 against a disposable Neo4j: path spellings and the depth limit on a real chain."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from menhir.infrastructure.structure_queries import StructureGraphWriter

pytestmark = [pytest.mark.online, pytest.mark.timeout(60)]


def _chain(graph: Any, project: str, length: int) -> list[str]:
    """f0 <- f1 <- ... <- f{length}: each file imports the previous one."""
    paths = [f"src/f{i}.py" for i in range(length + 1)]
    for path in paths:
        graph.execute(
            """CREATE (:Entity {uuid: $uuid, structure_project: $project,
            structure_path: $path, structure_role: 'file'})""",
            {"uuid": str(uuid4()), "project": project, "path": path},
        )
    for importer, imported in zip(paths[1:], paths[:-1]):
        graph.execute(
            """MATCH (a:Entity {structure_project: $project, structure_path: $a}),
                     (b:Entity {structure_project: $project, structure_path: $b})
            CREATE (a)-[:IMPORTS]->(b)""",
            {"project": project, "a": importer, "b": imported},
        )
    return paths


def test_depth_limit_is_reported_on_a_real_import_chain(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    project = f"chain-{uuid4().hex[:8]}"
    paths = _chain(graph, project, 7)
    writer = StructureGraphWriter(graph)

    cut = writer.query_blast_radius(project, ["src\\f0.py"], max_depth=5)
    assert cut["changed"] == ["src/f0.py"]
    assert cut["unindexed_paths"] == []
    assert cut["directly_affected"] == ["src/f1.py"]
    assert sorted(cut["transitively_affected"]) == paths[2:6]
    assert cut["depth_truncated"] is True

    whole = writer.query_blast_radius(project, ["./src/f0.py"], max_depth=7)
    assert sorted(whole["transitively_affected"]) == paths[2:]
    assert whole["depth_truncated"] is False


def test_function_caller_limit_parameter_runs(test_neo4j_repo: Any) -> None:
    """The callers query now takes LIMIT $limit; a real server must accept it."""
    graph = test_neo4j_repo
    project = f"calls-{uuid4().hex[:8]}"
    _chain(graph, project, 1)
    result = StructureGraphWriter(graph).query_blast_radius(project, ["src/f0.py"])
    assert result["function_callers"] == []
    assert result["function_callers_truncated"] is False
