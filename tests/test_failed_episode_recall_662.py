"""#662: a memory whose enrichment fails stays recallable through the source-memory lane;
an erase tombstone (also FAILED) never is, and legacy raw-capture copies are not recalled."""

from __future__ import annotations

import uuid as uuidlib
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_pipeline import _memory_admitted


def _service() -> SimpleNamespace:
    return SimpleNamespace(
        _session_scope_admitted=lambda *a, **k: True,
        scalar_history_enabled=True,
        counting_enabled=True,
    )


def _meta(**extra: Any) -> dict[str, Any]:
    return {"uuid": "e1", "scope": "PERSISTENT", "freshness": "ACTIVE", "namespace": "default",
            **extra}


@pytest.mark.unit
def test_legacy_raw_capture_is_not_admitted_to_recall() -> None:
    kw = dict(include_session=True, session_id=None, namespace=None)
    assert _memory_admitted(_service(), _meta(raw_capture=False), **kw) is True
    assert _memory_admitted(_service(), _meta(), **kw) is True
    assert _memory_admitted(_service(), _meta(raw_capture=True), **kw) is False


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------

VEC = [1.0, 0.0, 0.0]


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuidlib.uuid4().hex[:10]}"


def _pending(graph: Any, ns: str, text: str) -> str:
    uid = _uid("f662")
    EpisodeRepository(graph).create_pending_episode(
        episode_uuid=uid, name=uid, content=text, session_id="s", user_id="u",
        source="remote-api", source_confidence=0.5, namespace=ns)
    return uid


def _failed(graph: Any, ns: str, text: str, *, embed: bool) -> str:
    repo = EpisodeRepository(graph)
    uid = _pending(graph, ns, text)
    claimed = repo.claim_pending_episode(uid, max_attempts=3, worker_id="w", lease_seconds=60)
    assert claimed is not None
    if embed:
        assert repo.set_episode_content_embedding(uid, VEC, "m-1")
    assert repo.mark_episode_failed(
        uid, "ValueError: unexpected model output", worker_id="w",
        claim_started_at=claimed["processing_started_at"])
    return uid


def _tombstone(graph: Any, uid: str) -> None:
    graph.execute(
        "MATCH (n:Episodic {uuid: $u}) SET n.processing_state = 'FAILED', "
        "n.processing_substage = 'deleted_by_user', n.erased_at = datetime()",
        params={"u": uid},
    )


@pytest.mark.online
def test_live_failed_episode_is_source_recallable_and_a_tombstone_is_not(test_neo4j_repo) -> None:
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    failed = _failed(graph, ns, "The deploy key rotates every 90 days.", embed=True)
    erased = _failed(graph, ns, "An erased memory.", embed=True)
    _tombstone(graph, erased)
    state = graph.execute("MATCH (n:Episodic {uuid: $u}) RETURN n.processing_state AS s",
                          params={"u": failed})[0]["s"]
    assert state == "FAILED"

    hits = MemoryGraphAdapter(graph).search_episode_embeddings(VEC, limit=5, namespace=ns, model="m-1")
    assert [h["uuid"] for h in hits] == [failed]


@pytest.mark.online
def test_live_repair_listing_covers_failed_but_not_tombstones_or_in_flight(test_neo4j_repo) -> None:
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    failed = _failed(graph, ns, "Failed and never embedded.", embed=False)
    erased = _failed(graph, ns, "Erased, never embedded.", embed=False)
    _tombstone(graph, erased)
    pending = _pending(graph, ns, "Still pending.")

    rows = MemoryGraphAdapter(graph).list_episodes_missing_content_embedding(
        ns, 50, settled_only=True)
    listed = {r["uuid"] for r in rows}
    assert failed in listed
    assert erased not in listed
    assert pending not in listed


def _mark(graph: Any, uid: str, ns: str) -> None:
    graph.execute(
        "CREATE (:ErasureMarker {namespace_key: $ns, subject_uuid: $u, op_id: $op})",
        params={"ns": ns, "u": uid, "op": _uid("op")},
    )


@pytest.mark.online
def test_live_sealed_erase_hides_a_failed_episode_before_the_delete(test_neo4j_repo) -> None:
    # Review F3/F4: the erase seal writes its marker before the delete runs. In that window the
    # episode must not be recalled, listed for repair, or reported as source-searchable.
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    sealed = _failed(graph, ns, "Sealed for erase, delete not applied yet.", embed=True)
    unembedded = _failed(graph, ns, "Sealed, never embedded.", embed=False)
    _mark(graph, sealed, ns)
    _mark(graph, unembedded, ns)
    adapter = MemoryGraphAdapter(graph)
    try:
        assert adapter.search_episode_embeddings(VEC, limit=5, namespace=ns, model="m-1") == []
        listed = {r["uuid"] for r in adapter.list_episodes_missing_content_embedding(
            ns, 50, settled_only=True)}
        assert unembedded not in listed
        row = EpisodeRepository(graph).fetch_episode_processing(sealed)
        assert row is not None and row["source_search_eligible"] is False
    finally:
        graph.execute("MATCH (m:ErasureMarker) WHERE m.subject_uuid IN $u DETACH DELETE m",
                      params={"u": [sealed, unembedded]})


@pytest.mark.online
def test_live_failed_episode_is_source_search_eligible(test_neo4j_repo) -> None:
    # Review F2: readiness agrees with recall -- a FAILED episode is eligible, a tombstone is not.
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    failed = _failed(graph, ns, "Readiness for a failed memory.", embed=True)
    erased = _failed(graph, ns, "Readiness for an erased memory.", embed=True)
    _tombstone(graph, erased)
    repo = EpisodeRepository(graph)
    assert repo.fetch_episode_processing(failed)["source_search_eligible"] is True
    assert repo.fetch_episode_processing(erased)["source_search_eligible"] is False


@pytest.mark.online
def test_live_timeline_search_skips_failed_receipts(test_neo4j_repo) -> None:
    # Review F5: a FAILED receipt cannot anchor a timeline, so it must not use up a slot.
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    failed = _failed(graph, ns, "A failed memory with a resolved episode.", embed=True)
    g = _uid("g662")
    graph.execute(
        "CREATE (:Episodic {uuid: $g, group_id: $ns, namespace: $ns, valid_at: datetime(), "
        "content: 'resolved'}) WITH 1 AS x MATCH (n:Episodic {uuid: $u}) "
        "SET n.resolved_episode_uuid = $g",
        params={"g": g, "ns": ns, "u": failed})
    adapter = MemoryGraphAdapter(graph)
    assert [h["uuid"] for h in adapter.search_episode_embeddings(
        VEC, limit=5, namespace=ns, model="m-1")] == [failed]
    assert adapter.search_episode_embeddings(
        VEC, limit=5, namespace=ns, model="m-1", resolved_only=True) == []


@pytest.mark.online
def test_live_recent_memories_skip_legacy_raw_captures(test_neo4j_repo) -> None:
    # Review F7: startup context (recall_context_memories, /recent) must not show raw captures.
    graph = test_neo4j_repo
    ns = _uid("ns-662")
    keep, raw = _uid("e662"), _uid("rc662")
    graph.execute(
        "UNWIND $rows AS r CREATE (n:Entity {uuid: r.u, name: r.u, namespace: $ns, group_id: $ns, "
        "scope: 'PERSISTENT', freshness: 'ACTIVE', raw_capture: r.raw, created_at: datetime()})",
        params={"ns": ns, "rows": [{"u": keep, "raw": False}, {"u": raw, "raw": True}]})
    uuids = {r["uuid"] for r in MemoryGraphAdapter(graph).fetch_recent_memories(10, namespace=ns)}
    assert keep in uuids
    assert raw not in uuids
