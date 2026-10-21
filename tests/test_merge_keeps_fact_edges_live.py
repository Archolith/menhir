"""Merge keeps Graphiti fact edges (online, real Neo4j).

A fact edge is a RELATES_TO between the absorbed node and a peer Entity with ``fact IS NOT NULL``.
Merge moves each one to the survivor in the same direction with every property preserved (endpoint
properties source_uuid/target_uuid rewritten); unmerge moves them back with their CURRENT properties.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import MergeCoordinator
from menhir.services.unmerge_coordinator import UnmergeCoordinator


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def journal(tmp_path):
    return GraphOperationsJournal(db_path=tmp_path / "saga.db")


@pytest.fixture
def merger(live_repo, journal):
    return MergeCoordinator(graph_adapter=CorrelationRepository(live_repo), journal=journal)


@pytest.fixture
def unmerger(live_repo, journal):
    return UnmergeCoordinator(graph_adapter=CorrelationRepository(live_repo), journal=journal)


@pytest.fixture
def facts(live_repo):
    tag = f"test-merge-facts-{uuidlib.uuid4()}"
    ids = {
        "s": f"{tag}-s", "a": f"{tag}-a", "p": f"{tag}-p", "q": f"{tag}-q",
        "ep": f"{tag}-ep",
        "out": f"{tag}-fact-out", "in": f"{tag}-fact-in", "as": f"{tag}-fact-as",
    }
    live_repo.execute(
        """
        CREATE (s:Entity {uuid:$s, name:'survivor', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          summary:'survivor summary', content:'survivor content',
                          source:'claude-code', source_confidence:0.6})
        CREATE (a:Entity {uuid:$a, name:'absorbed', test_tag:$t, namespace:'default',
                          freshness:'ACTIVE', scope:'PERSISTENT',
                          summary:'a much longer absorbed summary that is clearly richer than the survivor text',
                          content:'a much longer absorbed content body that clearly exceeds the survivor by well over twenty percent',
                          source:'codex', source_confidence:0.8})
        CREATE (p:Entity {uuid:$p, name:'peer p', test_tag:$t})
        CREATE (q:Entity {uuid:$q, name:'peer q', test_tag:$t})
        CREATE (ep:Episodic {uuid:$ep, name:'episode', test_tag:$t})
        CREATE (ep)-[:MENTIONS]->(a)
        CREATE (a)-[:RELATES_TO {uuid:$out, name:'OWNS', fact:'a owns p', group_id:'',
                                 episodes:['ep-1','ep-2'], fact_embedding:[0.1,0.2,0.3],
                                 created_at:datetime(), valid_at:datetime(),
                                 expired_at:null, invalid_at:null,
                                 source_uuid:$a, target_uuid:$p}]->(p)
        CREATE (q)-[:RELATES_TO {uuid:$in, name:'EMPLOYS', fact:'q employs a', group_id:'',
                                 episodes:['ep-3'], fact_embedding:[0.1,0.2,0.3],
                                 created_at:datetime(), valid_at:datetime(),
                                 expired_at:null, invalid_at:null,
                                 source_uuid:$q, target_uuid:$a}]->(a)
        CREATE (a)-[:RELATES_TO {weight:0.4}]->(p)
        CREATE (a)-[:RELATES_TO {uuid:$as, name:'DUPLICATES', fact:'a duplicates s', group_id:'',
                                 episodes:['ep-4'], fact_embedding:[0.1,0.2,0.3],
                                 created_at:datetime(), valid_at:datetime(),
                                 expired_at:null, invalid_at:null,
                                 source_uuid:$a, target_uuid:$s}]->(s)
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _edge_props(repo, src, uuid, dst):
    rows = repo.execute(
        """
        MATCH (x:Entity {uuid:$src})-[r:RELATES_TO {uuid:$u}]->(y:Entity {uuid:$dst})
        RETURN properties(r) AS props
        """,
        params={"src": src, "u": uuid, "dst": dst},
    )
    return [r["props"] for r in rows]


def _count_edges(repo, uuid):
    return repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid:$u}]->() RETURN count(r) AS c", params={"u": uuid}
    )[0]["c"]


def _rel_sets(repo, uuid):
    """(plain relationship key set, fact-edge list sorted by uuid, embeddings by uuid) for a node.

    Fact edges are keyed on uuid plus every property except fact_embedding, compared as a list;
    embeddings are compared separately.
    """
    from menhir.domain import merge_snapshot as ms

    state = repo.capture_node_state(uuid)
    node = ms.decode_node(state) if state else None
    if node is None:
        return None
    plain, fact, emb = set(), [], {}
    for r in node["relationships"]:
        props = r["properties"]
        if r["type"] == "RELATES_TO" and props.get("fact") is not None:
            fact.append((
                r["direction"], r["peer_uuid"],
                tuple(sorted((k, str(v)) for k, v in props.items() if k != "fact_embedding")),
            ))
            emb[props["uuid"]] = list(props.get("fact_embedding") or [])
        else:
            plain.add((
                r["type"], r["direction"], r["peer_uuid"],
                tuple(sorted((k, str(v)) for k, v in props.items())),
            ))
    return plain, sorted(fact), emb


@pytest.mark.online
def test_merge_moves_fact_edges_to_survivor(merger, live_repo, facts):
    """Fails on the pre-fix code, which deleted fact edges with the absorbed node (and only left
    fact-less bridged edges)."""
    before_out = _edge_props(live_repo, facts["a"], facts["out"], facts["p"])
    before_in = _edge_props(live_repo, facts["q"], facts["in"], facts["a"])
    assert len(before_out) == 1 and len(before_in) == 1

    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1

    after_out = _edge_props(live_repo, facts["s"], facts["out"], facts["p"])
    after_in = _edge_props(live_repo, facts["q"], facts["in"], facts["s"])
    assert len(after_out) == 1 and len(after_in) == 1
    assert _count_edges(live_repo, facts["out"]) == 1
    assert _count_edges(live_repo, facts["in"]) == 1

    def rest(props):
        return {k: v for k, v in props.items() if k not in ("source_uuid", "target_uuid")}

    assert rest(after_out[0]) == rest(before_out[0])
    assert rest(after_in[0]) == rest(before_in[0])
    assert after_out[0]["source_uuid"] == facts["s"] and after_out[0]["target_uuid"] == facts["p"]
    assert after_in[0]["source_uuid"] == facts["q"] and after_in[0]["target_uuid"] == facts["s"]

    stale = live_repo.execute(
        """
        MATCH ()-[r]->() WHERE r.source_uuid = $a OR r.target_uuid = $a
        RETURN count(r) AS c
        """,
        params={"a": facts["a"]},
    )[0]["c"]
    assert stale == 0


@pytest.mark.online
def test_merge_does_not_move_absorbed_survivor_fact(merger, live_repo, facts):
    """Fails if a fact edge between absorbed and survivor is moved (it would become an s->s loop)."""
    assert _count_edges(live_repo, facts["as"]) == 1
    assert merger.merge(
        survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97
    )["merged"] == 1
    assert _count_edges(live_repo, facts["as"]) == 0
    loops = live_repo.execute(
        "MATCH (s:Entity {uuid:$s})-[r:RELATES_TO]->(s) RETURN count(r) AS c",
        params={"s": facts["s"]},
    )[0]["c"]
    assert loops == 0


@pytest.mark.online
def test_round_trip_returns_facts_and_leaves_no_survivor_copy(
    merger, unmerger, live_repo, facts
):
    """Fails if unmerge does not return fact edges to the absorbed node (or leaves a survivor copy)."""
    repo = CorrelationRepository(live_repo)
    before_a = _rel_sets(repo, facts["a"])
    before_s = _rel_sets(repo, facts["s"])

    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res

    assert _rel_sets(repo, facts["a"]) == before_a
    assert _rel_sets(repo, facts["s"]) == before_s
    attached = live_repo.execute(
        """
        MATCH (s:Entity {uuid:$s})-[r:RELATES_TO]-()
        WHERE r.uuid IN [$o, $i]
        RETURN count(r) AS c
        """,
        params={"s": facts["s"], "o": facts["out"], "i": facts["in"]},
    )[0]["c"]
    assert attached == 0
    assert _count_edges(live_repo, facts["out"]) == 1
    assert _count_edges(live_repo, facts["in"]) == 1


@pytest.mark.online
def test_unmerge_keeps_changes_made_to_a_moved_fact_after_the_merge(
    merger, unmerger, live_repo, facts
):
    """Fails if unmerge recreates the fact from the stale snapshot instead of moving the live edge."""
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    live_repo.execute(
        """
        MATCH (:Entity {uuid:$s})-[r:RELATES_TO {uuid:$u}]->(:Entity {uuid:$p})
        SET r.expired_at = datetime('2026-10-01T00:00:00Z'),
            r.invalid_at = datetime('2026-09-30T00:00:00Z'),
            r.episodes = r.episodes + ['later-episode']
        """,
        params={"s": facts["s"], "u": facts["out"], "p": facts["p"]},
    )
    current = _edge_props(live_repo, facts["s"], facts["out"], facts["p"])[0]

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res

    back = _edge_props(live_repo, facts["a"], facts["out"], facts["p"])
    assert len(back) == 1
    assert _count_edges(live_repo, facts["out"]) == 1
    assert back[0]["expired_at"] == current["expired_at"]
    assert back[0]["invalid_at"] == current["invalid_at"]
    assert back[0]["episodes"] == ["ep-1", "ep-2", "later-episode"]
    assert back[0]["source_uuid"] == facts["a"]
    assert back[0]["target_uuid"] == facts["p"]


@pytest.mark.online
def test_unmerge_recreates_from_snapshot_when_moved_fact_was_deleted(
    merger, unmerger, live_repo, facts
):
    """Documents today's recreate-from-snapshot behaviour (same class as issue #250); it is not an
    endorsement. Fails if unmerge stops restoring a fact whose survivor copy is gone."""
    original = _edge_props(live_repo, facts["q"], facts["in"], facts["a"])[0]
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    live_repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid:$u}]->() DELETE r", params={"u": facts["in"]}
    )
    assert _count_edges(live_repo, facts["in"]) == 0

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res

    back = _edge_props(live_repo, facts["q"], facts["in"], facts["a"])
    assert len(back) == 1
    assert _count_edges(live_repo, facts["in"]) == 1
    assert back[0] == original


@pytest.mark.online
def test_graphiti_style_resave_after_merge_updates_moved_edge_in_place(
    merger, live_repo, facts
):
    """Fails if the moved edge lacks its uuid or sits on the wrong endpoints: Graphiti's save MERGE
    would then create a duplicate edge instead of updating this one."""
    assert merger.merge(
        survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97
    )["merged"] == 1
    live_repo.execute(
        """
        MATCH (source:Entity {uuid:$s}) MATCH (target:Entity {uuid:$p})
        MERGE (source)-[e:RELATES_TO {uuid:$uuid}]->(target)
        SET e.fact = 'a owns p (updated)'
        """,
        params={"s": facts["s"], "p": facts["p"], "uuid": facts["out"]},
    )
    assert _count_edges(live_repo, facts["out"]) == 1
    rows = _edge_props(live_repo, facts["s"], facts["out"], facts["p"])
    assert len(rows) == 1 and rows[0]["fact"] == "a owns p (updated)"


# --------------------------------------------------------------------------------------------
# Fact-set guard, endpoint-property shapes, direction edge cases and result counters.
# --------------------------------------------------------------------------------------------


def _after_capture(monkeypatch, merger, action):
    """Run ``action()`` right after the real PREPARE snapshot capture (the race window)."""
    adapter = merger.graph_adapter
    real = adapter.capture_merge_snapshot

    def wrapped(*args, **kwargs):
        snap = real(*args, **kwargs)
        action()
        return snap

    monkeypatch.setattr(adapter, "capture_merge_snapshot", wrapped)


def _on_survivor(repo, survivor, uuids):
    return repo.execute(
        """
        MATCH (s:Entity {uuid:$s})-[r:RELATES_TO]-()
        WHERE r.uuid IN $us
        RETURN count(r) AS c
        """,
        params={"s": survivor, "us": list(uuids)},
    )[0]["c"]


@pytest.mark.online
def test_fact_added_after_snapshot_makes_the_merge_abstain(
    merger, journal, live_repo, facts, monkeypatch
):
    """Fails if merge_entity moves a fact the snapshot never recorded (unmerge could not return it)."""
    new_uuid = f"{facts['out']}-late"

    def add_fact():
        live_repo.execute(
            """
            MATCH (a:Entity {uuid:$a}) MATCH (p:Entity {uuid:$p})
            CREATE (a)-[:RELATES_TO {uuid:$u, name:'LATE', fact:'late fact', group_id:'',
                                     episodes:['ep-9'], source_uuid:$a, target_uuid:$p}]->(p)
            """,
            params={"a": facts["a"], "p": facts["p"], "u": new_uuid},
        )

    _after_capture(monkeypatch, merger, add_fact)
    res = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)

    assert res["merged"] == 0
    assert res["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
    assert live_repo.execute(
        "MATCH (a:Entity {uuid:$a}) RETURN count(a) AS c", params={"a": facts["a"]}
    )[0]["c"] == 1
    assert len(_edge_props(live_repo, facts["a"], facts["out"], facts["p"])) == 1
    assert len(_edge_props(live_repo, facts["q"], facts["in"], facts["a"])) == 1
    assert len(_edge_props(live_repo, facts["a"], new_uuid, facts["p"])) == 1
    assert _on_survivor(live_repo, facts["s"], [facts["out"], facts["in"], new_uuid]) == 0
    assert journal.get(res["op_id"])["state"] == "FAILED"


@pytest.mark.online
def test_fact_deleted_after_snapshot_makes_the_merge_abstain(
    merger, journal, live_repo, facts, monkeypatch
):
    """Fails if the merge proceeds when a snapshotted fact has vanished (live set != snapshot set)."""

    def drop_fact():
        live_repo.execute(
            "MATCH ()-[r:RELATES_TO {uuid:$u}]->() DELETE r", params={"u": facts["out"]}
        )

    _after_capture(monkeypatch, merger, drop_fact)
    res = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)

    assert res["merged"] == 0
    assert res["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
    assert live_repo.execute(
        "MATCH (a:Entity {uuid:$a}) RETURN count(a) AS c", params={"a": facts["a"]}
    )[0]["c"] == 1
    assert len(_edge_props(live_repo, facts["q"], facts["in"], facts["a"])) == 1
    assert _on_survivor(live_repo, facts["s"], [facts["in"], facts["out"]]) == 0
    assert journal.get(res["op_id"])["state"] == "FAILED"


def _endpoint_hits(repo, absorbed):
    return repo.execute(
        """
        MATCH ()-[r]->()
        WHERE r.source_uuid = $a OR r.target_uuid = $a
           OR r.source_node_uuid = $a OR r.target_node_uuid = $a
        RETURN count(r) AS c
        """,
        params={"a": absorbed},
    )[0]["c"]


@pytest.mark.online
def test_bulk_save_endpoint_properties_are_rewritten_both_ways(
    merger, unmerger, live_repo, facts
):
    """Fails if only source_uuid/target_uuid are rewritten and the Graphiti bulk-save names
    (source_node_uuid/target_node_uuid) keep pointing at the absorbed node, or are not restored."""
    live_repo.execute(
        """
        MATCH (:Entity {uuid:$a})-[o:RELATES_TO {uuid:$out}]->(:Entity {uuid:$p})
        SET o.source_node_uuid = $a, o.target_node_uuid = $p
        REMOVE o.source_uuid, o.target_uuid
        WITH 1 AS x
        MATCH (:Entity {uuid:$q})-[i:RELATES_TO {uuid:$in}]->(:Entity {uuid:$a})
        SET i.source_node_uuid = $q, i.target_node_uuid = $a
        REMOVE i.source_uuid, i.target_uuid
        """,
        params={k: facts[k] for k in ("a", "p", "q", "out", "in")},
    )
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1

    out = _edge_props(live_repo, facts["s"], facts["out"], facts["p"])
    inn = _edge_props(live_repo, facts["q"], facts["in"], facts["s"])
    assert len(out) == 1 and len(inn) == 1
    assert out[0]["source_node_uuid"] == facts["s"] and out[0]["target_node_uuid"] == facts["p"]
    assert inn[0]["source_node_uuid"] == facts["q"] and inn[0]["target_node_uuid"] == facts["s"]
    assert "source_uuid" not in out[0] and "target_uuid" not in inn[0]
    assert _endpoint_hits(live_repo, facts["a"]) == 0

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res
    out = _edge_props(live_repo, facts["a"], facts["out"], facts["p"])
    inn = _edge_props(live_repo, facts["q"], facts["in"], facts["a"])
    assert len(out) == 1 and len(inn) == 1
    assert out[0]["source_node_uuid"] == facts["a"] and out[0]["target_node_uuid"] == facts["p"]
    assert inn[0]["source_node_uuid"] == facts["q"] and inn[0]["target_node_uuid"] == facts["a"]
    assert _on_survivor(live_repo, facts["s"], [facts["out"], facts["in"]]) == 0


@pytest.mark.online
def test_survivor_to_absorbed_fact_is_not_moved_and_comes_back(
    merger, unmerger, live_repo, facts
):
    """Fails if an s->a fact is moved into an s->s loop, left dangling, or not restored (or doubled)."""
    sa = f"{facts['out']}-sa"
    live_repo.execute(
        """
        MATCH (s:Entity {uuid:$s}) MATCH (a:Entity {uuid:$a})
        CREATE (s)-[:RELATES_TO {uuid:$u, name:'KNOWS', fact:'s knows a', group_id:'',
                                 episodes:['ep-5'], source_uuid:$s, target_uuid:$a}]->(a)
        """,
        params={"s": facts["s"], "a": facts["a"], "u": sa},
    )
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    assert _count_edges(live_repo, sa) == 0
    loops = live_repo.execute(
        "MATCH (s:Entity {uuid:$s})-[r:RELATES_TO]->(s) RETURN count(r) AS c",
        params={"s": facts["s"]},
    )[0]["c"]
    assert loops == 0

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res
    assert _count_edges(live_repo, sa) == 1
    assert len(_edge_props(live_repo, facts["s"], sa, facts["a"])) == 1


@pytest.mark.online
def test_counters(merger, unmerger, live_repo, facts):
    """Fails if facts_moved / facts_moved_back / facts_recreated / fact_relationships miscount
    (e.g. counting the a->s fact or the plain weight edge, or reporting a recreate as a move)."""
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    assert merged["facts_moved"] == 2

    dry = unmerger.unmerge(merged["op_id"], dry_run=True)
    assert dry["reason"] == "DRY_RUN", dry
    assert dry["would_restore"]["fact_relationships"] == 2

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res
    assert res["facts_moved_back"] == 2
    assert res["facts_recreated"] == 0


@pytest.mark.online
def test_counters_when_one_moved_fact_was_deleted(merger, unmerger, live_repo, facts):
    """Fails if a recreated fact is counted as moved back (or the live one is not counted)."""
    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1
    live_repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid:$u}]->() DELETE r", params={"u": facts["in"]}
    )
    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res
    assert res["facts_moved_back"] == 1
    assert res["facts_recreated"] == 1


@pytest.mark.online
def test_parallel_duplicate_fact_edges_round_trip(merger, unmerger, live_repo, facts):
    """Fails if merge moves only one of two parallel same-uuid edges, or unmerge restores only one /
    leaves one on the survivor."""
    live_repo.execute(
        """
        MATCH (a:Entity {uuid:$a}) MATCH (p:Entity {uuid:$p})
        CREATE (a)-[:RELATES_TO {uuid:$u, name:'OWNS', fact:'a owns p (dup)', group_id:'',
                                 episodes:['ep-7'], source_uuid:$a, target_uuid:$p}]->(p)
        """,
        params={"a": facts["a"], "p": facts["p"], "u": facts["out"]},
    )
    assert len(_edge_props(live_repo, facts["a"], facts["out"], facts["p"])) == 2

    merged = merger.merge(survivor_uuid=facts["s"], absorbed_uuid=facts["a"], similarity=0.97)
    assert merged["merged"] == 1, merged
    assert _count_edges(live_repo, facts["out"]) == 2
    assert len(_edge_props(live_repo, facts["s"], facts["out"], facts["p"])) == 2

    res = unmerger.unmerge(merged["op_id"])
    assert res["restored"] == 1, res
    assert len(_edge_props(live_repo, facts["a"], facts["out"], facts["p"])) == 2
    assert _count_edges(live_repo, facts["out"]) == 2
    assert _on_survivor(live_repo, facts["s"], [facts["out"]]) == 0
