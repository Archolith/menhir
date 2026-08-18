"""Explicit erasure against a REAL Neo4j (CF-165, online).

Everything else about CF-165 is proven offline against a fake adapter. That leaves the one
question offline tests cannot answer: does the erasure saga work when the graph is a real
Neo4j, with real Cypher, real namespace partitioning, and a real sidecar on disk?

Three properties are load-bearing here and are only meaningful live:

1. **The node is actually gone from the graph AND the content is gone from the sidecar.** The
   whole finding is that the second half never happened.
2. **Namespace membership is captured before the partition is destroyed.** Once
   ``delete_namespace`` runs, the graph can no longer be asked which uuids needed sidecar
   cleanup -- so if capture were ordered wrongly, uuid-keyed rows would survive and only a live
   partition can demonstrate that.
3. **Erasure is sidecar-authoritative.** A uuid absent from the real graph must still have its
   sidecar content erased, because that is exactly the state a merge leaves its absorbed
   participant in.

Run with:  pytest --run-online -m online tests/test_erasure_coordinator_live.py
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    ERASED,
    GRAPH_ALREADY_ABSENT,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.online]


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def sidecar(tmp_path):
    """A real sidecar on disk, separate from the operator's telemetry database."""
    db = tmp_path / "erasure-live.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    return db


@pytest.fixture
def coord(live_repo, sidecar):
    return ErasureCoordinator(
        graph_adapter=MemoryGraphAdapter(neo4j=live_repo),
        journal=GraphOperationsJournal(db_path=sidecar),
        subjects=ErasureSubjectStore(db_path=sidecar),
    )


def _seed_revision(db, node_uuid: str, content: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memory_revisions "
            "(recorded_at, node_uuid, field, old_value, new_value, changed_by) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-08-18T00:00:00+00:00", node_uuid, "content", content, content, "live-test"),
        )
        conn.commit()


def _revisions(db, node_uuid: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid = ?",
            (node_uuid,),
        ).fetchall()


def _exists(live_repo, uuid: str) -> bool:
    rows = live_repo.execute(
        "MATCH (n) WHERE n.uuid = $uuid RETURN count(n) AS c", params={"uuid": uuid}
    )
    return int(rows[0]["c"]) > 0 if rows else False


@pytest.fixture
def node(live_repo):
    tag = f"test-erasure-{uuidlib.uuid4()}"
    uuid = f"{tag}-target"
    live_repo.execute(
        "CREATE (n:Entity {uuid:$uuid, name:'erase me', test_tag:$t, group_id:$t})",
        params={"uuid": uuid, "t": tag},
    )
    yield uuid, tag
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def test_erasure_removes_the_node_and_its_sidecar_content(coord, live_repo, sidecar, node):
    uuid, _tag = node
    _seed_revision(sidecar, uuid, "the secret text")
    assert _exists(live_repo, uuid) is True
    assert _revisions(sidecar, uuid) == [("the secret text", "the secret text")]

    out = coord.erase_memory(uuid)

    assert out["reason"] == ERASED
    assert _exists(live_repo, uuid) is False
    # The half that never used to happen.
    assert _revisions(sidecar, uuid) == [(None, None)]


def test_erasure_is_sidecar_authoritative_on_a_real_graph(coord, live_repo, sidecar):
    """A uuid that is genuinely absent from a real Neo4j still gets its content erased."""
    absent = f"test-erasure-absent-{uuidlib.uuid4()}"
    _seed_revision(sidecar, absent, "left behind by a merge")
    assert _exists(live_repo, absent) is False

    out = coord.erase_memory(absent)

    assert out["reason"] == GRAPH_ALREADY_ABSENT
    assert _revisions(sidecar, absent) == [(None, None)]


def test_namespace_erasure_captures_members_before_destroying_the_partition(
    coord, live_repo, sidecar
):
    """The ordering property: membership must be captured while the graph can still answer."""
    group = f"test-erasure-ns-{uuidlib.uuid4()}"
    members = [f"{group}-m{i}" for i in range(3)]
    live_repo.execute(
        "UNWIND $uuids AS u CREATE (n:Entity {uuid:u, group_id:$g, test_tag:$g})",
        params={"uuids": members, "g": group},
    )
    try:
        for m in members:
            _seed_revision(sidecar, m, f"content of {m}")

        out = coord.erase_namespace(group)

        assert out["reason"] == ERASED
        for m in members:
            assert _exists(live_repo, m) is False
            # Reachable only because the uuids were captured before the partition went away.
            assert _revisions(sidecar, m) == [(None, None)]

        recorded = {
            (r["subject_type"], r["subject_value"])
            for r in coord.subjects.fetch_subjects(out["op_id"])
        }
        assert ("NAMESPACE", group) in recorded
        for m in members:
            assert ("NODE_UUID", m) in recorded
    finally:
        live_repo.execute(
            "MATCH (n) WHERE n.test_tag = $g DETACH DELETE n", params={"g": group}
        )


def test_crashed_erasure_resumes_from_the_inventory_against_a_real_graph(
    coord, live_repo, sidecar
):
    """A crash after PREPARE: replay must finish it using only the inventory.

    The graph partition is deliberately still intact, so this also proves replay completes the
    graph side rather than assuming a previous attempt got that far.
    """
    group = f"test-erasure-crash-{uuidlib.uuid4()}"
    member = f"{group}-m0"
    live_repo.execute(
        "CREATE (n:Entity {uuid:$u, group_id:$g, test_tag:$g})",
        params={"u": member, "g": group},
    )
    try:
        _seed_revision(sidecar, member, "survived a crash")
        op_id = uuidlib.uuid4().hex
        with sqlite3.connect(sidecar) as conn:
            coord.journal.prepare(
                operation_kind="EXPLICIT_ERASURE",
                request_json=f'{{"namespace":"{group}","member_count":1,"targets":[]}}',
                target_key=f"erasure:namespace:{group}",
                op_id=op_id,
                conn=conn,
            )
            coord.subjects.record_subjects(
                op_id, [("NAMESPACE", group), ("NODE_UUID", member)], conn=conn
            )
            conn.commit()

        outcome, _diagnostics = coord.replay_prepared_row({"op_id": op_id})

        assert outcome == "REPLAYED"
        assert _exists(live_repo, member) is False
        assert _revisions(sidecar, member) == [(None, None)]
        with sqlite3.connect(sidecar) as conn:
            state = conn.execute(
                "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()[0]
        assert state == "COMMITTED"
    finally:
        live_repo.execute(
            "MATCH (n) WHERE n.test_tag = $g DETACH DELETE n", params={"g": group}
        )


def test_merge_snapshot_is_erased_from_either_side_on_a_real_sidecar(coord, sidecar):
    """merge_audit is NOT NULL, so the content is redacted rather than nulled -- either way gone."""
    survivor = f"test-erasure-surv-{uuidlib.uuid4()}"
    absorbed = f"test-erasure-abs-{uuidlib.uuid4()}"
    with sqlite3.connect(sidecar) as conn:
        conn.execute(
            "INSERT INTO merge_audit "
            "(recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json) "
            "VALUES (?,?,?,?,?)",
            ("2026-08-18T00:00:00+00:00", survivor, absorbed, 0.91,
             '{"content":"absorbed node secret"}'),
        )
        conn.commit()

    coord.erase_memory(absorbed)

    with sqlite3.connect(sidecar) as conn:
        snapshot = conn.execute(
            "SELECT snapshot_json FROM merge_audit WHERE absorbed_uuid = ?", (absorbed,)
        ).fetchone()[0]
    assert "absorbed node secret" not in (snapshot or "")


def test_membership_capture_failure_leaves_the_real_partition_intact(
    coord, live_repo, sidecar
):
    """The counterexample the fake cannot prove: a real partition SURVIVES a failed capture.

    The old code turned any capture error into an empty member set and carried on, so this
    scenario ended with the partition deleted, its uuid-keyed sidecar content never purged, and
    ``erased`` returned -- and by then nothing could enumerate the members to finish the job.
    The property is not "the error is logged", it is "the graph is still there afterwards".
    """
    from unittest.mock import patch

    from menhir.services.erasure_coordinator import MEMBERSHIP_CAPTURE_FAILED

    group = f"test-erasure-abstain-{uuidlib.uuid4()}"
    members = [f"{group}-m{i}" for i in range(3)]
    live_repo.execute(
        "UNWIND $uuids AS u CREATE (n:Entity {uuid:u, group_id:$g, test_tag:$g})",
        params={"uuids": members, "g": group},
    )
    try:
        for m in members:
            _seed_revision(sidecar, m, f"content of {m}")

        with patch.object(
            coord.graph_adapter,
            "capture_namespace_uuids",
            side_effect=ConnectionError("neo4j went away mid-erasure"),
        ):
            out = coord.erase_namespace(group)

        assert out["reason"] == MEMBERSHIP_CAPTURE_FAILED
        # Nothing destroyed: every member still in the real graph.
        for m in members:
            assert _exists(live_repo, m) is True
        # Sidecar content untouched, so a later successful run can still erase it.
        for m in members:
            assert _revisions(sidecar, m) == [(f"content of {m}", f"content of {m}")]
        # No durable intent, so no reconciler will try to resume a half-done erasure.
        with sqlite3.connect(sidecar) as conn:
            unresolved = conn.execute(
                "SELECT COUNT(*) FROM graph_operations "
                "WHERE operation_kind = 'EXPLICIT_ERASURE'"
            ).fetchone()[0]
        assert unresolved == 0

        # And the retry the abstain promises is real: the same call succeeds once the graph
        # answers again, which is what makes failing closed safe rather than merely strict.
        retry = coord.erase_namespace(group)
        assert retry["reason"] in (ERASED, GRAPH_ALREADY_ABSENT)
        for m in members:
            assert _exists(live_repo, m) is False
            assert _revisions(sidecar, m) == [(None, None)]
    finally:
        live_repo.execute(
            "MATCH (n) WHERE n.test_tag = $g DETACH DELETE n", params={"g": group}
        )


def test_namespace_erasure_reaches_episode_keyed_sidecar_content(coord, live_repo, sidecar):
    """Episodic members own episode_uuid-keyed sidecar rows, on a real partition.

    The existing namespace test seeds only ``memory_revisions``, i.e. the node_uuid path, so it
    could not see this: a captured uuid went into ``node_uuids`` alone and every
    ``episode_uuid``-keyed column was skipped while the outcome still looked clean.
    """
    group = f"test-erasure-ep-{uuidlib.uuid4()}"
    episode = f"{group}-episode"
    live_repo.execute(
        "CREATE (n:Episodic {uuid:$u, group_id:$g, namespace:$g, test_tag:$g})",
        params={"u": episode, "g": group},
    )
    try:
        with sqlite3.connect(sidecar) as conn:
            conn.execute(
                "INSERT INTO failure_events (recorded_at, operation, episode_uuid, "
                "failure_stage, classification, retryable, error, details_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("t", "add_episode", episode, "extract", "transient", 0,
                 "boom", "verbatim user content"),
            )
            conn.execute(
                "INSERT INTO lifecycle_events (recorded_at, phase, event, status, "
                "episode_uuid, details_json) VALUES (?,?,?,?,?,?)",
                ("t", "ingest", "started", "ok", episode, "more user content"),
            )
            conn.commit()

        out = coord.erase_namespace(group)
        assert out["reason"] in (ERASED, GRAPH_ALREADY_ABSENT)

        with sqlite3.connect(sidecar) as conn:
            assert conn.execute(
                "SELECT details_json FROM failure_events WHERE episode_uuid=?", (episode,)
            ).fetchone()[0] is None
            assert conn.execute(
                "SELECT details_json FROM lifecycle_events WHERE episode_uuid=?", (episode,)
            ).fetchone()[0] is None
    finally:
        live_repo.execute(
            "MATCH (n) WHERE n.test_tag = $g DETACH DELETE n", params={"g": group}
        )
