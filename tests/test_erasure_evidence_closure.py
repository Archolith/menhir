"""Erasing a memory erases the captured evidence that carries its words (#170 item 5).

Found by the E2E-14 deletion lane: `DELETE /api/memory/{uuid}` removed the memory and its scalar
claim, but left the admitted `:TurnEvidence`, its verbatim projection episode and its event
assertions. The next Phase 3 pass re-perceived the turn and the deleted claim came back; the
orphaned event assertion made the timeline rebuild fail closed.
"""

from __future__ import annotations

import asyncio
import uuid as uuidlib

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.infrastructure.view_projection_repair import ViewProjectionRepairRepository
from menhir.services import scheduler_tasks
from menhir.services.erasure_coordinator import ERASED, ErasureCoordinator
from tests.test_consolidate_personal_memory import (
    _empty_llm,
    _eps,
    _ScalarGraph,
)


class EvidenceAdapter:
    """Graph stand-in: twins plus the evidence each node carries."""

    def __init__(self, twins: dict[str, list[str]], evidence: dict[str, list[str]], *,
                 fail_evidence: bool = False, fail_delete: set[str] | None = None) -> None:
        self.twins = twins
        self.evidence = evidence
        self.fail_evidence = fail_evidence
        self.fail_delete = fail_delete or set()
        self.present = {u for m in (twins, evidence) for k, vs in m.items() for u in [k, *vs]}
        self.deleted: list[str] = []

    def episodic_twin_uuids(self, node_uuid: str) -> list[str]:
        return list(self.twins.get(node_uuid, []))

    def erasure_evidence_uuids(self, node_uuid: str) -> list[str]:
        if self.fail_evidence:
            raise RuntimeError("graph unavailable")
        return list(self.evidence.get(node_uuid, []))

    def delete_memory(self, node_uuid: str) -> bool:
        if node_uuid in self.fail_delete:
            raise RuntimeError(f"crash deleting {node_uuid}")
        self.deleted.append(node_uuid)
        was = node_uuid in self.present
        self.present.discard(node_uuid)
        return was


def _coordinator(tmp_path, adapter) -> tuple[ErasureCoordinator, object]:
    db = tmp_path / "closure.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    return ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    ), db


@pytest.mark.unit
def test_memory_erasure_also_erases_its_turn_projection_and_projection_twin(tmp_path) -> None:
    adapter = EvidenceAdapter(
        twins={"queue-1": ["graphiti-1"]},
        evidence={"queue-1": ["turn-1", "proj-1", "proj-graphiti-1"], "graphiti-1": []},
    )
    coord, _db = _coordinator(tmp_path, adapter)

    out = coord.erase_memory("queue-1")

    assert out["reason"] == ERASED
    assert adapter.deleted == ["queue-1", "graphiti-1", "turn-1", "proj-1", "proj-graphiti-1"]


@pytest.mark.unit
def test_evidence_shared_by_the_memory_and_its_twin_is_erased_once(tmp_path) -> None:
    adapter = EvidenceAdapter(
        twins={"queue-1": ["graphiti-1"]},
        evidence={"queue-1": ["turn-1", "proj-1"], "graphiti-1": ["turn-1", "proj-1"]},
    )
    coord, _db = _coordinator(tmp_path, adapter)
    coord.erase_memory("queue-1")
    assert adapter.deleted == ["queue-1", "graphiti-1", "turn-1", "proj-1"]


@pytest.mark.unit
def test_evidence_lookup_failure_fails_closed_before_prepare(tmp_path) -> None:
    adapter = EvidenceAdapter(twins={}, evidence={"queue-1": ["turn-1"]}, fail_evidence=True)
    coord, db = _coordinator(tmp_path, adapter)

    with pytest.raises(RuntimeError, match="graph unavailable"):
        coord.erase_memory("queue-1")

    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM graph_operations").fetchone()[0] == 0
    assert adapter.deleted == []


@pytest.mark.unit
def test_crash_before_the_turn_delete_is_replayed_for_the_turn(tmp_path) -> None:
    adapter = EvidenceAdapter(twins={}, evidence={"queue-1": ["turn-1", "proj-1"]},
                              fail_delete={"turn-1"})
    coord, db = _coordinator(tmp_path, adapter)

    with pytest.raises(RuntimeError, match="crash deleting turn-1"):
        coord.erase_memory("queue-1")

    import sqlite3
    with sqlite3.connect(db) as conn:
        [(op_id, state)] = conn.execute("SELECT op_id, state FROM graph_operations").fetchall()
    assert state == "PREPARED"

    adapter.fail_delete.clear()
    outcome, _diagnostics = coord.replay_prepared_row({"op_id": op_id})

    assert outcome == "REPLAYED"
    assert {"turn-1", "proj-1"} <= set(adapter.deleted)
    assert "turn-1" not in adapter.present


class _RowsNeo4j:
    def __init__(self, *responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict | None = None, **_kwargs) -> list[dict]:
        self.calls.append((query, dict(params or {})))
        return self.responses.pop(0) if self.responses else []


@pytest.mark.unit
def test_evidence_lookup_keeps_only_the_target_silo_and_adds_projection_twins() -> None:
    rows = [
        {"n_namespace": "ns", "n_group_id": None, "turn_id": "turn-1", "t_namespace": "ns",
         "projection_uuid": "proj-1", "p_namespace": "ns", "p_group_id": None},
        # A turn in another silo is never erased under this memory's authorization.
        {"n_namespace": "ns", "n_group_id": None, "turn_id": "turn-x", "t_namespace": "other",
         "projection_uuid": "proj-x", "p_namespace": "other", "p_group_id": None},
        {"n_namespace": "ns", "n_group_id": None, "turn_id": None, "t_namespace": None,
         "projection_uuid": None, "p_namespace": None, "p_group_id": None},
    ]
    twin_rows = [{"n_namespace": "ns", "n_group_id": None, "uuid": "proj-graphiti-1",
                  "t_namespace": None, "t_group_id": "ns"}]
    neo4j = _RowsNeo4j(rows, twin_rows)
    repo = EpisodeLifecycleRepository()
    repo.neo4j = neo4j

    assert repo.erasure_evidence_uuids("queue-1") == ["turn-1", "proj-1", "proj-graphiti-1"]
    query = neo4j.calls[0][0]
    assert "(n)-[:ADMITTED_ON]->(admitted:TurnEvidence)" in query
    assert "p.evidence_projection_of = t.turn_id" in query


@pytest.mark.unit
def test_cascade_deletes_event_assertions_and_scrubs_pending_text() -> None:
    neo4j = _RowsNeo4j([{"namespace_keys": ["ns"]}], [{
        "memory_touched": 1, "assertions_deleted": 0, "heads_deleted": 0,
        "dependent_views_retired": 1, "dependent_views_scrubbed": 1, "view_repairs_created": 1,
        "watermarks_reset": 1, "event_assertions_deleted": 2, "event_heads_deleted": 1,
        "repairs": [],
    }])
    repo = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]

    out = repo.delete_memory_with_scalar_cascade("turn-1", operation_id="op-1")

    query = neo4j.calls[1][0]
    assert "OPTIONAL MATCH (event:TypedEventAssertion)" in query
    assert "FOREACH (event IN doomed_events | DETACH DELETE event)" in query
    assert "FOREACH (head IN event_heads | DETACH DELETE head)" in query
    # The timeline that shows a deleted event is retired with it.
    assert "MATCH (v)-[:EVENT_HISTORY_ENTRY]->(entry:TypedEventAssertion)" in query
    # A pending episode is kept to stop its queue, without its text.
    pending = query.index("n.processing_substage = 'deleted_by_user'")
    assert query.index("REMOVE n.content, n.content_embedding", pending) > pending
    # Events are deleted before the evidence node, inside the same statement.
    assert query.index("DETACH DELETE event") < query.index("WITH n WHERE n:TurnEvidence")
    assert out["event_assertions_deleted"] == 2 and out["event_heads_deleted"] == 1


@pytest.mark.unit
def test_repair_claim_can_be_scoped_to_namespaces() -> None:
    neo4j = _RowsNeo4j([])
    repo = ViewProjectionRepairRepository(neo4j)  # type: ignore[arg-type]

    repo.claim_pending(owner_id="pass-1", limit=5, lease_seconds=60, namespaces=["ns-a", ""])

    query, params = neo4j.calls[0]
    assert "IN $namespaces" in query
    assert params["namespaces"] == ["ns-a", "default"]
    lock_at = query.index("SET r.claim_lock_version")
    assert query.index("IN $namespaces") < lock_at


class _RepairGraph(_ScalarGraph):
    def __init__(self, *args, claimed: int = 1, fail: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.repair_calls: list[tuple[list[str], dict]] = []
        self._claimed = claimed
        self._fail = fail

    def run_view_projection_repairs(self, namespaces, **kwargs):
        self.repair_calls.append((list(namespaces), kwargs))
        if self._fail:
            raise RuntimeError("neo4j unavailable")
        return {"claimed": self._claimed, "complete": self._claimed, "failed": 0,
                "terminal_not_rebuildable": 0, "lost_claim": 0, "results": []}


def _scalar_pass(graph, **kwargs):
    return asyncio.run(scheduler_tasks.consolidate_personal_memory(
        graph, llm_complete=_empty_llm, k=1, enable_scalar_state=True, **kwargs))


@pytest.mark.unit
def test_pass_runs_repairs_for_its_own_namespaces_after_the_lanes() -> None:
    graph = _RepairGraph(scalar_dirty=[], entities_by_ep={}, episodes_by_ns={"tenant-a": _eps("spent money")})

    out = _scalar_pass(graph, namespaces=["tenant-a"])

    assert [ns for ns, _kw in graph.repair_calls] == [["tenant-a"]]
    assert graph.repair_calls[0][1]["owner_id"].startswith("consolidation:")
    assert out["view_repairs"]["claimed"] == 1 and out["view_repairs"]["complete"] == 1


@pytest.mark.unit
def test_pass_without_repair_work_keeps_its_result_shape() -> None:
    graph = _RepairGraph(scalar_dirty=[], entities_by_ep={}, episodes_by_ns={}, claimed=0)
    out = _scalar_pass(graph, namespaces=["tenant-a"])
    assert "view_repairs" not in out


@pytest.mark.unit
def test_repair_failure_does_not_fail_the_pass() -> None:
    graph = _RepairGraph(scalar_dirty=[], entities_by_ep={}, episodes_by_ns={}, fail=True)
    out = _scalar_pass(graph, namespaces=["tenant-a"])
    assert "view_repairs" not in out and len(graph.repair_calls) == 1


# ---------------------------------------------------------------- online (real graph)


@pytest.mark.online
def test_real_graph_memory_erasure_leaves_no_turn_text_or_event(test_neo4j_repo, tmp_path) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    tag = f"test-closure-{uuidlib.uuid4()}"
    ns, other = f"{tag}-ns", f"{tag}-other"
    secret = f"I have 7 secret mugs {tag}"
    ids = {k: f"{tag}-{k}" for k in ("memory", "turn", "proj", "event", "head", "view",
                                      "kept_turn", "kept_event", "foreign_turn")}
    repo = test_neo4j_repo
    repo.execute(
        """
        CREATE (t:TurnEvidence {turn_id:$turn, turn_key:$turn, namespace:$ns, role:'user',
                                declarant:'user', text:$secret, test_tag:$tag})
        CREATE (m:Episodic {uuid:$memory, namespace:$ns, processing_state:'READY',
                            content:'User owns mugs', test_tag:$tag})
        CREATE (m)-[:ADMITTED_ON]->(t)
        CREATE (p:Episodic {uuid:$proj, namespace:$ns, evidence_projection_of:$turn,
                            is_evidence_projection:true, processing_state:'PENDING',
                            content:$secret, test_tag:$tag})
        CREATE (p)-[:ADMITTED_ON]->(t)
        CREATE (e:TypedEventAssertion {assertion_id:$event, turn_evidence_uuid:$turn,
                                       namespace:$ns, stated_span:$secret, test_tag:$tag})
        CREATE (t)-[:FOUNDS]->(e)
        CREATE (h:TypedEventAssertionHead {source_key:$head, test_tag:$tag})
        CREATE (h)-[:HAS_VERSION]->(e)
        CREATE (v:Entity {uuid:$view, view_kind:'timeline', group_id:$ns, view_current:true,
                          view_key:$view, view_predicate:'acquired', view_subject_uuid:'user',
                          test_tag:$tag})
        CREATE (v)-[:EVENT_HISTORY_ENTRY]->(e)
        CREATE (k:TurnEvidence {turn_id:$kept_turn, turn_key:$kept_turn, namespace:$ns,
                                role:'user', text:'I have a bike', test_tag:$tag})
        CREATE (ke:TypedEventAssertion {assertion_id:$kept_event, turn_evidence_uuid:$kept_turn,
                                        namespace:$ns, test_tag:$tag})
        CREATE (k)-[:FOUNDS]->(ke)
        CREATE (v)-[:EVENT_HISTORY_ENTRY]->(ke)
        CREATE (f:TurnEvidence {turn_id:$foreign_turn, turn_key:$foreign_turn, namespace:$other,
                                role:'user', text:$secret, test_tag:$tag})
        CREATE (m)-[:ADMITTED_ON]->(f)
        """,
        params={**ids, "ns": ns, "other": other, "secret": secret, "tag": tag},
    )
    try:
        adapter = MemoryGraphAdapter(neo4j=repo)
        assert adapter.erasure_evidence_uuids(ids["memory"]) == [ids["turn"], ids["proj"]]

        db = tmp_path / "closure-live.db"
        McpTelemetryStore(db_path=db)._ensure_ready()
        coord = ErasureCoordinator(graph_adapter=adapter,
                                   journal=GraphOperationsJournal(db_path=db),
                                   subjects=ErasureSubjectStore(db_path=db))
        assert coord.erase_memory(ids["memory"])["reason"] == ERASED

        def count(cypher: str, **params) -> int:
            return int(repo.execute(cypher, params={"tag": tag, **params})[0]["c"])

        # No node of this namespace still carries the erased words.
        assert count("MATCH (n {test_tag:$tag}) WHERE coalesce(n.namespace, n.group_id) = $ns "
                     "AND any(k IN keys(n) WHERE (n[k] IS :: STRING AND n[k] CONTAINS $s) "
                     "OR (n[k] IS :: LIST<STRING> AND any(x IN n[k] WHERE x CONTAINS $s))) "
                     "RETURN count(n) AS c",
                     ns=ns, s="secret mugs") == 0
        for gone in ("memory", "turn", "event", "head"):
            assert count("MATCH (n {test_tag:$tag}) WHERE n.uuid = $u OR n.turn_id = $u "
                         "OR n.assertion_id = $u OR n.source_key = $u RETURN count(n) AS c",
                         u=ids[gone]) == 0, gone
        # The pending projection is gone, or at most a FAILED shell without its text.
        rows = repo.execute("MATCH (p:Episodic {uuid:$u}) RETURN p.processing_state AS s, "
                            "p.content AS c", params={"u": ids["proj"]})
        assert not rows or (rows[0]["s"] == "FAILED" and rows[0]["c"] is None)
        # The timeline that showed the event is retired; the surviving event is untouched.
        rows = repo.execute("MATCH (v:Entity {uuid:$u}) RETURN v.retired AS r", params={"u": ids["view"]})
        assert rows and rows[0]["r"] is True
        assert count("MATCH (e:TypedEventAssertion {assertion_id:$u}) RETURN count(e) AS c",
                     u=ids["kept_event"]) == 1
        # The other silo's turn is never erased under this memory's authorization.
        assert count("MATCH (t:TurnEvidence {turn_id:$u}) RETURN count(t) AS c",
                     u=ids["foreign_turn"]) == 1
    finally:
        repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
