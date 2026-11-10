"""Erase of an in-flight (PENDING/ENRICHING) queue node must leave a content-less tombstone.

The memory delete cascade's node-delete ``CALL { ... UNION ... }`` once flipped a PENDING/ENRICHING
Episodic to FAILED/deleted_by_user in one branch and a later branch re-read ``processing_state`` and
DETACH DELETEd it in the same statement, so the tombstone never survived and the enrichment worker's
``_compensate_if_erased`` never fired (e2e-21 erase_during_enrichment). The tombstone must also be
invisible to the claim, retry-candidate and force-reset queries (``EPISODE_NOT_ERASED``).

The graph is seeded directly with Cypher (no LLM). All tests need a live Neo4j.
"""

from __future__ import annotations

import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator

SECRET = "The tombstone probe's favourite colour is teal-9921"


# ------------------------------------------------------------------ helpers
def _ids(tag: str) -> dict[str, str]:
    return {
        "tag": tag,
        "ns": f"{tag}-ns",
        "queue": f"{tag}-queue",
        "ordinary": f"{tag}-ordinary-failed",
        "twin": f"{tag}-graphiti",
        "entity": f"{tag}-entity",
        "entity2": f"{tag}-entity2",
        "edge": f"{tag}-edge",
        "mention": f"{tag}-mention",
        "name": f"{tag}-anchor-name",
    }


def _seed_queue(repo: Any, ids: dict[str, str], state: str) -> None:
    """Queue node with SECRET content, an ingest key and (if ENRICHING) an owner/lease."""
    repo.execute(
        """
        CREATE (:Episodic {uuid:$queue, namespace:$ns, name:$name, content:$text,
                           processing_state:$state, processing_owner:'w1',
                           processing_attempts: 0,
                           processing_started_at: datetime(),
                           ingest_key:$ikey, test_tag:$tag})
        """,
        params={**ids, "text": SECRET, "state": state, "ikey": f"{ids['tag']}-ikey"},
    )


def _seed_existing_tombstone(repo: Any, ids: dict[str, str]) -> None:
    """A tombstone as an earlier erase would have left it. No attempts, no retry_after."""
    repo.execute(
        """
        CREATE (:Episodic {uuid:$queue, namespace:$ns, name:$name,
                           processing_state:'FAILED', processing_substage:'deleted_by_user',
                           processing_error:'deleted_by_user',
                           processing_attempts: 0,
                           erased_at: datetime(), test_tag:$tag})
        """,
        params=ids,
    )


def _seed_ordinary_failed(repo: Any, ids: dict[str, str]) -> None:
    repo.execute(
        """
        CREATE (:Episodic {uuid:$ordinary, namespace:$ns, name:$name, content:'ordinary',
                           processing_state:'FAILED', processing_attempts: 0,
                           processing_error:'boom', processing_started_at: datetime(),
                           test_tag:$tag})
        """,
        params=ids,
    )


def _seed_twin_with_derivatives(repo: Any, ids: dict[str, str]) -> None:
    """Twin T + single-source entity E (mentioned only by T) + fact E->E2 supported only by T."""
    repo.execute(
        """
        CREATE (t:Episodic {uuid:$twin, group_id:$ns, name:$name, valid_at:datetime(),
                            created_at: datetime(), content:$text, test_tag:$tag})
        CREATE (e1:Entity {uuid:$entity, name:'tombstone probe', group_id:$ns, summary:$text,
                           created_at:datetime(), test_tag:$tag})
        CREATE (e2:Entity {uuid:$entity2, name:'teal-9921', group_id:$ns, summary:'a colour',
                           created_at:datetime(), test_tag:$tag})
        CREATE (t)-[:MENTIONS {uuid:$mention, group_id:$ns, created_at:datetime()}]->(e1)
        CREATE (e1)-[:RELATES_TO {uuid:$edge, name:'HAS_FAVOURITE_COLOUR', fact:$text,
                                  episodes:[$twin], group_id:$ns, created_at:datetime(),
                                  test_tag:$tag}]->(e2)
        """,
        params={**ids, "text": SECRET},
    )


def _coordinator(repo: Any, tmp_path: Path):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(neo4j=repo)
    db = tmp_path / "inflight-tombstone.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    journal = GraphOperationsJournal(db_path=db)
    coord = ErasureCoordinator(
        graph_adapter=adapter, journal=journal, subjects=ErasureSubjectStore(db_path=db)
    )
    return adapter, coord


def _cleanup(repo: Any, tag: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
    repo.execute(
        "MATCH (r:DerivedRederiveRepair) WHERE r.repair_key CONTAINS $t DETACH DELETE r",
        params={"t": tag},
    )


def _exists(repo: Any, node_uuid: str) -> bool:
    rows = repo.execute("MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid})
    return int(rows[0]["c"]) > 0


def _edge_exists(repo: Any, edge_uuid: str) -> bool:
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN count(r) AS c", params={"u": edge_uuid}
    )
    return int(rows[0]["c"]) > 0


def _node(repo: Any, node_uuid: str) -> dict[str, Any] | None:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid:$u}) RETURN n.processing_state AS state, "
        "n.processing_substage AS substage, n.erased_at AS erased_at, "
        "n.processing_owner AS owner, n.content AS content, n.ingest_key AS ingest_key",
        params={"u": node_uuid},
    )
    return dict(rows[0]) if rows else None


def _assert_tombstone(repo: Any, node_uuid: str) -> None:
    node = _node(repo, node_uuid)
    assert node is not None, "in-flight queue node was deleted instead of tombstoned"
    assert node["state"] == "FAILED"
    assert node["substage"] == "deleted_by_user"
    assert node["erased_at"] is not None
    assert node["owner"] is None
    assert node["content"] is None, "erased text must not survive on the tombstone"
    assert node["ingest_key"] is None


def _erase_inflight(repo: Any, tmp_path: Path, ids: dict[str, str], state: str):
    _seed_queue(repo, ids, state)
    adapter, coord = _coordinator(repo, tmp_path)
    coord.erase_memory(ids["queue"])
    return adapter, coord


# ------------------------------------------------------------------ cascade
@pytest.mark.online
def test_erase_enriching_node_leaves_content_less_tombstone(test_neo4j_repo, tmp_path) -> None:
    """Breaks if a later UNION branch of the node-delete re-reads the post-flip processing_state
    (FAILED) and DETACH DELETEs the ENRICHING node, or if the flip stops scrubbing content,
    ingest_key, owner or stamping erased_at."""
    repo = test_neo4j_repo
    tag = f"test-tomb-enriching-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _erase_inflight(repo, tmp_path, ids, "ENRICHING")
        _assert_tombstone(repo, ids["queue"])
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_llm_progress_stamps_never_overwrite_the_erase_marker(test_neo4j_repo, tmp_path) -> None:
    """e2e-21 counterexample: Graphiti's LLM callbacks stamp the queue node mid-extraction. Breaks
    if increment_episode_llm_usage or the worker_id=None progress stamp may write a tombstone
    (they overwrote substage deleted_by_user), or if erase detection keys on the substage alone."""
    repo = test_neo4j_repo
    tag = f"test-tomb-llmstamp-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        adapter, _coord = _erase_inflight(repo, tmp_path, ids, "ENRICHING")
        assert adapter.increment_episode_llm_usage(ids["queue"], phase="completed") is False
        assert _node(repo, ids["queue"])["substage"] == "deleted_by_user"
        # A writer outside these two still clobbering the substage must not hide the erase.
        repo.execute(
            "MATCH (n:Episodic {uuid:$u}) SET n.processing_substage = 'llm_response_received'",
            params={"u": ids["queue"]},
        )
        assert adapter.episode_erased_by_user(ids["queue"]) is True
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_erase_pending_node_leaves_content_less_tombstone(test_neo4j_repo, tmp_path) -> None:
    """Same as the ENRICHING case for PENDING. Breaks if PENDING is dropped from the tombstoning
    branch's prior_state list, or the delete branch stops excluding PENDING."""
    repo = test_neo4j_repo
    tag = f"test-tomb-pending-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _erase_inflight(repo, tmp_path, ids, "PENDING")
        _assert_tombstone(repo, ids["queue"])
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_erase_ready_node_is_deleted_outright(test_neo4j_repo, tmp_path) -> None:
    """Control: a finished (READY) node has no worker to compensate, so it is deleted, not kept.
    Breaks if the tombstone branch widens to every state (erased READY memories would linger)."""
    repo = test_neo4j_repo
    tag = f"test-tomb-ready-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _erase_inflight(repo, tmp_path, ids, "READY")
        assert _node(repo, ids["queue"]) is None
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_erase_of_unlinked_tombstone_keeps_it(test_neo4j_repo, tmp_path) -> None:
    """A repeat erase of an UNLINKED tombstone must keep it (review B-2): its worker may still
    publish a twin, and only the tombstone lets it detect the erase and compensate. It is cleared
    later, when its linked twin is erased with it. Breaks if a re-erase or replay deletes it."""
    repo = test_neo4j_repo
    tag = f"test-tomb-reerase-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_existing_tombstone(repo, ids)
        _adapter, coord = _coordinator(repo, tmp_path)
        coord.erase_memory(ids["queue"])
        node = _node(repo, ids["queue"])
        assert node is not None
        assert node["state"] == "FAILED" and node["substage"] == "deleted_by_user"
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ tombstone is inert
@pytest.mark.online
def test_claim_refuses_tombstone(test_neo4j_repo, tmp_path) -> None:
    """Breaks if EPISODE_NOT_ERASED is dropped from claim_pending_episode: a FAILED tombstone with
    0 attempts and no retry_after would be re-leased to ENRICHING and re-enriched from nothing."""
    repo = test_neo4j_repo
    tag = f"test-tomb-claim-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_existing_tombstone(repo, ids)
        adapter, _coord = _coordinator(repo, tmp_path)

        claimed = adapter.claim_pending_episode(
            ids["queue"], max_attempts=3, worker_id="w-claim", lease_seconds=60
        )

        assert claimed is None
        node = _node(repo, ids["queue"])
        assert node["state"] == "FAILED" and node["substage"] == "deleted_by_user"
        assert node["owner"] is None
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_retry_candidates_exclude_tombstone_but_list_ordinary_failed(
    test_neo4j_repo, tmp_path
) -> None:
    """Breaks if EPISODE_NOT_ERASED is dropped from fetch_failed_episode_retry_candidates (the
    retry scheduler would requeue erased memories). The ordinary FAILED control proves the query
    still returns FAILED rows, so the exclusion is not vacuous."""
    repo = test_neo4j_repo
    tag = f"test-tomb-retry-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_existing_tombstone(repo, ids)
        _seed_ordinary_failed(repo, ids)
        adapter, _coord = _coordinator(repo, tmp_path)

        rows = adapter.fetch_failed_episode_retry_candidates(500)
        mine = {r["uuid"] for r in rows if r["uuid"] in {ids["queue"], ids["ordinary"]}}

        assert ids["ordinary"] in mine, "control: ordinary FAILED node must be a candidate"
        assert ids["queue"] not in mine
    finally:
        _cleanup(repo, tag)


@pytest.mark.online
def test_force_reset_refuses_tombstone_but_resets_ordinary_failed(test_neo4j_repo, tmp_path) -> None:
    """Breaks if EPISODE_NOT_ERASED is dropped from force_reset_failed_episode (an operator reset
    would flip the tombstone back to PENDING). The ordinary FAILED control returns True."""
    repo = test_neo4j_repo
    tag = f"test-tomb-reset-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        _seed_existing_tombstone(repo, ids)
        _seed_ordinary_failed(repo, ids)
        adapter, _coord = _coordinator(repo, tmp_path)

        assert adapter.force_reset_failed_episode(ids["queue"]) is False
        node = _node(repo, ids["queue"])
        assert node["state"] == "FAILED" and node["substage"] == "deleted_by_user"

        assert adapter.force_reset_failed_episode(ids["ordinary"]) is True
        assert _node(repo, ids["ordinary"])["state"] == "PENDING"
    finally:
        _cleanup(repo, tag)


# ------------------------------------------------------------------ end to end worker path
@pytest.mark.online
def test_worker_compensation_removes_twin_derivatives_and_the_tombstone(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """End to end: erase an ENRICHING node (tombstone survives), THEN the in-flight worker writes
    twin T with a single-source entity and fact, then runs `_compensate_if_erased`. Breaks if the
    tombstone is not kept by the erase (the hook finds nothing erased and T leaks), or if
    compensation stops cascading to T/derivatives, or leaves the tombstone behind."""
    repo = test_neo4j_repo
    tag = f"test-tomb-e2e-{uuidlib.uuid4()}"
    try:
        ids = _ids(tag)
        adapter, coord = _erase_inflight(repo, tmp_path, ids, "ENRICHING")
        _assert_tombstone(repo, ids["queue"])
        # The worker, already inside add_episode, writes the twin after the erase.
        _seed_twin_with_derivatives(repo, ids)
        assert _exists(repo, ids["twin"]) is True

        import menhir.services.erasure_compensation as comp

        monkeypatch.setattr(comp, "_default_coordinator", lambda _a: coord)
        from menhir.services.enrichment_steps import _compensate_if_erased

        _compensate_if_erased(SimpleNamespace(graph_adapter=adapter, episode_uuid=ids["queue"]), ids["twin"])

        assert _exists(repo, ids["twin"]) is False
        assert _exists(repo, ids["entity"]) is False
        assert _edge_exists(repo, ids["edge"]) is False
        assert _exists(repo, ids["queue"]) is False, "tombstone must be removed with its twin"
        # E2 was never mentioned by T, so it was never derived from T alone.
        assert _exists(repo, ids["entity2"]) is True
    finally:
        _cleanup(repo, tag)
