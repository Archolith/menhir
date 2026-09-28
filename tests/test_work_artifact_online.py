"""WorkArtifact lifecycle races against a disposable Neo4j test instance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from typing import Any

import pytest

from menhir.domain.work_artifact import ArtifactStatus, ArtifactType
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

pytestmark = [pytest.mark.online, pytest.mark.timeout(60)]


def _artifact(repo: WorkArtifactRepository, *, title: str) -> str:
    return str(repo.create_artifact(artifact_type=ArtifactType.PLAN, title=title)["artifact_uuid"])


def _state(graph: Any, artifact_uuid: str) -> dict[str, Any]:
    rows = graph.execute(
        """MATCH (a:WorkArtifact {artifact_uuid: $uuid})
        OPTIONAL MATCH (replacement:WorkArtifact)-[:SUPERSEDES]->(a)
        RETURN a.status AS status, collect(replacement.artifact_uuid) AS replacements""",
        {"uuid": artifact_uuid},
    )
    return rows[0]


def test_two_transitions_from_one_observed_state_have_one_winner(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    artifact_uuid = _artifact(WorkArtifactRepository(graph), title="old")
    both_read = Barrier(2, timeout=15)

    class SynchronizedGraph:
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            rows = graph.execute(query, params)
            if "RETURN a.artifact_type AS artifact_type, a.status AS status" in query:
                both_read.wait()
            return rows

    repo = WorkArtifactRepository(SynchronizedGraph())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(repo.transition_status, artifact_uuid, status)
            for status in (ArtifactStatus.REVIEWED, ArtifactStatus.DEFERRED)
        ]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(result["applied"] for result in results) == [False, True]
    assert next(result for result in results if not result["applied"])["reason"] == "stale_transition"
    assert _state(graph, artifact_uuid)["status"] in {ArtifactStatus.REVIEWED, ArtifactStatus.DEFERRED}


def test_supersession_wins_against_stale_transition(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    plain_repo = WorkArtifactRepository(graph)
    old_uuid = _artifact(plain_repo, title="old")
    new_uuid = _artifact(plain_repo, title="new")
    read_done = Event()
    release = Event()

    class PausedGraph:
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            rows = graph.execute(query, params)
            if "RETURN a.artifact_type AS artifact_type, a.status AS status" in query:
                read_done.set()
                assert release.wait(timeout=15)
            return rows

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            WorkArtifactRepository(PausedGraph()).transition_status,
            old_uuid, ArtifactStatus.REVIEWED,
        )
        assert read_done.wait(timeout=15)
        try:
            supersession = plain_repo.supersede_artifact(new_uuid, old_uuid)
        finally:
            release.set()
        transition = future.result(timeout=20)

    assert supersession["applied"] is True
    assert transition["reason"] == "stale_transition"
    assert _state(graph, old_uuid) == {
        "status": ArtifactStatus.SUPERSEDED, "replacements": [new_uuid],
    }


def test_two_replacements_have_one_winner(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    plain_repo = WorkArtifactRepository(graph)
    old_uuid = _artifact(plain_repo, title="old")
    replacements = [_artifact(plain_repo, title=title) for title in ("new-a", "new-b")]
    both_started = Barrier(2, timeout=15)

    class SynchronizedGraph:
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "SET first.lifecycle_revision" in query:
                both_started.wait()
            return graph.execute(query, params)

    repo = WorkArtifactRepository(SynchronizedGraph())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(repo.supersede_artifact, replacement, old_uuid) for replacement in replacements]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(result["applied"] for result in results) == [False, True]
    state = _state(graph, old_uuid)
    assert state["status"] == ArtifactStatus.SUPERSEDED
    assert len(state["replacements"]) == 1
    assert state["replacements"][0] in replacements


@pytest.mark.parametrize("legacy_status", [None, "OLD_UNKNOWN"])
def test_legacy_unknown_status_cannot_be_superseded(
    test_neo4j_repo: Any, legacy_status: str | None
) -> None:
    graph = test_neo4j_repo
    repo = WorkArtifactRepository(graph)
    old_uuid = _artifact(repo, title="old")
    new_uuid = _artifact(repo, title="new")
    if legacy_status is None:
        graph.execute(
            "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) REMOVE a.status",
            {"uuid": old_uuid},
        )
    else:
        graph.execute(
            "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) SET a.status = $status",
            {"uuid": old_uuid, "status": legacy_status},
        )
    assert repo.supersede_artifact(new_uuid, old_uuid)["applied"] is False
    assert _state(graph, old_uuid) == {"status": legacy_status, "replacements": []}


def test_wrong_type_and_namespace_refuse_lifecycle_writes(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    repo = WorkArtifactRepository(graph)
    old_uuid = _artifact(repo, title="old")
    new_uuid = _artifact(repo, title="new")
    assert repo.transition_status(old_uuid, ArtifactStatus.REVIEWED, namespace="foreign") == {
        "applied": False, "reason": "artifact_not_found",
    }
    graph.execute(
        "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) SET a.artifact_type = 'OLD_UNKNOWN'",
        {"uuid": old_uuid},
    )
    assert repo.supersede_artifact(new_uuid, old_uuid)["applied"] is False
    assert _state(graph, old_uuid) == {"status": ArtifactStatus.PROPOSED, "replacements": []}


def test_legacy_null_namespace_can_transition_when_unscoped(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    repo = WorkArtifactRepository(graph)
    artifact_uuid = _artifact(repo, title="legacy namespace")
    graph.execute(
        "MATCH (a:WorkArtifact {artifact_uuid: $uuid}) REMOVE a.namespace",
        {"uuid": artifact_uuid},
    )

    assert repo.transition_status(artifact_uuid, ArtifactStatus.REVIEWED)["applied"] is True
    assert _state(graph, artifact_uuid)["status"] == ArtifactStatus.REVIEWED


def test_supersession_rolls_back_edge_if_status_write_fails(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    plain_repo = WorkArtifactRepository(graph)
    old_uuid = _artifact(plain_repo, title="old")
    new_uuid = _artifact(plain_repo, title="new")

    class FaultGraph:
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "MERGE (new)-[:SUPERSEDES]->(old)" in query:
                query = query.replace("SET old.status = $superseded", "SET old.status = $invalid_map")
                params = {**(params or {}), "invalid_map": {"not": "a Neo4j property"}}
            return graph.execute(query, params)

    with pytest.raises(Exception, match="(?i)(property|map|type)"):
        WorkArtifactRepository(FaultGraph()).supersede_artifact(new_uuid, old_uuid)
    assert _state(graph, old_uuid) == {"status": ArtifactStatus.PROPOSED, "replacements": []}
