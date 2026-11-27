"""Bridges around a removed node, and the journaled decay delete (online, real Neo4j).

Every test is a counterexample against a seeded graph and reads the result back from the store.
Each test uses its own uuid prefix and deletes what it created.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.consolidation_queries import (
    MAX_BRIDGE_NEIGHBOURS,
    ConsolidationRepository,
)
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.delete_coordinator import DeleteCoordinator

pytestmark = pytest.mark.online

_LIVE = {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"}
_OLD = "datetime() - duration({days: 400})"
_GATE = {"min_days": 90.0, "max_edge_count": 3, "max_sharpness": 0.1}


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def tag(live_repo):
    value = f"test-bridge-{uuidlib.uuid4().hex[:12]}"
    yield value
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})


def _seed(repo, tag, nodes):
    ids = {}
    for key, props in nodes.items():
        ids[key] = f"{tag}-{key}"
        repo.execute(
            "CREATE (n:Entity) SET n = $p",
            params={"p": {"uuid": ids[key], "name": key, "test_tag": tag, **props}},
        )
    return ids


def _link(repo, src, dst, rel="RELATES_TO", **props):
    repo.execute(
        f"MATCH (a:Entity {{uuid:$a}}), (b:Entity {{uuid:$b}}) CREATE (a)-[:{rel} $p]->(b)",
        params={"a": src, "b": dst, "p": props},
    )


def _bridges(repo, origin):
    return repo.execute(
        """
        MATCH (a:Entity)-[r:RELATES_TO {bridged_from: $o}]->(b:Entity)
        RETURN a.uuid AS a, b.uuid AS b, r.uuid AS uuid
        """,
        params={"o": origin},
    )


def _edges_between(repo, a, b):
    return repo.execute(
        "MATCH (:Entity {uuid:$a})-[r:RELATES_TO]-(:Entity {uuid:$b}) RETURN count(r) AS c",
        params={"a": a, "b": b},
    )[0]["c"]


def _exists(repo, uuid):
    return repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": uuid})[0]["c"] > 0


def _removed(**extra):
    return {**_LIVE, "freshness": "COMPRESSED", **extra}


def test_hub_over_the_cap_is_not_bridged_and_is_reported(live_repo, tag):
    nodes = {"n": _removed()}
    nodes.update({f"x{i}": dict(_LIVE) for i in range(MAX_BRIDGE_NEIGHBOURS + 1)})
    ids = _seed(live_repo, tag, nodes)
    for i in range(MAX_BRIDGE_NEIGHBOURS + 1):
        _link(live_repo, ids["n"], ids[f"x{i}"], weight=0.5)

    result = ConsolidationRepository(live_repo).bridge_and_delete(ids["n"])

    assert result["deleted"] == 1 and result["edges_bridged"] == 0
    assert result["bridge_skipped_hubs"] == [ids["n"]]
    assert _bridges(live_repo, ids["n"]) == []
    assert not _exists(live_repo, ids["n"])


def test_node_at_the_cap_is_bridged_over_adjacency_edges_only(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "anchor": dict(_LIVE),
    })
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["b"], ids["n"])  # direction does not matter
    _link(live_repo, ids["n"], ids["anchor"], rel="ANCHORED_TO")

    result = ConsolidationRepository(live_repo).bridge_and_delete(ids["n"])

    assert result["edges_bridged"] == 1
    assert {frozenset((r["a"], r["b"])) for r in _bridges(live_repo, ids["n"])} == {
        frozenset((ids["a"], ids["b"]))}
    assert _edges_between(live_repo, ids["a"], ids["anchor"]) == 0


def test_existing_reverse_edge_stops_a_new_bridge(live_repo, tag):
    ids = _seed(live_repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    first, second = sorted((ids["a"], ids["b"]))
    _link(live_repo, second, first, weight=0.9)  # the REVERSE of the direction a bridge would take

    result = ConsolidationRepository(live_repo).bridge_and_delete(ids["n"])

    assert result["edges_bridged"] == 0
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1
    assert _bridges(live_repo, ids["n"]) == []


def test_count_equals_created_edges_when_some_pairs_already_exist(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "c": dict(_LIVE)})
    for k in ("a", "b", "c"):
        _link(live_repo, ids["n"], ids[k])
    _link(live_repo, ids["a"], ids["b"], weight=0.9)  # a-b exists: only a-c and b-c are new

    result = ConsolidationRepository(live_repo).bridge_and_delete(ids["n"])

    assert result["edges_bridged"] == 2
    assert len(_bridges(live_repo, ids["n"])) == 2
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1


def test_bridges_get_a_uuid(live_repo, tag):
    ids = _seed(live_repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])

    ConsolidationRepository(live_repo).bridge_and_delete(ids["n"])

    rows = _bridges(live_repo, ids["n"])
    assert len(rows) == 1 and rows[0]["uuid"]


def test_gone_removed_and_view_neighbours_are_not_bridged(live_repo, tag):
    gone = {**_LIVE, "freshness": "GONE"}
    ids = _seed(live_repo, tag, {
        "n1": {**_LIVE, "freshness": "GONE"}, "n2": {**_LIVE, "freshness": "GONE"},
        "a": dict(_LIVE), "b": dict(_LIVE), "gone": gone,
        "view": {**_LIVE, "is_view": True, "view_kind": "FACT"},
    })
    for removed in ("n1", "n2"):
        for k in ("a", "b", "gone", "view"):
            _link(live_repo, ids[removed], ids[k])
    _link(live_repo, ids["n1"], ids["n2"])  # a removed node next to a removed node

    ConsolidationRepository(live_repo).bridge_edges_for_nodes([ids["n1"], ids["n2"]])

    pairs = {frozenset((r["a"], r["b"]))
             for origin in ("n1", "n2") for r in _bridges(live_repo, ids[origin])}
    assert pairs == {frozenset((ids["a"], ids["b"]))}
    assert _edges_between(live_repo, ids["a"], ids["gone"]) == 0
    assert _edges_between(live_repo, ids["a"], ids["view"]) == 0
    # The setup links a to n2 directly; only a bridge would be new.
    assert live_repo.execute(
        "MATCH (:Entity {uuid:$a})-[r:RELATES_TO]-(:Entity {uuid:$b}) WHERE r.type = 'bridged' "
        "RETURN count(r) AS c", params={"a": ids["a"], "b": ids["n2"]})[0]["c"] == 0


def test_batch_bridge_reports_a_hub_and_the_estimate_matches(live_repo, tag):
    nodes = {"hub": {**_LIVE, "freshness": "GONE"}, "small": {**_LIVE, "freshness": "GONE"}}
    nodes.update({f"x{i}": dict(_LIVE) for i in range(MAX_BRIDGE_NEIGHBOURS + 1)})
    ids = _seed(live_repo, tag, nodes)
    for i in range(MAX_BRIDGE_NEIGHBOURS + 1):
        _link(live_repo, ids["hub"], ids[f"x{i}"])
    for i in range(3):
        _link(live_repo, ids["small"], ids[f"x{i}"])
    repo = ConsolidationRepository(live_repo)

    estimate = repo.estimate_bridges_for_nodes([ids["hub"], ids["small"]])
    out = repo.bridge_edges_for_nodes_detailed([ids["hub"], ids["small"]])

    assert estimate["over_cap"] == [ids["hub"]]
    assert estimate["neighbour_counts"][ids["small"]] == 3 and estimate["estimated_edges"] == 3
    assert out["bridge_skipped_hubs"] == [ids["hub"]]
    assert out["edges_bridged"] == 3


# ------------------------------------------------------------------- journaled decay delete

@pytest.fixture
def coord(live_repo, tmp_path):
    return DeleteCoordinator(
        graph_adapter=MemoryGraphAdapter(neo4j=live_repo),
        journal=GraphOperationsJournal(db_path=tmp_path / "saga.db"),
    )


def _decayable(repo, tag):
    ids = _seed(repo, tag, {
        "n": _removed(edge_count=0, sharpness=0.05), "a": dict(_LIVE), "b": dict(_LIVE)})
    repo.execute(
        f"MATCH (n:Entity {{uuid:$u}}) SET n.last_accessed = {_OLD}, n.created_at = {_OLD}",
        params={"u": ids["n"]},
    )
    _link(repo, ids["n"], ids["a"])
    _link(repo, ids["n"], ids["b"])
    return ids


def test_decay_delete_writes_snapshot_and_journal_row_and_bridges(coord, live_repo, tag):
    ids = _decayable(live_repo, tag)

    result = coord.delete_decayed_node(ids["n"], **_GATE)

    assert result["deleted"] == [ids["n"]] and result["edges_bridged"] == 1
    assert not _exists(live_repo, ids["n"])
    row = coord.journal.get(result["op_id"])
    assert (row["operation_kind"], row["state"]) == ("DECAY_DELETE", "COMMITTED")
    assert coord.load_snapshot(result["op_id"])["nodes"][ids["n"]]


@pytest.mark.parametrize("change", [
    "n.freshness = 'ACTIVE'",                      # rehydrated after selection
    "n.last_accessed = datetime()",                # recalled after selection
    "n.rederive_pending = true",
    "n.edge_count = 9",
    "n.sharpness = 0.9",
])
def test_decay_delete_refuses_a_node_that_changed_after_selection(coord, live_repo, tag, change):
    ids = _decayable(live_repo, tag)
    live_repo.execute(f"MATCH (n:Entity {{uuid:$u}}) SET {change}", params={"u": ids["n"]})

    result = coord.delete_decayed_node(ids["n"], **_GATE)

    assert result["deleted"] == [] and result["skipped"] == [ids["n"]]
    assert _exists(live_repo, ids["n"])
    assert _bridges(live_repo, ids["n"]) == []
    assert coord.journal.get(result["op_id"])["state"] != "PREPARED"
