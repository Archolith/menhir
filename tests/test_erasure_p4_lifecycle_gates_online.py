"""Online tests for the P4 queue-node lifecycle gates (G-q, claim reservation, G-create, TypedAssertion).

Real Neo4j through ``test_neo4j_repo``. Every test seeds unique uuid4-based ids and namespaces and
cleans up by namespace / namespace_key / subject_uuid in ``finally``.
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any, Callable

import pytest

from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

pytestmark = pytest.mark.online


class _Rig:
    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.tag = f"p4g-{uuidlib.uuid4().hex[:10]}"
        self.ns = f"{self.tag}-ns"
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        self.uuids: list[str] = []
        self.n = 0

    def uid(self, what: str) -> str:
        self.n += 1
        value = f"{self.tag}-{what}{self.n}"
        self.uuids.append(value)
        return value

    def seed_pending(self) -> str:
        q = self.uid("q")
        self.adapter.create_pending_episode(
            episode_uuid=q,
            name=f"anchor-{q}",
            content=f"content {q}",
            session_id=f"{self.tag}-s",
            user_id="u",
            source="claude-code",
            source_confidence=0.5,
            namespace=self.ns,
        )
        return q

    def claim(self, q: str, worker: str = "w1") -> dict[str, Any] | None:
        return self.adapter.claim_pending_episode(
            q, max_attempts=3, worker_id=worker, lease_seconds=300
        )

    def mark(self, subject: str, key: str | None = None) -> None:
        self.repo.execute(
            "CREATE (:ErasureMarker {namespace_key: $k, subject_uuid: $u, op_id: $op})",
            params={"k": key or self.ns, "u": subject, "op": f"{self.tag}-op"},
        )

    def state(self, q: str) -> dict[str, Any]:
        rows = self.repo.execute(
            "MATCH (n:Episodic {uuid: $u}) RETURN n.processing_state AS state, "
            "n.processing_owner AS owner, n.processing_error AS error, "
            "n.processing_stage AS stage, n.content_embedding AS emb, "
            "n.processing_attempts AS attempts, n.reserved_twin_uuids AS reserved, "
            "n.publish_protocol AS protocol",
            params={"u": q},
        )
        return dict(rows[0]) if rows else {}

    def cleanup(self) -> None:
        for query in (
            "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n",
            "MATCH (m:ErasureMarker) WHERE m.namespace_key = $ns OR m.subject_uuid IN $ids DETACH DELETE m",
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) DETACH DELETE f",
            "MATCH (n) WHERE n.uuid IN $ids OR n.turn_id IN $ids DETACH DELETE n",
        ):
            self.repo.execute(query, params={"ns": self.ns, "ids": self.uuids})


@pytest.fixture
def rig(test_neo4j_repo):
    r = _Rig(test_neo4j_repo)
    try:
        yield r
    finally:
        r.cleanup()


def test_claim_reserves_twin_and_sets_protocol(rig: _Rig) -> None:
    q = rig.seed_pending()
    row = rig.claim(q)
    assert row is not None
    reserved = row["reserved_twin_uuid"]
    assert reserved and uuidlib.UUID(reserved)
    assert row["publish_protocol"] == 2
    assert row["resolved_episode_uuid"] is None
    assert row["fence_namespace_key"] == rig.ns
    assert rig.state(q)["reserved"] == [reserved]
    # A re-claim after a requeue appends a second reservation; the first stays recorded.
    assert rig.adapter.mark_episode_pending(q, worker_id="w1") is True
    row2 = rig.claim(q, "w2")
    assert row2 is not None
    assert rig.state(q)["reserved"] == [reserved, row2["reserved_twin_uuid"]]


def test_marked_queue_node_is_not_claimable(rig: _Rig) -> None:
    q = rig.seed_pending()
    rig.mark(q)
    assert rig.claim(q) is None
    assert rig.state(q)["state"] == "PENDING"
    assert (
        rig.adapter.list_pending_episode_uuids(max_attempts=3, limit=500).count(q) == 0
    )


def _seed_enriching(rig: _Rig) -> tuple[str, Any]:
    q = rig.seed_pending()
    row = rig.claim(q)
    assert row is not None
    return q, row["processing_started_at"]


def _writers(
    rig: _Rig, q: str, started: Any
) -> list[tuple[str, Callable[[], Any], Callable[[Any], bool]]]:
    a = rig.adapter
    miss = lambda r: not r  # noqa: E731
    return [
        (
            "mark_episode_ready",
            lambda: a.mark_episode_ready(
                q,
                worker_id="w1",
                resolved_episode_uuid="x",
                nodes_touched=1,
                edges_touched=1,
                claim_started_at=started,
            ),
            miss,
        ),
        (
            "mark_episode_verbatim_skipped",
            lambda: a.mark_episode_verbatim_skipped(
                q, worker_id="w1", claim_started_at=started
            ),
            miss,
        ),
        (
            "mark_episode_store_only",
            lambda: a.mark_episode_store_only(
                q, worker_id="w1", claim_started_at=started, by_turn_role=True
            ),
            miss,
        ),
        (
            "mark_episode_failed",
            lambda: a.mark_episode_failed(
                q, "boom", worker_id="w1", claim_started_at=started
            ),
            miss,
        ),
        (
            "mark_episode_pending",
            lambda: a.mark_episode_pending(q, worker_id="w1"),
            miss,
        ),
        (
            "set_episode_content_embedding",
            lambda: a.set_episode_content_embedding(q, [0.1, 0.2], "m"),
            miss,
        ),
        (
            "update_episode_processing",
            lambda: a.update_episode_processing(q, worker_id="w1", stage="extracting"),
            miss,
        ),
        (
            "update_episode_processing_unfenced",
            lambda: a.update_episode_processing(q, stage="extracting"),
            miss,
        ),
        (
            "touch_heartbeat_legacy",
            lambda: a.touch_episode_processing_heartbeat(q, worker_id="w1"),
            miss,
        ),
        (
            "touch_heartbeat_renew",
            lambda: a.touch_episode_processing_heartbeat(
                q,
                worker_id="w1",
                claim_started_at=started,
                lease_seconds=60,
                max_enrichment_seconds=600,
            ),
            miss,
        ),
        ("increment_episode_llm_usage", lambda: a.increment_episode_llm_usage(q), miss),
        (
            "force_release_episode_lease",
            lambda: a.force_release_episode_lease(q, max_attempts=3),
            miss,
        ),
        (
            "release_worker_episode_leases",
            lambda: a.release_worker_episode_leases("w1", max_attempts=3),
            lambda r: int(r or 0) == 0,
        ),
        (
            "reset_stale_enriching_episodes",
            lambda: _expire_then(
                rig, q, lambda: a.reset_stale_enriching_episodes(max_attempts=3)
            ),
            lambda r: int(r or 0) == 0,
        ),
        (
            "reset_orphaned_enriching_episodes",
            lambda: _orphan_then(
                rig, q, lambda: a.reset_orphaned_enriching_episodes(max_attempts=3)
            ),
            lambda r: int(r or 0) == 0,
        ),
    ]


def _expire_then(rig: _Rig, q: str, call: Callable[[], Any]) -> Any:
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_lease_expires_at = datetime() - duration('PT1H')",
        params={"u": q},
    )
    return call()


def _orphan_then(rig: _Rig, q: str, call: Callable[[], Any]) -> Any:
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_owner = null", params={"u": q}
    )
    try:
        return call()
    finally:
        rig.repo.execute(
            "MATCH (n:Episodic {uuid: $u}) WHERE n.processing_owner IS NULL SET n.processing_owner = 'w1'",
            params={"u": q},
        )


def test_marked_enriching_node_matches_no_writer(rig: _Rig) -> None:
    q, started = _seed_enriching(rig)
    rig.mark(q)
    before = rig.state(q)
    for name, call, is_miss in _writers(rig, q, started):
        result = call()
        assert is_miss(result), f"{name} wrote a marked queue node: {result!r}"
    after = rig.state(q)
    assert after["state"] == before["state"] == "ENRICHING"
    assert after["owner"] == before["owner"] == "w1"
    assert after["stage"] == before["stage"]
    assert after["emb"] is None


def test_marked_failed_and_pending_nodes_match_no_retry_or_reset_writer(
    rig: _Rig,
) -> None:
    failed = rig.seed_pending()
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'FAILED', n.processing_attempts = 1",
        params={"u": failed},
    )
    exhausted = rig.seed_pending()
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_attempts = 99, n.transient_retries = 99",
        params={"u": exhausted},
    )
    ready = rig.seed_pending()
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'READY', n.enriched_nodes_touched = 0, "
        "n.enriched_edges_touched = 0",
        params={"u": ready},
    )
    for q in (failed, exhausted, ready):
        rig.mark(q)
    a = rig.adapter
    assert a.force_reset_failed_episode(failed) is False
    assert a.fail_exhausted_pending_episodes(max_attempts=3) == 0
    assert a.fail_transient_exhausted_pending_episodes() == 0
    assert a.reset_zero_extraction_episodes() == 0
    assert failed not in [
        r["uuid"] for r in a.fetch_failed_episode_retry_candidates(500)
    ]
    assert exhausted not in a.list_pending_episode_uuids(max_attempts=1000, limit=500)
    assert rig.state(failed)["state"] == "FAILED"
    assert rig.state(exhausted)["state"] == "PENDING"
    assert rig.state(ready)["state"] == "READY"


def test_reenable_verbatim_skips_skips_marked_client(rig: _Rig) -> None:
    q = rig.seed_pending()
    rig.repo.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'READY', "
        "n.extraction_skipped_for = $gone",
        params={"u": q, "gone": f"{rig.tag}-missing-projection"},
    )
    rig.mark(q)
    assert rig.adapter.reenable_verbatim_skips(limit=500) == []
    assert rig.state(q)["state"] == "READY"


def test_linked_twin_artifact_returns_link_set_and_none_when_marked(rig: _Rig) -> None:
    q = rig.seed_pending()
    twin, ent, other_ent, edge = (
        rig.uid("twin"),
        rig.uid("ent"),
        rig.uid("ent"),
        rig.uid("edge"),
    )
    rig.repo.execute(
        """
        MATCH (q:Episodic {uuid: $q}) SET q.resolved_episode_uuid = $twin
        CREATE (g:Episodic {uuid: $twin, name: 'twin', namespace: $ns, group_id: $ns})
        CREATE (a:Entity {uuid: $ent, name: 'a', namespace: $ns, group_id: $ns})
        CREATE (b:Entity {uuid: $other, name: 'b', namespace: $ns, group_id: $ns})
        CREATE (g)-[:MENTIONS]->(a)
        CREATE (a)-[:RELATES_TO {uuid: $edge, fact: 'f', episodes: [$twin]}]->(b)
        """,
        params={
            "q": q,
            "twin": twin,
            "ent": ent,
            "other": other_ent,
            "edge": edge,
            "ns": rig.ns,
        },
    )
    got = rig.adapter.linked_twin_artifact(q)
    assert got is not None
    assert got["resolved_episode_uuid"] == twin
    assert got["entity_uuids"] == [ent]
    assert got["edge_uuids"] == [edge]
    assert got["namespace"] == rig.ns
    assert got["fence_namespace_key"] == rig.ns
    rig.mark(q)
    assert rig.adapter.linked_twin_artifact(q) is None


def test_linked_twin_artifact_none_without_link_or_twin(rig: _Rig) -> None:
    q = rig.seed_pending()
    assert rig.adapter.linked_twin_artifact(q) is None
    rig.repo.execute(
        "MATCH (q:Episodic {uuid: $q}) SET q.resolved_episode_uuid = $gone",
        params={"q": q, "gone": f"{rig.tag}-no-such-twin"},
    )
    assert rig.adapter.linked_twin_artifact(q) is None


def _fence(rig: _Rig, key: str) -> dict[str, Any]:
    rows = rig.repo.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) RETURN coalesce(f.erasure_epoch, 0) AS epoch",
        params={"k": key},
    )
    return dict(rows[0]) if rows else {}


def _start_erase(rig: _Rig, key: str) -> None:
    rig.repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $k}) "
        "ON CREATE SET f.generation = 0, f.created_at = datetime() "
        "SET f.erasure_epoch = coalesce(f.erasure_epoch, 0) + 1, f.erasing_ops = [$op]",
        params={"k": key, "op": f"{rig.tag}-op"},
    )


def test_ensure_self_entity_stamps_created_epoch_and_refuses_while_erasing(
    rig: _Rig,
) -> None:
    self_uuid = self_uuid_for_namespace(rig.ns)
    rig.uuids.append(self_uuid)
    rig.repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $k}) "
        "ON CREATE SET f.generation = 0, f.created_at = datetime() SET f.erasure_epoch = 4",
        params={"k": rig.ns},
    )
    assert rig.adapter.ensure_self_entity(rig.ns) == self_uuid
    rows = rig.repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN n.created_epoch AS e",
        params={"u": self_uuid},
    )
    assert rows[0]["e"] == 4
    # Existing node while erasing: refused too (the statement is gated before the MERGE).
    _start_erase(rig, rig.ns)
    assert rig.adapter.ensure_self_entity(rig.ns) is None
    # A brand-new namespace under an erase creates nothing.
    ns2 = f"{rig.tag}-ns2"
    self2 = self_uuid_for_namespace(ns2)
    rig.uuids.append(self2)
    try:
        _start_erase(rig, ns2)
        assert rig.adapter.ensure_self_entity(ns2) is None
        assert (
            rig.repo.execute(
                "MATCH (n:Entity {uuid: $u}) RETURN n", params={"u": self2}
            )
            == []
        )
    finally:
        rig.repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) DETACH DELETE f",
            params={"k": ns2},
        )


def _seed_turn(rig: _Rig) -> str:
    turn = rig.uid("turn")
    rig.repo.execute(
        "CREATE (t:TurnEvidence {turn_id: $t, role: 'user', declarant: 'user', text: 'I own 3 coins', "
        "namespace: $ns, recorded_at: datetime()})",
        params={"t": turn, "ns": rig.ns},
    )
    return turn


def _project(rig: _Rig, turn: str, projection: str) -> str | None:
    return rig.adapter.create_evidence_projection(
        turn_evidence_uuid=turn,
        projection_uuid=projection,
        name="proj",
        session_id=f"{rig.tag}-s",
        user_id="u",
        namespace=rig.ns,
    )


def test_create_evidence_projection_stamps_epoch_and_refuses_marked_turn(
    rig: _Rig,
) -> None:
    ok_turn = _seed_turn(rig)
    proj = rig.uid("proj")
    rig.repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $k}) "
        "ON CREATE SET f.generation = 0, f.created_at = datetime() SET f.erasure_epoch = 7",
        params={"k": rig.ns},
    )
    assert _project(rig, ok_turn, proj) == proj
    rows = rig.repo.execute(
        "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN p.created_epoch AS e",
        params={"t": ok_turn},
    )
    assert rows[0]["e"] == 7

    marked_turn = _seed_turn(rig)
    rig.mark(marked_turn)
    assert _project(rig, marked_turn, rig.uid("proj")) is None
    assert (
        rig.repo.execute(
            "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN p",
            params={"t": marked_turn},
        )
        == []
    )

    marked_proj_turn = _seed_turn(rig)
    marked_proj = rig.uid("proj")
    rig.mark(marked_proj)
    assert _project(rig, marked_proj_turn, marked_proj) is None


def test_create_evidence_projection_refuses_while_erasing(rig: _Rig) -> None:
    turn = _seed_turn(rig)
    _start_erase(rig, rig.ns)
    assert _project(rig, turn, rig.uid("proj")) is None
    assert (
        rig.repo.execute(
            "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN p",
            params={"t": turn},
        )
        == []
    )


def _assertion(rig: _Rig, anchor: str, subject: str) -> TypedAssertion:
    return TypedAssertion(
        subject_uuid=subject,
        subject_display="my coins",
        attribute="owned",
        scope="",
        value_kind="count",
        unit="",
        operation="absolute",
        value=37,
        stated_span="I own 37 coins",
        span_start=0,
        span_end=14,
        episode_uuid=anchor,
        valid_at="2026-07-20T00:00:00+00:00",
        learned_at="2026-07-20T00:00:00+00:00",
        evidence_tier="user",
        perceiver_version="v1",
        namespace=rig.ns,
    )


def test_record_typed_assertion_writes_nothing_when_episode_marked(rig: _Rig) -> None:
    subject = rig.uid("subject")
    unmarked = rig.uid("anchor")
    repo = TypedAssertionRepository(rig.repo)
    first = repo.record_assertion(_assertion(rig, unmarked, subject))
    assert first["assertion_id"]
    marked = rig.uid("anchor")
    rig.mark(marked)
    res = repo.record_assertion(_assertion(rig, marked, subject))
    assert res["assertion_id"] == ""
    assert res["created"] is False
    count = rig.repo.execute(
        "MATCH (a:TypedAssertion {episode_uuid: $e}) RETURN count(a) AS c",
        params={"e": marked},
    )[0]["c"]
    assert count == 0
    heads = rig.repo.execute(
        "MATCH (h:TypedAssertionHead {namespace: $ns}) RETURN count(h) AS c",
        params={"ns": rig.ns},
    )[0]["c"]
    assert heads == 1
