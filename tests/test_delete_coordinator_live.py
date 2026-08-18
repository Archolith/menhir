"""Journaled delete paths against a REAL Neo4j (remediation plan Phase 6, online).

Two properties are load-bearing here.

1. The snapshot is durable BEFORE the node is destroyed. The missing durable-before-delete record is
   exactly why ~24 nodes destroyed by the degree-zero orphan cleanup on 2026-07-12 were
   unrecoverable.

2. The audit records what HAPPENED, not what was intended. The old TTL sweep logged its candidates as
   deleted before running the mutation -- but the mutation re-filtered on ``scope = 'SESSION'``, so a
   node promoted in the race window survived while the audit already claimed it was gone. The
   deleted-uuid list now comes back FROM the mutation, and a survivor is recorded as ``skipped``.

Evidence left unreferenced is REPORTED, never deleted: isolation is not authorization.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.delete_coordinator import DeleteCoordinator


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def coord(live_repo, tmp_path):
    return DeleteCoordinator(
        graph_adapter=MemoryGraphAdapter(neo4j=live_repo),
        journal=GraphOperationsJournal(db_path=tmp_path / "saga.db"),
    )


@pytest.fixture
def nodes(live_repo):
    """A node with typed properties, an edge, and Evidence: one shared, one exclusively its own."""
    tag = f"test-delete-{uuidlib.uuid4()}"
    ids = {
        "target": f"{tag}-target", "other": f"{tag}-other", "peer": f"{tag}-peer",
        "ev_own": f"{tag}-ev-own", "ev_shared": f"{tag}-ev-shared",
    }
    live_repo.execute(
        """
        CREATE (t:Entity {uuid:$target, name:'target', test_tag:$t, scope:'SESSION',
                          when: datetime('2026-07-13T10:30:15.123456789Z'), tags:['a','b']})
        CREATE (o:Entity {uuid:$other, name:'other', test_tag:$t, scope:'SESSION'})
        CREATE (p:Entity {uuid:$peer, name:'peer', test_tag:$t})
        CREATE (e1:Evidence {uuid:$ev_own, test_tag:$t})
        CREATE (e2:Evidence {uuid:$ev_shared, test_tag:$t})
        CREATE (t)-[:RELATES_TO {weight:0.9}]->(p)
        CREATE (t)-[:SUPPORTED_BY]->(e1)
        CREATE (t)-[:SUPPORTED_BY]->(e2)
        CREATE (o)-[:SUPPORTED_BY]->(e2)
        """,
        params={**ids, "t": tag},
    )
    yield ids
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _exists(live_repo, uuid: str) -> bool:
    return int(live_repo.execute(
        "MATCH (n {uuid:$u}) RETURN count(n) AS c", params={"u": uuid}
    )[0]["c"]) > 0


# --------------------------------------------------------------------------- explicit delete

@pytest.mark.online
def test_delete_snapshots_before_destroying_and_verifies_absence(coord, live_repo, nodes):
    res = coord.delete_entity(nodes["target"])

    assert res["deleted"] == [nodes["target"]]
    assert not _exists(live_repo, nodes["target"])
    assert coord.journal.get(res["op_id"])["state"] == "COMMITTED"

    # The snapshot OUTLIVES the node it describes -- the whole point.
    snap = coord.load_snapshot(res["op_id"])
    node = ms.decode_node(snap["nodes"][nodes["target"]])
    from neo4j.time import DateTime

    assert node["properties"]["name"] == "target"
    assert isinstance(node["properties"]["when"], DateTime), "typed values preserved"
    assert node["properties"]["tags"] == ["a", "b"]
    assert any(r["type"] == "RELATES_TO" for r in node["relationships"]), "edges preserved"


@pytest.mark.online
def test_prepare_failure_deletes_nothing(coord, live_repo, nodes, monkeypatch):
    monkeypatch.setattr(
        coord.journal, "prepare",
        lambda **k: (_ for _ in ()).throw(RuntimeError("sidecar down")),
    )
    res = coord.delete_entity(nodes["target"])

    assert res["deleted"] == []
    assert res["reason"] == "PREPARE_FAILED"
    assert _exists(live_repo, nodes["target"]), "no durable record => no destruction"


@pytest.mark.online
def test_orphaned_evidence_is_reported_not_deleted(coord, live_repo, nodes):
    """Isolation is not authorization. Report the newly unreferenced Evidence; never cascade it."""
    res = coord.delete_entity(nodes["target"])

    # ev_own is referenced ONLY by the deleted node; ev_shared is still held by `other`.
    assert nodes["ev_own"] in res["newly_unreferenced_evidence"]
    assert nodes["ev_shared"] not in res["newly_unreferenced_evidence"]
    # Reported -- and still there.
    assert _exists(live_repo, nodes["ev_own"]), "Evidence must NOT be cascade-deleted"
    assert _exists(live_repo, nodes["ev_shared"])


@pytest.mark.online
def test_dry_run_reports_without_deleting(coord, live_repo, nodes):
    res = coord.delete_entity(nodes["target"], dry_run=True)

    assert res["dry_run"] is True
    assert res["would_delete"] == [nodes["target"]]
    assert nodes["ev_own"] in res["newly_unreferenced_evidence"]
    assert _exists(live_repo, nodes["target"])
    assert coord.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_deleting_an_absent_node_is_a_no_op(coord):
    res = coord.delete_entity("does-not-exist")
    assert res["deleted"] == []
    assert res["reason"] == "NOTHING_TO_DELETE"
    assert res["already_absent"] == ["does-not-exist"]


# --------------------------------------------------------------------------- TTL sweep

@pytest.mark.online
def test_ttl_sweep_audits_what_happened_not_what_was_intended(coord, live_repo, nodes):
    """A target promoted out of SESSION scope in the race window must be recorded as SKIPPED.

    The old sweep logged every candidate as deleted BEFORE running the mutation, while the mutation
    itself re-filtered on scope='SESSION' -- so the survivor was recorded as destroyed. Taking the
    deleted list from the mutation's RETURN is what makes the audit true.
    """
    live_repo.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $u SET n.ttl_expires = datetime() - duration({days: 1})",
        params={"u": [nodes["target"], nodes["other"]]},
    )

    # THE RACE: both nodes are valid SESSION candidates when the sweep reads them. `other` is then
    # promoted out of SESSION scope in the window BEFORE the mutation runs -- exactly the interleaving
    # the old code mis-audited (it had already logged `other` as deleted by this point).
    real_delete = coord.graph_adapter.delete_entities_returning_uuids

    def promote_then_delete(node_uuids, *, require_scope=None):
        live_repo.execute(
            "MATCH (n:Entity {uuid:$u}) SET n.scope = 'PERSISTENT'", params={"u": nodes["other"]}
        )
        return real_delete(node_uuids, require_scope=require_scope)

    coord.graph_adapter.delete_entities_returning_uuids = promote_then_delete  # type: ignore[method-assign]

    res = coord.delete_expired_session_nodes()

    assert nodes["target"] in res["deleted"]
    assert nodes["other"] in res["skipped"], "a promoted node must be skipped, not claimed deleted"
    assert nodes["other"] not in res["deleted"]
    assert not _exists(live_repo, nodes["target"])
    assert _exists(live_repo, nodes["other"]), "the promoted node must survive"
    assert coord.journal.get(res["op_id"])["state"] == "COMMITTED"


@pytest.mark.online
def test_ttl_sweep_with_no_expired_nodes_is_a_no_op(coord):
    res = coord.delete_expired_session_nodes()
    assert res["deleted"] == []
    assert res["reason"] == "NOTHING_TO_DELETE"


# --------------------------------------------------------------------------- crash recovery

@pytest.mark.online
def test_crash_after_delete_commits_on_reconcile(coord, live_repo, nodes):
    """Crash between the delete and COMMITTED: reconcile observes the targets are gone and commits.
    It must NOT re-run the delete -- that could destroy nodes a crash spared."""
    real_commit = coord.journal.mark_committed
    coord.journal.mark_committed = lambda op_id: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        coord.delete_entity(nodes["target"])
    coord.journal.mark_committed = real_commit  # type: ignore[method-assign]

    assert not _exists(live_repo, nodes["target"]), "the delete landed"
    assert len(coord.journal.list_by_state("PREPARED")) == 1

    out = coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out == {"committed": 1, "needs_review": 0}
    assert coord.journal.list_by_state("PREPARED") == []


@pytest.mark.online
def test_crash_before_delete_needs_review_and_is_not_retried(coord, live_repo, nodes, monkeypatch):
    """A PREPARED delete whose mutation never ran is NOT auto-retried: the node still exists, and
    re-deleting it blindly is how a crash turns into data loss."""
    monkeypatch.setattr(
        coord.graph_adapter, "delete_entities_returning_uuids",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("neo4j outage")),
    )
    with pytest.raises(RuntimeError):
        coord.delete_entity(nodes["target"])
    monkeypatch.undo()

    assert _exists(live_repo, nodes["target"])
    out = coord._replay_prepared()  # live sweep: reconcile() is observation-only since CF-20a
    assert out == {"committed": 0, "needs_review": 1}
    assert _exists(live_repo, nodes["target"]), "reconcile must NOT delete it now"
