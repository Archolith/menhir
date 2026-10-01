"""Audit Q1 / #119: erasing a memory erases both of its `:Episodic` nodes.

Each memory has Menhir's queue node and the Graphiti episode it resolves to
(`resolved_episode_uuid`); both carry the verbatim content. Erasing either must erase both, the
twins must be inventoried before PREPARE so a crash between the two deletes is replayed, and a
twin lookup failure must not erase half the memory.
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ERASED, ErasureCoordinator


class TwinAdapter:
    """Graph stand-in with queue -> Graphiti episode pairs."""

    def __init__(self, twins: dict[str, list[str]], *, fail_delete: set[str] | None = None,
                 fail_lookup: bool = False) -> None:
        self.twins = twins
        self.present = {u for pair in twins.items() for u in [pair[0], *pair[1]]}
        self.fail_delete = fail_delete or set()
        self.fail_lookup = fail_lookup
        self.deleted: list[str] = []

    def episodic_twin_uuids(self, node_uuid: str) -> list[str]:
        if self.fail_lookup:
            raise RuntimeError("graph unavailable")
        return list(self.twins.get(node_uuid, []))

    def node_exists(self, node_uuid: str) -> bool:
        return node_uuid in self.present

    def delete_memory(self, node_uuid: str) -> bool:
        if node_uuid in self.fail_delete:
            raise RuntimeError(f"crash deleting {node_uuid}")
        self.deleted.append(node_uuid)
        was = node_uuid in self.present
        self.present.discard(node_uuid)
        return was


def _coordinator(tmp_path, adapter) -> tuple[ErasureCoordinator, object]:
    db = tmp_path / "twin.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    return coord, db


def _seed_revision(db, node_uuid: str, content: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memory_revisions "
            "(recorded_at, node_uuid, field, old_value, new_value, changed_by) "
            "VALUES (?,?,?,?,?,?)",
            ("2026-10-01T00:00:00+00:00", node_uuid, "content", content, content, "twin-test"),
        )
        conn.commit()


def _revisions(db, node_uuid: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid = ?", (node_uuid,)
        ).fetchall()


def _operations(db) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT op_id, state FROM graph_operations").fetchall()


# ---------------------------------------------------------------- unit (fake graph)


@pytest.mark.unit
@pytest.mark.parametrize("target,twin", [("queue-1", "graphiti-1"), ("graphiti-1", "queue-1")])
def test_erasing_either_node_erases_both_and_their_sidecar_rows(tmp_path, target, twin) -> None:
    adapter = TwinAdapter({"queue-1": ["graphiti-1"], "graphiti-1": ["queue-1"]})
    coord, db = _coordinator(tmp_path, adapter)
    _seed_revision(db, target, "verbatim memory")
    _seed_revision(db, twin, "verbatim memory")

    out = coord.erase_memory(target)

    assert out["reason"] == ERASED
    assert adapter.deleted == [target, twin]
    assert _revisions(db, target) == [(None, None)]
    assert _revisions(db, twin) == [(None, None)]


@pytest.mark.unit
def test_twin_lookup_failure_fails_closed_before_prepare(tmp_path) -> None:
    adapter = TwinAdapter({"queue-1": ["graphiti-1"]}, fail_lookup=True)
    coord, db = _coordinator(tmp_path, adapter)

    with pytest.raises(RuntimeError, match="graph unavailable"):
        coord.erase_memory("queue-1")

    assert adapter.deleted == []
    assert _operations(db) == []


@pytest.mark.unit
def test_crash_between_the_two_deletes_is_replayed_for_both(tmp_path) -> None:
    adapter = TwinAdapter({"queue-1": ["graphiti-1"]}, fail_delete={"graphiti-1"})
    coord, db = _coordinator(tmp_path, adapter)
    _seed_revision(db, "graphiti-1", "verbatim memory")

    with pytest.raises(RuntimeError, match="crash deleting graphiti-1"):
        coord.erase_memory("queue-1")

    [(op_id, state)] = _operations(db)
    assert state == "PREPARED"
    assert adapter.deleted == ["queue-1"]

    adapter.fail_delete.clear()  # the process restarts; the graph is reachable again
    outcome, _diagnostics = coord.replay_prepared_row({"op_id": op_id})

    assert outcome == "REPLAYED"
    assert "graphiti-1" in adapter.deleted
    assert _revisions(db, "graphiti-1") == [(None, None)]
    assert dict(_operations(db))[op_id] == "COMMITTED"


@pytest.mark.unit
def test_memory_without_a_twin_is_erased_as_before(tmp_path) -> None:
    adapter = TwinAdapter({"entity-1": []})
    coord, _db = _coordinator(tmp_path, adapter)
    assert coord.erase_memory("entity-1")["reason"] == ERASED
    assert adapter.deleted == ["entity-1"]


# ---------------------------------------------------------------- online (real graph, real adapter)


@pytest.mark.online
def test_real_graph_erasure_removes_both_episodes_but_never_a_foreign_twin(test_neo4j_repo, tmp_path) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    tag = f"test-twin-{uuidlib.uuid4()}"
    ns, other_ns = f"{tag}-ns", f"{tag}-other"
    queue, graphiti, foreign = f"{tag}-queue", f"{tag}-graphiti", f"{tag}-foreign"
    repo = test_neo4j_repo
    repo.execute(
        """
        CREATE (:Episodic {uuid:$graphiti, group_id:$ns, valid_at:datetime(), content:'secret', test_tag:$tag})
        CREATE (:Episodic {uuid:$queue, namespace:$ns, resolved_episode_uuid:$graphiti,
                           processing_state:'READY', content:'secret', test_tag:$tag})
        CREATE (:Episodic {uuid:$foreign, namespace:$other_ns, resolved_episode_uuid:$graphiti,
                           processing_state:'READY', content:'other silo', test_tag:$tag})
        """,
        params={"graphiti": graphiti, "queue": queue, "foreign": foreign, "ns": ns,
                "other_ns": other_ns, "tag": tag},
    )
    try:
        adapter = MemoryGraphAdapter(neo4j=repo)
        assert adapter.episodic_twin_uuids(queue) == [graphiti]
        # The other silo's node is never treated as this memory's twin.
        assert adapter.episodic_twin_uuids(graphiti) == [queue]

        db = tmp_path / "twin-live.db"
        McpTelemetryStore(db_path=db)._ensure_ready()
        coord = ErasureCoordinator(
            graph_adapter=adapter,
            journal=GraphOperationsJournal(db_path=db),
            subjects=ErasureSubjectStore(db_path=db),
        )
        assert coord.erase_memory(queue)["reason"] == ERASED

        def exists(uuid: str) -> bool:
            rows = repo.execute("MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": uuid})
            return int(rows[0]["c"]) > 0

        assert exists(queue) is False
        assert exists(graphiti) is False
        assert exists(foreign) is True
    finally:
        repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
