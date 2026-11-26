"""#888: the anchor-project veto fires when both entities are anchored and their project sets differ.

Identical anchor sets (single or multi) do not veto; an unanchored side never vetoes.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository


@pytest.fixture
def anchored(test_neo4j_repo):
    tag = f"test-anchor-veto-{uuidlib.uuid4()}"

    def _make(projs_a: list[str], projs_b: list[str]) -> tuple[str, str]:
        a, b = f"{tag}-a", f"{tag}-b"
        test_neo4j_repo.execute(
            """
            CREATE (a:Entity {uuid: $a, name: 'a', test_tag: $tag})
            CREATE (b:Entity {uuid: $b, name: 'b', test_tag: $tag})
            WITH a, b
            UNWIND $all AS p
            MERGE (:Project {name: $tag + '-' + p, test_tag: $tag})
            """,
            params={"a": a, "b": b, "tag": tag, "all": sorted(set(projs_a) | set(projs_b)) or ["_"]},
        )
        for uuid, projs in ((a, projs_a), (b, projs_b)):
            test_neo4j_repo.execute(
                """
                MATCH (e:Entity {uuid: $u})
                UNWIND $projs AS p
                MATCH (proj:Project {name: $tag + '-' + p})
                CREATE (e)-[:ANCHORED_TO]->(proj)
                """,
                params={"u": uuid, "projs": projs, "tag": tag},
            )
        return a, b

    yield _make
    test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag})


@pytest.mark.online
@pytest.mark.parametrize(
    "projs_a, projs_b, expected",
    [
        (["p1"], ["p2"], True),
        (["p1"], ["p1"], False),
        (["p1", "p2"], ["p1", "p2"], False),
        (["p1", "p2"], ["p3"], True),
        (["p1", "p2"], ["p1"], True),
        ([], ["p1"], False),
        (["p1"], [], False),
    ],
)
def test_anchor_project_veto(test_neo4j_repo, anchored, projs_a, projs_b, expected):
    a, b = anchored(projs_a, projs_b)
    assert CorrelationRepository(test_neo4j_repo).check_anchor_project_veto(a, b) is expected
