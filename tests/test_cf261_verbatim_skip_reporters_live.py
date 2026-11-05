"""CF-261 live: reporters project a verbatim-skipped client onto its evidence projection.

Online because each reporter does it in Cypher (OPTIONAL MATCH plus a CASE or map projection).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.verbatim_skip_status import effective_processing_row
from menhir.explorer.app import _queued_episodes, _recovered_episodes, _successful_episodes
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository

pytestmark = pytest.mark.online


@pytest.fixture
def graph(test_neo4j_repo):
    tag = f"cf261-{uuidlib.uuid4().hex[:10]}"
    ns = f"{tag}-ns"
    u = {k: f"{tag}-{k}" for k in (
        "c_pending", "p_pending", "c_ready", "p_ready", "c_lost", "c_failed", "p_failed",
        "c_retried", "p_enr", "c_enr",
    )}
    test_neo4j_repo.execute(
        """
        UNWIND $rows AS row
        CREATE (n:Episodic {uuid: row.uuid, name: row.uuid, namespace: $ns, content: 'x',
                            processing_state: row.state, processing_attempts: row.attempts,
                            created_at: datetime(), test_tag: $tag})
        FOREACH (_ IN CASE WHEN row.skip IS NULL THEN [] ELSE [1] END |
            SET n.processing_substage = 'verbatim_skipped', n.extraction_skipped_for = row.skip)
        FOREACH (_ IN CASE WHEN row.key IS NULL THEN [] ELSE [1] END | SET n.ingest_key = row.key)
        """,
        params={
            "ns": ns,
            "tag": tag,
            "rows": [
                {"uuid": u["c_pending"], "state": "READY", "attempts": 1, "skip": u["p_pending"],
                 "key": f"{tag}-key"},
                {"uuid": u["p_pending"], "state": "PENDING", "attempts": 0, "skip": None, "key": None},
                {"uuid": u["c_ready"], "state": "READY", "attempts": 1, "skip": u["p_ready"], "key": None},
                {"uuid": u["p_ready"], "state": "READY", "attempts": 1, "skip": None, "key": None},
                {"uuid": u["c_lost"], "state": "READY", "attempts": 1, "skip": f"{tag}-gone", "key": None},
                {"uuid": u["c_failed"], "state": "READY", "attempts": 1, "skip": u["p_failed"], "key": None},
                {"uuid": u["p_failed"], "state": "FAILED", "attempts": 1, "skip": None, "key": None},
                {"uuid": u["c_retried"], "state": "READY", "attempts": 2, "skip": u["p_pending"], "key": None},
                {"uuid": u["p_enr"], "state": "ENRICHING", "attempts": 1, "skip": None, "key": None},
                {"uuid": u["c_enr"], "state": "READY", "attempts": 1, "skip": u["p_enr"], "key": None},
            ],
        },
    )
    try:
        yield test_neo4j_repo, ns, tag, u
    finally:
        test_neo4j_repo.execute("MATCH (n {test_tag: $t}) DETACH DELETE n", params={"t": tag})


def test_memory_overview_counts_the_projected_state(graph) -> None:
    neo4j, ns, _tag, _u = graph

    overview = MemoryQueryRepository(neo4j).fetch_memory_overview(namespace=ns)

    # Pending: p_pending, c_pending, c_retried, c_lost, c_failed. Ready: c_ready, p_ready.
    # Enriching: p_enr, c_enr.
    assert overview["pending_count"] == 5
    assert overview["ready_count"] == 2
    assert overview["enriching_count"] == 2
    assert overview["failed_count"] == 1
    assert overview["episode_count"] == 10


def test_ingest_key_replay_row_projects_the_client(graph) -> None:
    neo4j, _ns, tag, _u = graph
    repo = EpisodeLifecycleRepository()
    repo.neo4j = neo4j

    row = repo.find_episode_by_ingest_key(f"{tag}-key")

    assert row is not None and row["processing_state"] == "READY"
    assert effective_processing_row(row)["processing_state"] == "PENDING"


def test_queue_listing_carries_what_the_projection_needs(graph) -> None:
    neo4j, ns, _tag, u = graph
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = neo4j

    rows = repo.list_episode_processing(processing_states=["READY"], limit=25, namespace=ns)

    by_uuid = {r["uuid"]: effective_processing_row(r) for r in rows}
    assert by_uuid[u["c_pending"]]["processing_state"] == "PENDING"
    assert by_uuid[u["c_ready"]]["processing_state"] == "READY"
    assert by_uuid[u["c_lost"]]["processing_state"] == "PENDING"
    assert by_uuid[u["c_failed"]]["processing_state"] == "PENDING"


def test_explorer_lists_follow_the_projection(graph) -> None:
    neo4j, _ns, _tag, u = graph

    queued = {r["uuid"]: r for r in _queued_episodes(neo4j, limit=1000)}
    successful = {r["uuid"] for r in _successful_episodes(neo4j, limit=1000)}
    recovered = {r["uuid"] for r in _recovered_episodes(neo4j, limit=1000)}

    for key in ("c_pending", "c_lost", "c_failed", "p_pending", "c_retried"):
        assert queued[u[key]]["processing_state"] == "PENDING"
    assert u["c_ready"] not in queued
    assert {u["c_ready"], u["p_ready"]} <= successful
    assert not {u["c_pending"], u["c_lost"], u["c_failed"]} & successful
    assert u["c_retried"] not in recovered


def test_explorer_enriching_projection_is_listed_with_its_own_columns(graph) -> None:
    neo4j, _ns, _tag, u = graph
    neo4j.execute(
        "MATCH (p:Episodic {uuid: $p}) SET p.processing_stage = 'extracting', "
        "p.processing_progress = 40.0, p.processing_steps_completed = 2, p.processing_steps_total = 5",
        params={"p": u["p_enr"]},
    )
    neo4j.execute(
        "MATCH (c:Episodic {uuid: $c}) SET c.processing_stage = 'ready', "
        "c.processing_progress = 100.0, c.processing_steps_completed = 5, c.processing_steps_total = 5",
        params={"c": u["c_enr"]},
    )

    queued = {r["uuid"]: r for r in _queued_episodes(neo4j, limit=1000)}

    row = queued[u["c_enr"]]
    assert row["processing_state"] == "ENRICHING"
    assert row["processing_stage"] == "extracting"
    assert row["processing_progress"] == 40.0
    assert row["processing_steps_completed"] == 2
    assert "skip_projection" not in row


def test_memory_node_resource_reports_the_projected_state(graph) -> None:
    import asyncio

    from menhir.mcp.formatters import _project_skipped_memory_rows

    neo4j, ns, _tag, u = graph
    memory = MemoryQueryRepository(neo4j)
    lifecycle = EpisodeLifecycleRepository()
    lifecycle.neo4j = neo4j

    class _Backend:
        async def fetch_episode_processing(self, episode_uuid):
            return lifecycle.fetch_episode_processing(episode_uuid)

    row = memory.fetch_memory_by_uuid(u["c_enr"], namespace=ns)
    assert row["processing_state"] == "READY"  # the raw read is the client's own

    [projected] = asyncio.run(_project_skipped_memory_rows(_Backend(), [row]))

    assert projected["processing_state"] == "ENRICHING"
    assert projected["processing_completed_at"] is None


def test_scoped_overview_counts_real_anchor_shape(graph) -> None:
    neo4j, ns, _tag, _u = graph
    other = MemoryQueryRepository(neo4j).fetch_memory_overview(namespace=f"{ns}-other")
    assert other["episode_count"] == 0
