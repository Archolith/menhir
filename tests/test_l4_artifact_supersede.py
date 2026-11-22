"""#741: L4 artifact supersession refuses cycles and re-supersession, under a lock."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from menhir.infrastructure.artifact_repository import ArtifactRepository
from menhir.services.artifact_service import ArtifactService


class _CaptureNeo4j:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "params": params or {}})
        return [{"superseded": 0}]


@pytest.mark.unit
def test_supersede_locks_in_id_order_then_guards_both_statuses() -> None:
    neo4j = _CaptureNeo4j()
    assert ArtifactRepository(neo4j).supersede_artifact("zeta", "alpha") is False
    call = neo4j.calls[0]
    q = " ".join(call["query"].split())
    assert call["params"]["first_id"] == "alpha" and call["params"]["second_id"] == "zeta"
    # Both lock writes come before the status guard.
    guard = q.index("WHERE old.artifact_id <> new.artifact_id")
    assert q.index("SET first.artifact_lock_revision") < guard
    assert q.index("SET second.artifact_lock_revision") < guard
    assert "coalesce(old.artifact_status, '') <> 'historical' AND old.superseded_by IS NULL" in q
    assert "coalesce(new.artifact_status, '') <> 'historical' AND new.superseded_by IS NULL" in q


class _Adapter:
    def __init__(self, store: dict[str, dict[str, Any]], applied: bool) -> None:
        self.store = store
        self.applied = applied

    def fetch_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self.store.get(artifact_id)

    def supersede_l4_artifact(self, old_id: str, new_id: str) -> bool:
        return self.applied


def _art(status: str, superseded_by: str | None = None) -> dict[str, Any]:
    return {"status": status, "superseded_by": superseded_by}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("store", "expected"),
    [
        ({"a": _art("historical", "b"), "b": _art("trusted")}, ("refused", "already_historical")),
        ({"a": _art("trusted"), "b": _art("historical", "c")}, ("refused", "replacement_historical")),
        ({"a": _art("trusted")}, ("not_found", None)),
    ],
)
def test_service_reports_refusal_apart_from_not_found(store, expected) -> None:
    service = ArtifactService.__new__(ArtifactService)
    service.graph_adapter = _Adapter(store, applied=False)
    result = asyncio.run(service.supersede("a", "b"))
    assert (result["status"], result.get("reason")) == expected


# --- online: real locks and statuses ---------------------------------------------------------


def _create(repo: ArtifactRepository, artifact_id: str) -> None:
    repo.create_artifact(
        artifact_id=artifact_id, artifact_type="decision", summary=f"summary {artifact_id}",
        source="human", status="trusted",
    )


def _row(graph: Any, artifact_id: str) -> dict[str, Any]:
    return graph.execute(
        """MATCH (a:Entity {artifact_id: $id})
        OPTIONAL MATCH (r:Entity)-[:SUPERSEDES]->(a)
        RETURN a.artifact_status AS status, a.superseded_by AS superseded_by,
               collect(r.artifact_id) AS replacements""",
        {"id": artifact_id},
    )[0]


@pytest.mark.online
@pytest.mark.timeout(60)
def test_reverse_supersession_and_resupersession_are_refused(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    repo = ArtifactRepository(graph)
    a, b, c = (f"l4-{name}-{uuid4().hex[:8]}" for name in "abc")
    for artifact_id in (a, b, c):
        _create(repo, artifact_id)

    assert repo.supersede_artifact(a, b) is True
    assert repo.supersede_artifact(b, a) is False  # would close a SUPERSEDES cycle
    assert repo.supersede_artifact(a, c) is False  # would overwrite a's recorded replacement

    assert _row(graph, a) == {"status": "historical", "superseded_by": b, "replacements": [b]}
    assert _row(graph, b)["status"] != "historical" and _row(graph, b)["replacements"] == []


@pytest.mark.online
@pytest.mark.timeout(60)
def test_opposite_concurrent_supersessions_have_one_winner(test_neo4j_repo: Any) -> None:
    graph = test_neo4j_repo
    repo = ArtifactRepository(graph)
    a, b = (f"l4-{name}-{uuid4().hex[:8]}" for name in "ab")
    _create(repo, a)
    _create(repo, b)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(repo.supersede_artifact, a, b), pool.submit(repo.supersede_artifact, b, a))
        results = [f.result(timeout=20) for f in futures]
    assert sorted(results) == [False, True]
    states = sorted(_row(graph, x)["status"] for x in (a, b))
    assert states.count("historical") == 1
