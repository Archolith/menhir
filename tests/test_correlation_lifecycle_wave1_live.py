"""Online (real Neo4j) checks for the Cypher changed by #871, #919 and #939.

- #871 ``create_related_to_edge``: True only when it created the edge; a re-check, in either
  direction, matches the one edge instead of counting or creating another.
- #919 ``promote_to_persistent`` / ``set_demote_ttl`` return the uuids they changed.
- #939 a merged-then-unmerged pair is refused by the next merge (node stamp and journal).

Each test tags what it creates and deletes it afterwards.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.unmerge_coordinator import UnmergeCoordinator


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def tag(live_repo):
    value = f"test-wave1-{uuidlib.uuid4().hex[:12]}"
    yield value
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})


def _seed(repo, tag: str, keys: list[str], **props) -> dict[str, str]:
    ids = {k: f"{tag}-{k}" for k in keys}
    for key, uuid in ids.items():
        repo.execute(
            "CREATE (n:Entity) SET n = $p",
            params={"p": {"uuid": uuid, "name": f"entity {key}", "test_tag": tag,
                          "namespace": "default", "freshness": "ACTIVE", "summary": "short",
                          **props}},
        )
    return ids


def _edges(repo, a: str, b: str) -> list[dict]:
    return repo.execute(
        "MATCH (x:Entity {uuid:$a})-[r:RELATES_TO]-(y:Entity {uuid:$b}) "
        "RETURN startNode(r).uuid AS start, r.similarity AS similarity",
        params={"a": a, "b": b},
    )


# --------------------------------------------------------------------------- #871

@pytest.mark.online
def test_related_edge_is_created_once_and_a_reverse_check_matches_it(live_repo, tag) -> None:
    ids = _seed(live_repo, tag, ["p", "q"], scope="PERSISTENT")
    repo = CorrelationRepository(live_repo)
    high, low = sorted([ids["p"], ids["q"]], reverse=True)

    assert repo.create_related_to_edge(high, low, similarity=0.72) is True
    assert repo.create_related_to_edge(high, low, similarity=0.72) is False
    assert repo.create_related_to_edge(low, high, similarity=0.80) is False

    edges = _edges(live_repo, ids["p"], ids["q"])
    assert len(edges) == 1, "a re-check either way round must not add an edge"
    assert edges[0]["start"] == low, "new edges point from the lower uuid"
    assert edges[0]["similarity"] == pytest.approx(0.80), "a match still raises similarity"


@pytest.mark.online
def test_a_legacy_edge_in_the_other_direction_is_matched(live_repo, tag) -> None:
    ids = _seed(live_repo, tag, ["p", "q"], scope="PERSISTENT")
    high, low = sorted([ids["p"], ids["q"]], reverse=True)
    live_repo.execute(
        "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
        "CREATE (a)-[:RELATES_TO {type:'correlation', similarity:0.7, weight:0.7}]->(b)",
        params={"a": high, "b": low},
    )

    assert CorrelationRepository(live_repo).create_related_to_edge(low, high, similarity=0.75) is False
    assert len(_edges(live_repo, ids["p"], ids["q"])) == 1


# --------------------------------------------------------------------------- #919

@pytest.mark.online
def test_promote_and_demote_return_only_what_changed(live_repo, tag) -> None:
    ids = _seed(live_repo, tag, ["fresh", "graced", "persistent"], scope="SESSION")
    live_repo.execute(
        "MATCH (n:Entity {uuid:$g}) SET n.ttl_expires = datetime() + duration({days: 3})",
        params={"g": ids["graced"]},
    )
    live_repo.execute(
        "MATCH (n:Entity {uuid:$p}) SET n.scope = 'PERSISTENT'", params={"p": ids["persistent"]},
    )
    repo = ConsolidationRepository(live_repo)

    demoted = repo.set_demote_ttl([ids["fresh"], ids["graced"], ids["persistent"]], 14)
    assert demoted == [ids["fresh"]]

    gone = f"{tag}-absorbed-and-deleted"
    promoted = repo.promote_to_persistent([ids["fresh"], ids["persistent"], gone])
    assert promoted == [ids["fresh"]]
    assert repo.promote_to_persistent([gone]) == []


# --------------------------------------------------------------------------- #939

@pytest.mark.online
def test_an_unmerged_pair_is_not_merged_again(live_repo, tag, tmp_path) -> None:
    ids = _seed(live_repo, tag, ["s", "b"], scope="SESSION")
    graph = CorrelationRepository(live_repo)
    journal = GraphOperationsJournal(db_path=tmp_path / "saga.db")
    merger = MergeCoordinator(graph_adapter=graph, journal=journal)
    unmerger = UnmergeCoordinator(graph_adapter=graph, journal=journal)

    merged = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["b"], similarity=0.97)
    assert merged["merged"] == 1
    assert unmerger.unmerge(merged["op_id"])["restored"] == 1

    # The node stamp alone vetoes (the policy, also the mutation-time precondition) ...
    decision = graph.evaluate_merge_eligibility(ids["s"], ids["b"])
    assert decision.reason_code == me.PREVIOUSLY_UNMERGED
    # ... and so does the journal alone, once the stamp is gone.
    again = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["b"], similarity=0.97)
    assert again["merged"] == 0 and again["reason"] == me.PREVIOUSLY_UNMERGED
    live_repo.execute(
        "MATCH (n:Entity {uuid:$b}) REMOVE n.restored_from_merge", params={"b": ids["b"]},
    )
    again = merger.merge(survivor_uuid=ids["s"], absorbed_uuid=ids["b"], similarity=0.97)
    assert again["merged"] == 0 and again["reason"] == me.PREVIOUSLY_UNMERGED
    assert again["diagnostics"]["source"] == "journal"
    assert live_repo.execute(
        "MATCH (n:Entity {uuid:$b}) RETURN count(n) AS c", params={"b": ids["b"]},
    )[0]["c"] == 1
