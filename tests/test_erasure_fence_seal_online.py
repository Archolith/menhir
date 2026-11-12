"""Online tests for the erase-side seal and release (P4 design note, "Seal and release").

Real Neo4j through ``test_neo4j_repo``; the sidecar journal is a per-test SQLite file. Every test uses
unique uuid4-based ids and namespaces and cleans up by namespace / namespace_key in ``finally``.
"""

from __future__ import annotations

import uuid as uuidlib
from pathlib import Path
from typing import Any

import pytest

from menhir.infrastructure.erasure_fence import (
    release_memory,
    release_orphaned_erasing_ops,
    seal_memory,
    seal_namespace,
)
from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ERASURE_KIND, ErasureCoordinator

SUCCESS_REASONS = frozenset({"erased", "graph_already_absent", "nothing_to_erase"})


class _Rig:
    def __init__(self, repo: Any, tmp_path: Path) -> None:
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        self.repo = repo
        self.tag = f"seal-{uuidlib.uuid4().hex[:8]}"
        self.ns = f"{self.tag}-ns"
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        self.db = tmp_path / "seal.db"
        McpTelemetryStore(db_path=self.db)._ensure_ready()
        self.journal = GraphOperationsJournal(db_path=self.db)
        self.subjects = ErasureSubjectStore(db_path=self.db)
        self.coord = self.make_coord(self.adapter)
        self.n = 0

    def make_coord(self, adapter: Any) -> ErasureCoordinator:
        return ErasureCoordinator(
            graph_adapter=adapter, journal=self.journal, subjects=self.subjects
        )

    def uid(self, what: str) -> str:
        self.n += 1
        return f"{self.tag}-{what}{self.n}"

    def seed_q(self) -> str:
        q = self.uid("q")
        self.adapter.create_pending_episode(
            episode_uuid=q,
            name=f"anchor-{q}",
            content=f"content of {q}",
            session_id=f"{self.tag}-session",
            user_id="seal-user",
            source="claude-code",
            source_confidence=0.5,
            namespace=self.ns,
        )
        return q

    def fence(self) -> dict[str, Any]:
        rows = self.repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) "
            "RETURN coalesce(f.erasure_epoch, 0) AS epoch, coalesce(f.erasing_ops, []) AS ops",
            params={"k": self.ns},
        )
        return dict(rows[0]) if rows else {"epoch": 0, "ops": []}

    def markers(self, uuid: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.repo.execute(
                "MATCH (m:ErasureMarker {subject_uuid: $u}) RETURN m.namespace_key AS key, m.op_id AS op_id, "
                "m.sealed_epoch AS sealed_epoch, m.released_epoch AS released_epoch, "
                "m.cascade_done AS cascade_done",
                params={"u": uuid},
            )
        ]

    def prepare_row(
        self, op_id: str, target: str | None, subjects: list[tuple[str, str]]
    ) -> None:
        with connect_telemetry_db(self.db) as conn:
            self.journal.prepare(
                operation_kind=ERASURE_KIND,
                request_json=f'{{"op_id":"{op_id}","kind":"{ERASURE_KIND}","targets":[]}}',
                target_uuid=target,
                target_key=None if target else f"erasure:test:{op_id}",
                op_id=op_id,
                conn=conn,
            )
            self.subjects.record_subjects(op_id, subjects, conn=conn)
            conn.commit()

    def cleanup(self) -> None:
        self.repo.execute(
            "MATCH (n) WHERE n.uuid STARTS WITH $tag OR n.turn_id STARTS WITH $tag "
            "OR n.group_id = $ns OR n.namespace = $ns OR n.namespace_key = $ns DETACH DELETE n",
            params={"tag": self.tag, "ns": self.ns},
        )


class _RaisingAdapter:
    """Delegates to the real adapter, but the cascade delete raises."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def delete_memory_detailed(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("cascade failed on purpose")


@pytest.mark.online
def test_erase_pending_queue_node_writes_marker_and_releases(
    test_neo4j_repo, tmp_path
) -> None:
    rig = _Rig(test_neo4j_repo, tmp_path)
    try:
        q = rig.seed_q()
        out = rig.coord.erase_memory(q)
        assert out["reason"] in SUCCESS_REASONS | {"erased_incomplete"}, out
        marks = rig.markers(q)
        assert len(marks) == 1 and marks[0]["key"] == rig.ns, marks
        assert (
            marks[0]["released_epoch"] is not None and marks[0]["cascade_done"] is True
        ), marks
        fence = rig.fence()
        assert fence["epoch"] == 2, fence  # seal + release
        assert fence["ops"] == [], fence
    finally:
        rig.cleanup()


@pytest.mark.online
def test_closure_covers_link_reservations_backlinked_twin_turn_and_projection(
    test_neo4j_repo, tmp_path
) -> None:
    rig = _Rig(test_neo4j_repo, tmp_path)
    op = uuidlib.uuid4().hex
    try:
        q, link, reserved, backlinked = (
            rig.uid(x) for x in ("q", "link", "resv", "back")
        )
        turn, proj, proj_twin = rig.uid("turn"), rig.uid("proj"), rig.uid("ptwin")
        foreign = rig.uid("foreign")
        rig.repo.execute(
            """
            CREATE (q:Episodic {uuid: $q, group_id: $ns, namespace: $ns, processing_state: 'READY',
                                resolved_episode_uuid: $link, reserved_twin_uuids: [$resv, $foreign]})
            CREATE (g:Episodic {uuid: $link, group_id: $ns, menhir_memory_uuid: $q})
            CREATE (b:Episodic {uuid: $back, group_id: $ns, menhir_memory_uuid: $q})
            CREATE (t:TurnEvidence {turn_id: $turn, namespace: $ns})
            CREATE (q)-[:ADMITTED_ON]->(t)
            CREATE (p:Episodic {uuid: $proj, group_id: $ns, namespace: $ns, evidence_projection_of: $turn,
                                processing_state: 'READY'})
            CREATE (pt:Episodic {uuid: $ptwin, group_id: $ns, menhir_memory_uuid: $proj})
            CREATE (f:Episodic {uuid: $foreign, group_id: $other})
            """,
            params={
                "q": q,
                "link": link,
                "resv": reserved,
                "back": backlinked,
                "turn": turn,
                "proj": proj,
                "ptwin": proj_twin,
                "foreign": foreign,
                "ns": rig.ns,
                "other": f"{rig.ns}-other",
            },
        )
        res = seal_memory(test_neo4j_repo, op, [q])
        try:
            assert res.namespace_keys == [rig.ns], res
            expected = {q, link, reserved, backlinked, turn, proj, proj_twin}
            assert expected <= set(res.closure_uuids), res
            assert foreign not in res.closure_uuids, res
            assert res.unfenced_inflight == [], res
            for u in expected:
                assert rig.markers(u), f"no marker for {u}"
            assert not rig.markers(foreign)
            assert rig.fence()["ops"] == [op]
        finally:
            released = release_memory(test_neo4j_repo, op)
        assert released == [rig.ns]
        assert rig.fence()["ops"] == []
        assert release_memory(test_neo4j_repo, op) == []  # idempotent
        assert rig.fence()["epoch"] == 2
    finally:
        rig.cleanup()
        rig.repo.execute(
            "MATCH (n:Episodic) WHERE n.group_id = $o DETACH DELETE n",
            params={"o": f"{rig.ns}-other"},
        )


@pytest.mark.online
def test_cascade_failure_still_releases_and_is_not_a_success(
    test_neo4j_repo, tmp_path
) -> None:
    rig = _Rig(test_neo4j_repo, tmp_path)
    try:
        q = rig.seed_q()
        coord = rig.make_coord(_RaisingAdapter(rig.adapter))
        reply: Any = None
        raised = False
        try:
            reply = coord.erase_memory(q)
        except RuntimeError:
            raised = True
        assert raised or reply.get("reason") not in SUCCESS_REASONS, reply
        assert rig.fence()["ops"] == []
        assert rig.markers(q), "markers must stay after a failed cascade"
    finally:
        rig.cleanup()


@pytest.mark.online
def test_replay_of_prepared_row_releases_a_crashed_seal(
    test_neo4j_repo, tmp_path
) -> None:
    rig = _Rig(test_neo4j_repo, tmp_path)
    try:
        q = rig.seed_q()
        op = uuidlib.uuid4().hex
        rig.prepare_row(op, q, [("NODE_UUID", q)])
        seal_memory(test_neo4j_repo, op, [q])  # crash: no release
        assert rig.fence()["ops"] == [op]
        row = rig.journal.get(op)
        assert row is not None and row["state"] == "PREPARED"
        rig.coord.replay_prepared_row(row)
        assert rig.fence()["ops"] == []
    finally:
        rig.cleanup()


@pytest.mark.online
def test_orphan_release_clears_terminal_ops_holds_prepared_and_fresh_absent_ops(
    test_neo4j_repo, tmp_path
) -> None:
    """CF-324 / B-8: an op absent from this journal may be another sidecar's live erase, so it is
    released only once its markers are older than the grace window; with no markers it is held."""
    rig = _Rig(test_neo4j_repo, tmp_path)
    try:
        terminal, absent_ns, prepared, absent_mem = (uuidlib.uuid4().hex for _ in range(4))
        rig.prepare_row(terminal, None, [("NAMESPACE", rig.ns)])
        rig.prepare_row(prepared, None, [("NAMESPACE", rig.ns)])
        with connect_telemetry_db(rig.db) as conn:
            conn.execute(
                "UPDATE graph_operations SET state = 'COMMITTED' WHERE op_id = ?",
                (terminal,),
            )
            conn.commit()
        for op in (terminal, absent_ns, prepared):
            seal_namespace(test_neo4j_repo, op, rig.ns)
        seal_memory(test_neo4j_repo, absent_mem, [rig.seed_q()])  # markers, no journal row
        assert sorted(rig.fence()["ops"]) == sorted([terminal, absent_ns, prepared, absent_mem])

        released = release_orphaned_erasing_ops(test_neo4j_repo, rig.journal)
        assert released == [terminal], released
        assert sorted(rig.fence()["ops"]) == sorted([absent_ns, prepared, absent_mem])

        # Past the grace window the marked absent op goes; the unmarked one is still held.
        released = release_orphaned_erasing_ops(test_neo4j_repo, rig.journal, absent_grace_s=0)
        assert released == [absent_mem], released
        assert sorted(rig.fence()["ops"]) == sorted([absent_ns, prepared])
    finally:
        rig.cleanup()


@pytest.mark.online
def test_enriching_claim_without_publish_protocol_is_unfenced_inflight(
    test_neo4j_repo, tmp_path
) -> None:
    rig = _Rig(test_neo4j_repo, tmp_path)
    try:
        q = rig.seed_q()
        rig.repo.execute(
            "MATCH (n:Episodic {uuid: $q}) SET n.processing_state = 'ENRICHING', "
            "n.processing_owner = 'w1', n.processing_lease_expires_at = datetime() + duration('PT1H')",
            params={"q": q},
        )
        out = rig.coord.erase_memory(q)
        assert out["reason"] == "erased_incomplete", out
        assert "unfenced_inflight" in out["incomplete_reasons"], out
        assert rig.fence()["ops"] == []
    finally:
        rig.cleanup()
