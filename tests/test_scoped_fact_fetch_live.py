"""A namespace-scoped recall shows only that namespace's facts (online, real Neo4j).

Counterexample for the leak found on 2026-11-26: before Archolith/graphiti#38 a default
episode could resolve onto an archolith entity, so the archolith entity carried a default-group
fact, and a recall scoped to archolith showed it. ``fetch_temporal_facts`` now scopes the fact
edge. Also covers ``requeue_conflicts_for_llm_review`` matching a node that has only ``group_id``.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository


@pytest.fixture
def tag(test_neo4j_repo):
    value = f"test-scoped-facts-{uuidlib.uuid4().hex[:12]}"
    yield value
    test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})


def _entity(repo, tag: str, key: str, **props) -> str:
    uid = f"{tag}-{key}"
    repo.execute("CREATE (n:Entity) SET n = $p",
                 params={"p": {"uuid": uid, "name": key, "test_tag": tag, **props}})
    return uid


def _fact(repo, src: str, dst: str, fact: str, group_id: str) -> None:
    repo.execute(
        "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) "
        "CREATE (a)-[:RELATES_TO {uuid: $u, fact: $f, group_id: $g, created_at: datetime()}]->(b)",
        params={"a": src, "b": dst, "u": uuidlib.uuid4().hex, "f": fact, "g": group_id},
    )


def _facts(repo, uuids, namespace):
    return sorted(r["fact"] for r in MemoryQueryRepository(repo).fetch_temporal_facts(
        uuids, namespace=namespace))


@pytest.mark.online
def test_scoped_fetch_hides_another_namespaces_fact(test_neo4j_repo, tag):
    repo = test_neo4j_repo
    arch = _entity(repo, tag, "arch", namespace="archolith", group_id="archolith")
    peer = _entity(repo, tag, "peer", namespace="archolith", group_id="archolith")
    dflt = _entity(repo, tag, "dflt", namespace="default", group_id="")
    _fact(repo, arch, peer, "archolith fact", "archolith")
    _fact(repo, dflt, arch, "default fact", "")

    assert _facts(repo, [arch], "archolith") == ["archolith fact"]
    # '' and 'default' are the same namespace.
    assert _facts(repo, [arch], "default") == ["default fact"]
    assert _facts(repo, [arch], None) == ["archolith fact", "default fact"]


@pytest.mark.online
def test_requeue_matches_group_only_node_by_its_group(test_neo4j_repo, tag):
    repo = test_neo4j_repo
    gid = f"{tag}-grp"
    node = _entity(repo, tag, "grouponly", group_id="tenant_b", conflict_group_id=gid,
                   conflict_status="unresolved")
    cons = ConsolidationRepository(repo)

    def status():
        return repo.execute("MATCH (n:Entity {uuid:$u}) RETURN n.conflict_status AS s",
                            params={"u": node})[0]["s"]

    cons.requeue_conflicts_for_llm_review(from_status="unresolved", namespace="default")
    assert status() == "unresolved", "a group-only node was treated as default"
    cons.requeue_conflicts_for_llm_review(from_status="unresolved", namespace="tenant_b")
    assert status() == "pending_llm_review"
