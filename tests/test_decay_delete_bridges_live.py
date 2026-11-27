"""Bridges around a removed node, and the journaled decay delete (online, real Neo4j).

Every test is a counterexample against a seeded graph and reads the result back from the store.
Each test uses its own uuid prefix and deletes what it created.
"""

from __future__ import annotations

import threading
import time
import uuid as uuidlib

import pytest

from menhir.infrastructure.consolidation_queries import (
    MAX_BRIDGE_NEIGHBOURS,
    ConsolidationRepository,
)
from menhir.infrastructure.cypher import memory_recency_cypher
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.delete_coordinator import DeleteCoordinator

pytestmark = pytest.mark.online

_LIVE = {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"}
_OLD = "2024-01-01T00:00:00Z"
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
    """A COMPRESSED node that is old, sparse and unsharp: every decay predicate holds."""
    return {**_LIVE, "freshness": "COMPRESSED", "edge_count": 0, "sharpness": 0.05,
            "last_accessed": _OLD, "created_at": _OLD, **extra}


def _gone(**extra):
    return {**_LIVE, "freshness": "GONE", **extra}


def _read(repo, uuid):
    """The values a sweep would have read for this node, in the statement's keyword names."""
    row = repo.execute(
        f"""
        MATCH (n:Entity {{uuid:$u}})
        OPTIONAL MATCH (n)-[r]-(p)
        RETURN coalesce(n.type, 'SEMANTIC') AS read_type,
               coalesce(toInteger(n.edge_count), 0) AS read_edge_count,
               toFloat(n.sharpness) AS read_sharpness,
               toString({memory_recency_cypher("n")}) AS read_recency,
               count(r) AS read_rel_count,
               collect(CASE WHEN r IS NULL THEN NULL ELSE type(r) + '|'
                   + CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END + '|'
                   + coalesce(p.uuid, p.name, elementId(p)) END) AS sigs
        """,
        params={"u": uuid},
    )[0]
    return {
        "read_type": row["read_type"], "read_edge_count": row["read_edge_count"],
        "read_sharpness": row["read_sharpness"], "read_recency": row["read_recency"],
        "requires_target_date": False, "read_rel_count": row["read_rel_count"],
        "read_rel_signatures": sorted(set(row["sigs"])),
    }


def _delete(repo, uuid, **override):
    """``bridge_and_delete`` with the live node's own values as the read (all else equal)."""
    gate = {**_GATE, **_read(repo, uuid), **override}
    return ConsolidationRepository(repo).bridge_and_delete(uuid, **gate)


def test_hub_over_the_cap_is_refused_not_deleted_and_reported(live_repo, tag):
    nodes = {"n": _removed()}
    nodes.update({f"x{i}": dict(_LIVE) for i in range(MAX_BRIDGE_NEIGHBOURS + 1)})
    ids = _seed(live_repo, tag, nodes)
    for i in range(MAX_BRIDGE_NEIGHBOURS + 1):
        _link(live_repo, ids["n"], ids[f"x{i}"], weight=0.5)

    result = _delete(live_repo, ids["n"])

    assert result["deleted"] == 0 and result["edges_bridged"] == 0
    assert result["bridge_skipped_hubs"] == [ids["n"]]
    assert result["reason"] == "bridge_cap_exceeded"
    assert _bridges(live_repo, ids["n"]) == []
    assert _exists(live_repo, ids["n"])


def test_node_at_the_cap_is_bridged_over_adjacency_edges_only(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "anchor": dict(_LIVE),
    })
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["b"], ids["n"])  # direction does not matter
    _link(live_repo, ids["n"], ids["anchor"], rel="ANCHORED_TO")

    result = _delete(live_repo, ids["n"])

    assert result["deleted"] == 1 and result["edges_bridged"] == 1
    assert {frozenset((r["a"], r["b"])) for r in _bridges(live_repo, ids["n"])} == {
        frozenset((ids["a"], ids["b"]))}
    assert _edges_between(live_repo, ids["a"], ids["anchor"]) == 0
    assert not _exists(live_repo, ids["n"])


def test_a_neighbour_waiting_for_rederive_is_not_bridged(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE),
        "scrubbed": {**_LIVE, "rederive_pending": True},
    })
    for k in ("a", "b", "scrubbed"):
        _link(live_repo, ids["n"], ids[k])

    result = _delete(live_repo, ids["n"])

    assert result["edges_bridged"] == 1
    assert _edges_between(live_repo, ids["a"], ids["scrubbed"]) == 0
    assert _edges_between(live_repo, ids["b"], ids["scrubbed"]) == 0


def test_existing_reverse_edge_stops_a_new_bridge(live_repo, tag):
    ids = _seed(live_repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    first, second = sorted((ids["a"], ids["b"]))
    _link(live_repo, second, first, weight=0.9)  # the REVERSE of the direction a bridge would take

    result = _delete(live_repo, ids["n"])

    assert result["edges_bridged"] == 0
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1
    assert _bridges(live_repo, ids["n"]) == []


def test_a_pair_already_joined_by_an_entity_mentions_edge_gets_no_second_edge(live_repo, tag):
    ids = _seed(live_repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    _link(live_repo, ids["a"], ids["b"], rel="MENTIONS")  # adjacency, but not RELATES_TO

    result = _delete(live_repo, ids["n"])

    assert result["edges_bridged"] == 0
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 0
    assert _bridges(live_repo, ids["n"]) == []


def test_count_equals_created_edges_when_some_pairs_already_exist(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "c": dict(_LIVE)})
    for k in ("a", "b", "c"):
        _link(live_repo, ids["n"], ids[k])
    _link(live_repo, ids["a"], ids["b"], weight=0.9)  # a-b exists: only a-c and b-c are new

    result = _delete(live_repo, ids["n"])

    assert result["edges_bridged"] == 2
    assert len(_bridges(live_repo, ids["n"])) == 2
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1


def test_bridges_get_a_uuid_and_the_delete_reports_them(live_repo, tag):
    ids = _seed(live_repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])

    result = _delete(live_repo, ids["n"])

    rows = _bridges(live_repo, ids["n"])
    assert len(rows) == 1 and rows[0]["uuid"]
    assert result["bridge_uuids"] == [rows[0]["uuid"]]


def test_two_removed_nodes_sharing_a_pair_create_one_edge_and_count_one(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n1": _gone(), "n2": _gone(), "a": dict(_LIVE), "b": dict(_LIVE)})
    for removed in ("n1", "n2"):
        for k in ("a", "b"):
            _link(live_repo, ids[removed], ids[k])

    count = ConsolidationRepository(live_repo).bridge_edges_for_nodes([ids["n1"], ids["n2"]])

    assert count == 1
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1


def test_duplicate_uuids_do_not_double_count_or_list_a_hub_twice(live_repo, tag):
    nodes = {"n": _gone(), "hub": _gone(), "a": dict(_LIVE), "b": dict(_LIVE)}
    nodes.update({f"x{i}": dict(_LIVE) for i in range(MAX_BRIDGE_NEIGHBOURS + 1)})
    ids = _seed(live_repo, tag, nodes)
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    for i in range(MAX_BRIDGE_NEIGHBOURS + 1):
        _link(live_repo, ids["hub"], ids[f"x{i}"])
    repo = ConsolidationRepository(live_repo)

    out = repo.bridge_edges_for_nodes_detailed([ids["n"], ids["n"], ids["hub"], ids["hub"]])

    assert out == {"edges_bridged": 1, "bridge_skipped_hubs": [ids["hub"]]}


def test_gone_removed_and_view_neighbours_are_not_bridged(live_repo, tag):
    ids = _seed(live_repo, tag, {
        "n1": _gone(), "n2": _gone(),
        "a": dict(_LIVE), "b": dict(_LIVE), "gone": _gone(),
        "view": {**_LIVE, "is_view": True, "view_kind": "FACT"},
    })
    for removed in ("n1", "n2"):
        for k in ("a", "b", "gone", "view"):
            _link(live_repo, ids[removed], ids[k])
    _link(live_repo, ids["n1"], ids["n2"])  # a removed node next to a removed node

    count = ConsolidationRepository(live_repo).bridge_edges_for_nodes([ids["n1"], ids["n2"]])

    assert count == 1
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
    nodes = {"hub": _gone(), "small": _gone()}
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


def test_a_concurrent_edge_for_the_same_pair_is_seen_not_duplicated(live_repo, tag):
    """A second session holds neighbour a's write lock, links a-b, then commits.

    The bridge must wait for that lock and then find the pair joined. Without the neighbour
    lock it would create its own a-b edge first and the second session would add another.
    """
    ids = _seed(live_repo, tag, {"n": _gone(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    locked, proceed = threading.Event(), threading.Event()

    def competitor(tx):
        tx.execute("MATCH (a:Entity {uuid:$a}) SET a.lock_probe = true", {"a": ids["a"]})
        locked.set()
        proceed.wait(30)
        tx.execute(
            "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
            "CREATE (a)-[:RELATES_TO {weight: 0.9}]->(b) REMOVE a.lock_probe",
            {"a": ids["a"], "b": ids["b"]},
        )

    rival = threading.Thread(target=lambda: live_repo.execute_write(competitor))
    rival.start()
    assert locked.wait(30)
    counts: list[int] = []
    bridger = threading.Thread(target=lambda: counts.append(
        ConsolidationRepository(live_repo).bridge_edges_for_nodes([ids["n"]])))
    bridger.start()
    time.sleep(2.0)
    proceed.set()
    rival.join(60)
    bridger.join(60)

    assert counts == [0]
    assert _edges_between(live_repo, ids["a"], ids["b"]) == 1


# ------------------------------------------------------------------- journaled decay delete

@pytest.fixture
def coord(live_repo, tmp_path):
    return DeleteCoordinator(
        graph_adapter=MemoryGraphAdapter(neo4j=live_repo),
        journal=GraphOperationsJournal(db_path=tmp_path / "saga.db"),
    )


def _decayable(repo, tag):
    ids = _seed(repo, tag, {"n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE)})
    _link(repo, ids["n"], ids["a"])
    _link(repo, ids["n"], ids["b"])
    return ids


def _candidate(repo, uuid, **over):
    """The row the sweep would have read for the node (type, values, recency)."""
    read = _read(repo, uuid)
    row = {
        "uuid": uuid, "type": read["read_type"], "scope": "PERSISTENT", "freshness": "COMPRESSED",
        "sharpness": read["read_sharpness"], "edge_count": read["read_edge_count"],
        "recency": read["read_recency"], "last_accessed_days_ago": 400.0, "user_flagged": False,
    }
    row.update(over)
    return row


def test_decay_delete_writes_snapshot_and_journal_row_and_bridges(coord, live_repo, tag):
    ids = _decayable(live_repo, tag)

    result = coord.delete_decayed_node(
        ids["n"], candidate=_candidate(live_repo, ids["n"]), **_GATE)

    assert result["deleted"] == [ids["n"]] and result["edges_bridged"] == 1
    assert len(result["bridge_uuids"]) == 1
    assert not _exists(live_repo, ids["n"])
    row = coord.journal.get(result["op_id"])
    assert (row["operation_kind"], row["state"]) == ("DECAY_DELETE", "COMMITTED")
    assert coord.load_snapshot(result["op_id"])["nodes"][ids["n"]]


@pytest.mark.parametrize("change", [
    "n.freshness = 'ACTIVE'",                      # rehydrated after selection
    "n.last_accessed = datetime()",                # recalled after selection
    "n.rederive_pending = true",
    "n.edge_count = 9",
    "n.edge_count = 2",                            # under the global cap, above what was read
    "n.sharpness = 0.9",
    "n.sharpness = 0.09",                          # under the global cap, above what was read
    "n.scope = 'SESSION'",                         # demoted: no longer PERSISTENT
    "n.type = 'IDENTITY'",                         # retyped after selection
    "n.decay_exempt = true",
    "n.user_flagged = true",
    "n.memory_retired = true",
])
def test_decay_delete_refuses_a_node_that_changed_after_selection(coord, live_repo, tag, change):
    ids = _decayable(live_repo, tag)
    candidate = _candidate(live_repo, ids["n"])
    live_repo.execute(f"MATCH (n:Entity {{uuid:$u}}) SET {change}", params={"u": ids["n"]})

    result = coord.delete_decayed_node(ids["n"], candidate=candidate, **_GATE)

    assert result["deleted"] == [] and result["skipped"] == [ids["n"]]
    assert result["reason"] == "GATE_REFUSED"
    assert _exists(live_repo, ids["n"])
    assert _bridges(live_repo, ids["n"]) == []
    assert coord.journal.get(result["op_id"])["state"] == "COMMITTED"


def test_a_stale_cached_edge_count_does_not_delete_an_over_cap_hub(coord, live_repo, tag):
    nodes = {"n": _removed()}
    nodes.update({f"x{i}": dict(_LIVE) for i in range(MAX_BRIDGE_NEIGHBOURS + 1)})
    ids = _seed(live_repo, tag, nodes)
    for i in range(MAX_BRIDGE_NEIGHBOURS + 1):
        _link(live_repo, ids["n"], ids[f"x{i}"])

    result = coord.delete_decayed_node(
        ids["n"], candidate=_candidate(live_repo, ids["n"]), **_GATE)

    assert result["deleted"] == [] and result["reason"] == "bridge_cap_exceeded"
    assert result["bridge_skipped_hubs"] == [ids["n"]]
    assert _exists(live_repo, ids["n"])
    assert coord.journal.get(result["op_id"])["state"] == "COMMITTED"


@pytest.mark.parametrize("drift", ["added", "swapped"])
def test_a_node_whose_links_changed_since_the_snapshot_is_not_deleted(live_repo, tag, drift):
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "c": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    gate = {**_GATE, **_read(live_repo, ids["n"])}  # the snapshot's view of the node
    if drift == "added":
        _link(live_repo, ids["n"], ids["c"])
    else:  # same count, different peer
        live_repo.execute(
            "MATCH (:Entity {uuid:$n})-[r]-(:Entity {uuid:$b}) DELETE r", params={
                "n": ids["n"], "b": ids["b"]})
        _link(live_repo, ids["n"], ids["c"])

    result = ConsolidationRepository(live_repo).bridge_and_delete(ids["n"], **gate)

    assert result["deleted"] == 0 and result["edges_bridged"] == 0
    assert _exists(live_repo, ids["n"])
    assert _bridges(live_repo, ids["n"]) == []


@pytest.mark.parametrize("interleaved", [
    "SET n.edge_count = 9",
    "SET n.last_accessed = datetime()",
    "WITH n MATCH (c:Entity {uuid:$c}) CREATE (n)-[:RELATES_TO {weight: 0.5}]->(c)",
])
def test_a_change_committed_while_the_delete_waits_for_the_lock_refuses_it(
    coord, live_repo, tag, interleaved
):
    """A second session holds the node's write lock and changes it; the delete waits, then re-checks."""
    ids = _seed(live_repo, tag, {
        "n": _removed(), "a": dict(_LIVE), "b": dict(_LIVE), "c": dict(_LIVE)})
    _link(live_repo, ids["n"], ids["a"])
    _link(live_repo, ids["n"], ids["b"])
    candidate = _candidate(live_repo, ids["n"])
    locked, proceed = threading.Event(), threading.Event()

    def competitor(tx):
        tx.execute("MATCH (n:Entity {uuid:$n}) SET n.lock_probe = true", {"n": ids["n"]})
        locked.set()
        proceed.wait(30)
        tx.execute(
            f"MATCH (n:Entity {{uuid:$n}}) {interleaved} REMOVE n.lock_probe",
            {"n": ids["n"], "c": ids["c"]},
        )

    rival = threading.Thread(target=lambda: live_repo.execute_write(competitor))
    rival.start()
    assert locked.wait(30)
    results: list[dict] = []
    deleter = threading.Thread(target=lambda: results.append(
        coord.delete_decayed_node(ids["n"], candidate=candidate, **_GATE)))
    deleter.start()
    time.sleep(2.0)
    proceed.set()
    rival.join(60)
    deleter.join(60)

    assert len(results) == 1
    assert results[0]["deleted"] == [] and results[0]["reason"] == "GATE_REFUSED"
    assert _exists(live_repo, ids["n"])
    assert _bridges(live_repo, ids["n"]) == []
    assert coord.journal.get(results[0]["op_id"])["state"] == "COMMITTED"
