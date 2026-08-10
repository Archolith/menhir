"""End-to-end coverage for episode-provenance preservation across a merge.

Why this file exists
--------------------
``merge_nodes`` bridged only ``(absorbed)-[r]-(neighbor:Entity)`` edges, and the
audit snapshot captured only Entity peers. It then ran ``DETACH DELETE absorbed``.

So every ``(:Episodic)-[:MENTIONS]->(absorbed)`` edge -- the absorbed node's source
provenance -- was destroyed on merge and was NOT recoverable from ``merge_audit``.
Consequences: the survivor lost the absorbed node's episode lineage, unmerge could
not restore it, and past merges could not be audited against the co-mention veto
(the merge deleted the very evidence the veto reads).

These tests run the real Cypher against real Neo4j.
"""

from __future__ import annotations

import json
import uuid as uuidlib

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.neo4j import Neo4jRepository


def _purge_sidecar_rows(tag: str) -> None:
    """Remove this test's rows from the real telemetry sidecar.

    merge_entity now writes a durable merge_audit row that (by design) outlives the
    graph node -- so DETACH DELETE-ing the fixture nodes does NOT clean it up. Without
    this, online runs would permanently pollute the operator's merge history.
    """
    import sqlite3

    from menhir.infrastructure.telemetry import telemetry_store

    try:
        with sqlite3.connect(telemetry_store.db_path) as conn:
            conn.execute(
                "DELETE FROM merge_audit WHERE survivor_uuid LIKE ? OR absorbed_uuid LIKE ?",
                (f"{tag}%", f"{tag}%"),
            )
            conn.commit()
    except sqlite3.Error:
        pass


@pytest.fixture
def live_repo(test_neo4j_repo):
    """Alias onto the STOOD-UP TEST instance (conftest.test_neo4j_repo).

    Never the operator's real graph: these tests run destructive, unscoped queries.
    """
    return test_neo4j_repo


@pytest.fixture
def merge_fixture(live_repo):
    """Throwaway subgraph.

        ep_survivor -[:MENTIONS]-> survivor
        ep_absorbed -[:MENTIONS]-> absorbed     <- must survive the merge
        absorbed    -[:RELATES_TO]-> peer       <- existing Entity bridge behavior
    """
    tag = f"test-merge-prov-{uuidlib.uuid4()}"
    ids = {
        "survivor": f"{tag}-survivor",
        "absorbed": f"{tag}-absorbed",
        "peer": f"{tag}-peer",
        "ep_survivor": f"{tag}-ep-survivor",
        "ep_absorbed": f"{tag}-ep-absorbed",
    }
    live_repo.execute(
        """
        CREATE (s:Entity {uuid: $survivor, name: 'survivor node', test_tag: $tag})
        CREATE (a:Entity {uuid: $absorbed, name: 'absorbed node', test_tag: $tag})
        CREATE (p:Entity {uuid: $peer, name: 'peer node', test_tag: $tag})
        CREATE (eps:Episodic {uuid: $ep_survivor, name: 'survivor episode', test_tag: $tag})
        CREATE (epa:Episodic {uuid: $ep_absorbed, name: 'absorbed episode', test_tag: $tag})
        CREATE (eps)-[:MENTIONS]->(s)
        CREATE (epa)-[:MENTIONS]->(a)
        CREATE (a)-[:RELATES_TO]->(p)
        """,
        params={**ids, "tag": tag},
    )
    yield ids
    live_repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
    )
    _purge_sidecar_rows(tag)


@pytest.mark.online
def test_merge_repoints_absorbed_episode_provenance_to_survivor(live_repo, merge_fixture) -> None:
    """The absorbed node's MENTIONS episodes must end up pointing at the survivor."""
    repo = CorrelationRepository(live_repo)

    result = repo.merge_entity(
        survivor_uuid=merge_fixture["survivor"],
        absorbed_uuid=merge_fixture["absorbed"],
        similarity=1.0,
    )
    assert result["merged"] == 1
    assert result["deleted"] == 1
    assert result["episodes_rebound"] == 1, "absorbed node's source episode was not re-pointed"

    # The absorbed node is gone...
    gone = live_repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN count(n) AS c", params={"u": merge_fixture["absorbed"]}
    )
    assert gone[0]["c"] == 0

    # ...but its episode now mentions the survivor (provenance preserved, not destroyed).
    rows = live_repo.execute(
        """
        MATCH (ep:Episodic)-[:MENTIONS]->(s:Entity {uuid: $survivor})
        RETURN collect(DISTINCT ep.uuid) AS episodes
        """,
        params={"survivor": merge_fixture["survivor"]},
    )
    episodes = set(rows[0]["episodes"])
    assert merge_fixture["ep_absorbed"] in episodes, "absorbed node's episode provenance was lost"
    assert merge_fixture["ep_survivor"] in episodes, "survivor's own provenance was clobbered"


@pytest.mark.online
def test_merge_audit_records_absorbed_episode_provenance(live_repo, merge_fixture) -> None:
    """merge_audit must record which episodes mentioned the absorbed node (for unmerge)."""
    repo = CorrelationRepository(live_repo)
    repo.merge_entity(
        survivor_uuid=merge_fixture["survivor"],
        absorbed_uuid=merge_fixture["absorbed"],
        similarity=1.0,
    )

    rows = live_repo.execute(
        "MATCH (s:Entity {uuid: $u}) RETURN s.merge_audit AS audit",
        params={"u": merge_fixture["survivor"]},
    )
    audit = [json.loads(entry) for entry in (rows[0]["audit"] or [])]
    assert len(audit) == 1
    entry = audit[0]
    assert entry["absorbed_uuid"] == merge_fixture["absorbed"]
    assert entry["mentioned_by_episodes"] == [merge_fixture["ep_absorbed"]], (
        "audit trail must capture the absorbed node's episode provenance so unmerge can restore it"
    )


@pytest.fixture
def chain_fixture(live_repo):
    """Three bare entities for a chained merge: B -> A, then A -> C."""
    tag = f"test-merge-chain-{uuidlib.uuid4()}"
    ids = {"a": f"{tag}-A", "b": f"{tag}-B", "c": f"{tag}-C"}
    live_repo.execute(
        """
        CREATE (a:Entity {uuid: $a, name: 'chain A', test_tag: $tag})
        CREATE (b:Entity {uuid: $b, name: 'chain B', test_tag: $tag})
        CREATE (c:Entity {uuid: $c, name: 'chain C', test_tag: $tag})
        """,
        params={**ids, "tag": tag},
    )
    yield ids
    live_repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
    )
    _purge_sidecar_rows(tag)


@pytest.mark.online
def test_chained_merge_preserves_earlier_lineage(live_repo, chain_fixture) -> None:
    """A merge chain must not lose the earliest absorption.

    A absorbs B (B's only snapshot now lives in A.merge_audit). C then absorbs A.
    Without carrying A's own merged_from/merge_audit forward, DETACH DELETE A
    destroys B's record entirely and B becomes unrecoverable.
    """
    repo = CorrelationRepository(live_repo)
    a, b, c = chain_fixture["a"], chain_fixture["b"], chain_fixture["c"]

    repo.merge_entity(survivor_uuid=a, absorbed_uuid=b, similarity=1.0)
    repo.merge_entity(survivor_uuid=c, absorbed_uuid=a, similarity=1.0)

    rows = live_repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN n.merged_from AS mf, n.merge_audit AS audit",
        params={"u": c},
    )
    merged_from = set(rows[0]["mf"] or [])
    audit = [json.loads(x) for x in (rows[0]["audit"] or [])]
    absorbed_uuids = {e["absorbed_uuid"] for e in audit}

    assert a in merged_from, "direct absorption (A) must be recorded"
    assert b in merged_from, "earlier absorption (B) was lost in the merge chain"
    assert absorbed_uuids == {a, b}, (
        f"merge_audit must retain BOTH absorptions; got {absorbed_uuids}. "
        "Losing B's entry makes B unrecoverable -- its snapshot only existed in A.merge_audit."
    )


@pytest.mark.online
def test_merge_audit_survives_deletion_of_the_survivor(live_repo, merge_fixture) -> None:
    """The durable sidecar audit must outlive the graph node that holds it.

    survivor.merge_audit is a property on a node the system is DESIGNED to delete
    (decay -> bridge_and_delete, orphan cleanup, user delete). Binding the audit's
    lifetime to the audited node guarantees eventual loss. The telemetry sidecar
    decouples them: after the survivor is deleted, the absorbed node's snapshot --
    and therefore the ability to unmerge -- must still be recoverable.
    """
    from menhir.infrastructure.telemetry import telemetry_store

    repo = CorrelationRepository(live_repo)
    survivor, absorbed = merge_fixture["survivor"], merge_fixture["absorbed"]

    repo.merge_entity(survivor_uuid=survivor, absorbed_uuid=absorbed, similarity=1.0)

    # Now destroy the survivor, exactly as decay/orphan-cleanup would.
    live_repo.execute("MATCH (n:Entity {uuid: $u}) DETACH DELETE n", params={"u": survivor})
    remaining = live_repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN count(n) AS c", params={"u": survivor}
    )
    assert remaining[0]["c"] == 0, "precondition: survivor should be gone"

    # The graph record is gone with it -- but the sidecar still has the merge.
    audit = telemetry_store.fetch_merge_audit(absorbed_uuid=absorbed)
    assert audit, "merge audit was lost when the survivor was deleted"
    entry = audit[0]
    assert entry["survivor_uuid"] == survivor
    assert entry["absorbed_uuid"] == absorbed

    snapshot = json.loads(entry["snapshot_json"])
    assert snapshot["properties"]["name"] == "absorbed node", "absorbed node is not recoverable"
    assert snapshot["mentioned_by_episodes"] == [merge_fixture["ep_absorbed"]]


@pytest.mark.online
def test_merge_still_bridges_entity_neighbors(live_repo, merge_fixture) -> None:
    """Regression: the existing Entity-neighbor bridge must keep working."""
    repo = CorrelationRepository(live_repo)
    result = repo.merge_entity(
        survivor_uuid=merge_fixture["survivor"],
        absorbed_uuid=merge_fixture["absorbed"],
        similarity=1.0,
    )
    assert result["edges_bridged"] == 1

    rows = live_repo.execute(
        """
        MATCH (s:Entity {uuid: $survivor})-[:RELATES_TO]->(p:Entity {uuid: $peer})
        RETURN count(*) AS c
        """,
        params={"survivor": merge_fixture["survivor"], "peer": merge_fixture["peer"]},
    )
    assert rows[0]["c"] == 1
