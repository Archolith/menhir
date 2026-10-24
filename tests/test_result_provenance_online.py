"""Online: MemoryQueryRepository.fetch_result_provenance against a real Neo4j (test instance only).

Needs the throwaway test Neo4j (fixture ``test_neo4j_repo``); run with::

    MENHIR_TEST_NEO4J_URI=bolt://127.0.0.1:<port> pytest --run-online tests/test_result_provenance_online.py

(also reads MENHIR_TEST_NEO4J_USER / _PASSWORD / _DATABASE; default bolt://127.0.0.1:7688).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.recall_provenance import derive_recall_provenance
from menhir.infrastructure.memory_queries import MemoryQueryRepository


def _seed(repo, ns: str) -> dict[str, str]:
    """Plain-Cypher graph. Namespace lives on ``group_id`` (what tenant_scope_cypher also reads)."""
    tag = uuidlib.uuid4().hex[:8]
    ids = {k: f"{tag}-{k}" for k in (
        "e_user", "e_agent", "r_user", "ent_both", "ent_agent", "ent_none", "other_ent",
        "edge", "pending",
    )}
    repo.execute(
        """
        CREATE (eu:Episodic {uuid: $e_user, group_id: $ns, source: 'user', content: 'u',
                             valid_at: datetime('2026-02-01T00:00:00Z')})
        CREATE (ea:Episodic {uuid: $e_agent, group_id: $ns, source: 'claude-code', content: 'a',
                             valid_at: datetime('2026-03-01T00:00:00Z')})
        CREATE (ru:Episodic {uuid: $r_user, group_id: $ns, source: 'user', content: 'receipt',
                             resolved_episode_uuid: $e_user,
                             reference_time: datetime('2026-01-15T00:00:00Z')})
        CREATE (ru)-[:ADMITTED_ON]->(:TurnEvidence {turn_id: 't1', namespace: $ns})
        CREATE (both:Entity {uuid: $ent_both, group_id: $ns, name: 'both'})
        CREATE (ag:Entity {uuid: $ent_agent, group_id: $ns, name: 'agent only'})
        CREATE (none:Entity {uuid: $ent_none, group_id: $ns, name: 'orphan'})
        CREATE (eu)-[:MENTIONS]->(both)
        CREATE (ea)-[:MENTIONS]->(both)
        CREATE (ea)-[:MENTIONS]->(ag)
        CREATE (a:Entity {uuid: $other_ent, group_id: $ns, name: 'edge src'})
        CREATE (a)-[:RELATES_TO {uuid: $edge, group_id: $ns, fact: 'f',
                                 episodes: [$e_user]}]->(both)
        CREATE (:Episodic {uuid: $pending, group_id: $ns, source: 'agent_inference',
                           content: 'pending anchor'})
        """,
        {**ids, "ns": ns},
    )
    return ids


def _by_uuid(rows) -> dict[str, dict]:
    return {r["uuid"]: dict(r) for r in rows}


@pytest.mark.online
def test_fetch_result_provenance_signals_and_scoping(test_neo4j_repo):
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    ids = _seed(test_neo4j_repo, ns)
    queries = MemoryQueryRepository(test_neo4j_repo)
    asked = [ids[k] for k in ("ent_both", "ent_agent", "ent_none", "edge", "pending")]
    asked.append(f"{ns}-does-not-exist")

    got = _by_uuid(queries.fetch_result_provenance(asked, ns))

    # entity mentioned by a user episode (with receipt) and an agent episode -> mixed
    both = got[ids["ent_both"]]
    assert sorted(s for s in both["sources"] if s) == ["claude-code", "user"]
    assert list(both["turn_evidence_ids"]) == ["t1"]
    assert both["first_seen_at"]  # receipt reference_time / valid_at, as a string
    assert derive_recall_provenance(both).said_by == "mixed"
    # the receipt's reference_time (2026-01) is the earliest first-seen
    assert str(both["first_seen_at"]).startswith("2026-01-15")

    # entity mentioned only by the agent episode
    agent = got[ids["ent_agent"]]
    assert [s for s in agent["sources"] if s] == ["claude-code"]
    assert derive_recall_provenance(agent).said_by == "agent"
    assert not [t for t in agent["turn_evidence_ids"] if t]

    # RELATES_TO edge whose episodes list holds only the user episode
    edge = got[ids["edge"]]
    assert derive_recall_provenance(edge).said_by == "user"
    assert list(edge["turn_evidence_ids"]) == ["t1"]

    # pending anchor queried by its own uuid reports its own source
    pending = got[ids["pending"]]
    assert [s for s in pending["sources"] if s] == ["agent_inference"]
    assert derive_recall_provenance(pending).said_by == "agent"

    # no contributing episode -> no row; unknown id -> no row
    assert ids["ent_none"] not in got
    assert f"{ns}-does-not-exist" not in got


@pytest.mark.online
def test_fetch_result_provenance_is_namespace_scoped(test_neo4j_repo):
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    ids = _seed(test_neo4j_repo, ns)
    queries = MemoryQueryRepository(test_neo4j_repo)
    asked = [ids["ent_both"], ids["edge"], ids["pending"]]

    other = queries.fetch_result_provenance(asked, f"{ns}-other-tenant")
    assert other == []

    own = _by_uuid(queries.fetch_result_provenance(asked, ns))
    assert set(own) == set(asked)

    # None = unscoped (no filter), as for fetch_temporal_facts
    unscoped = _by_uuid(queries.fetch_result_provenance(asked, None))
    assert set(unscoped) == set(asked)


@pytest.mark.online
def test_fetch_result_provenance_empty_input_returns_empty(test_neo4j_repo):
    assert MemoryQueryRepository(test_neo4j_repo).fetch_result_provenance([], "any") == []


@pytest.mark.online
def test_fetch_result_provenance_turn_ids_capped_at_five(test_neo4j_repo):
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    tag = uuidlib.uuid4().hex[:8]
    test_neo4j_repo.execute(
        """
        CREATE (e:Episodic {uuid: $e, group_id: $ns, source: 'user', content: 'u'})
        CREATE (r:Episodic {uuid: $r, group_id: $ns, source: 'user', content: 'r',
                            resolved_episode_uuid: $e})
        CREATE (n:Entity {uuid: $n, group_id: $ns, name: 'many'})
        CREATE (e)-[:MENTIONS]->(n)
        WITH r UNWIND range(1, 8) AS i
        CREATE (r)-[:ADMITTED_ON]->(:TurnEvidence {turn_id: 't' + toString(i), namespace: $ns})
        """,
        {"e": f"{tag}-e", "r": f"{tag}-r", "n": f"{tag}-n", "ns": ns},
    )
    rows = MemoryQueryRepository(test_neo4j_repo).fetch_result_provenance([f"{tag}-n"], ns)

    assert len(rows) == 1
    assert len(rows[0]["turn_evidence_ids"]) == 5
    assert len(derive_recall_provenance(rows[0]).turn_evidence_ids) == 5


@pytest.mark.online
def test_receipt_source_wins_over_graphiti_placeholder_source(test_neo4j_repo):
    """Review M1: Graphiti stamps source='message' on the episode until Menhir overwrites it;
    the gate receipt's real source must decide the label."""
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    tag = uuidlib.uuid4().hex[:8]
    test_neo4j_repo.execute(
        """
        CREATE (e:Episodic {uuid: $e, group_id: $ns, source: 'message', content: 'u'})
        CREATE (r:Episodic {uuid: $r, group_id: $ns, source: 'user', content: 'r',
                            resolved_episode_uuid: $e})
        CREATE (n:Entity {uuid: $n, group_id: $ns, name: 'x'})
        CREATE (e)-[:MENTIONS]->(n)
        """,
        {"e": f"{tag}-e", "r": f"{tag}-r", "n": f"{tag}-n", "ns": ns},
    )
    rows = MemoryQueryRepository(test_neo4j_repo).fetch_result_provenance([f"{tag}-n"], ns)
    assert derive_recall_provenance(rows[0]).said_by == "user"


@pytest.mark.online
def test_hub_entity_many_episodes_is_bounded_and_complete_on_sources(test_neo4j_repo):
    """Review M2: sources span all episodes; turn ids come only from the earliest few."""
    ns = f"prov-{uuidlib.uuid4().hex[:8]}"
    tag = uuidlib.uuid4().hex[:8]
    test_neo4j_repo.execute(
        """
        CREATE (n:Entity {uuid: $n, group_id: $ns, name: 'hub'})
        WITH n UNWIND range(1, 60) AS i
        CREATE (e:Episodic {uuid: $tag + '-e' + toString(i), group_id: $ns,
                            source: CASE WHEN i = 60 THEN 'claude-code' ELSE 'user' END,
                            content: 'c', valid_at: datetime() + duration({days: i})})
        CREATE (e)-[:MENTIONS]->(n)
        CREATE (r:Episodic {uuid: $tag + '-r' + toString(i), group_id: $ns, source: 'user',
                            content: 'r', resolved_episode_uuid: $tag + '-e' + toString(i),
                            reference_time: datetime() + duration({days: i})})
        CREATE (r)-[:ADMITTED_ON]->(:TurnEvidence {turn_id: 't' + toString(i), namespace: $ns})
        """,
        {"n": f"{tag}-n", "tag": tag, "ns": ns},
    )
    rows = MemoryQueryRepository(test_neo4j_repo).fetch_result_provenance([f"{tag}-n"], ns)
    assert len(rows) == 1
    assert derive_recall_provenance(rows[0]).said_by == "user"
    assert list(rows[0]["turn_evidence_ids"]) == ["t1", "t2", "t3", "t4", "t5"]
