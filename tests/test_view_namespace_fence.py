"""Atomic namespace-fence and exact-evidence checks for View writes."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.view_repository import ViewRepository


class _Neo4j:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_kwargs: Any):
        self.calls.append((query, params or {}))
        return self.responses.pop(0) if self.responses else []


@pytest.mark.unit
def test_new_fact_acquires_namespace_fence_before_current_and_evidence_reads() -> None:
    neo4j = _Neo4j([[], [{"uuid": "view-1"}]])
    repo = ViewRepository(neo4j)

    result = repo.record(
        "counter",
        subject="user",
        namespace="project",
        counter="widgets",
        value=2,
        episode_uuids=["turn-1"],
    )

    query, params = neo4j.calls[1]
    assert query.index("MERGE (f:EvidenceNamespaceFence") < query.index("OPTIONAL MATCH (actual:")
    assert query.index("SET f.lock_nonce") < query.index("OPTIONAL MATCH (e)")
    assert "size(row.candidates) = 1" in query
    assert "$tenant_namespaces IS NULL OR" in query
    assert "e.evidence_finalized = true" in query
    assert "e.publication_generation) = f.generation" in query
    assert "FOREACH (e IN evidence | MERGE (e)-[:MENTIONS]->(n))" in query
    assert params["eps"] == ["turn-1"]
    assert params["namespace_key"] == "project"
    assert params["tenant_namespaces"] == ["project"]
    assert params["extra"]["view_class"] == "FACT"
    assert params["extra"]["view_subtype"] == "counter"
    assert params["extra"]["view_audience"] == "RECALL"
    assert result["episodes_missing"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("namespace", "physical_group", "stamped_group", "view_key"),
    [
        (None, "", "default", "::user::widgets"),
        ("", "", "", "::user::widgets"),
        ("default", "default", "default", "default::user::widgets"),
    ],
)
def test_default_namespace_spellings_share_one_fence_without_rekeying_storage(
    namespace: str | None,
    physical_group: str,
    stamped_group: str,
    view_key: str,
) -> None:
    neo4j = _Neo4j([[], [{"uuid": "view-1"}]])
    repo = ViewRepository(neo4j)

    repo.record(
        "counter",
        subject="user",
        namespace=namespace,
        counter="widgets",
        value=2,
        episode_uuids=[],
    )

    _, params = neo4j.calls[1]
    assert params["namespace_key"] == "default"
    assert params["ns"] == physical_group
    assert params["ns_stamped"] == stamped_group
    assert params["tenant_namespaces"] == ["default", ""]
    assert params["key"] == view_key


@pytest.mark.unit
def test_fact_create_is_one_statement_without_post_commit_mentions_write() -> None:
    neo4j = _Neo4j([[], [{"uuid": "view-1"}]])
    repo = ViewRepository(neo4j)

    repo.record(
        "counter",
        subject="user",
        namespace="project",
        counter="widgets",
        value=2,
        episode_uuids=["turn-1"],
    )

    assert len(neo4j.calls) == 2  # lookup + one atomic fenced mutation
    assert sum("MERGE (e)-[:MENTIONS]->(n)" in query for query, _ in neo4j.calls) == 1


@pytest.mark.unit
def test_unchanged_fact_refresh_validates_old_edge_set_under_fence() -> None:
    neo4j = _Neo4j(
        [
            [{"uuid": "view-1", "sig": "2", "valid_at": "2026-08-28T00:00:00+00:00"}],
            [{"stored": ["turn-1", "turn-2"], "present": ["turn-1", "turn-2"]}],
        ]
    )
    repo = ViewRepository(neo4j)

    repo.record(
        "counter",
        subject="user",
        namespace="project",
        counter="widgets",
        value=2,
        episode_uuids=["turn-2"],
    )

    query, _ = neo4j.calls[1]
    assert query.index("SET f.lock_nonce") < query.index("MATCH (n:Entity")
    assert "OPTIONAL MATCH (old_evidence)-[:MENTIONS]->(n)" in query
    assert "size(old_mentions) = size(coalesce(n.episode_uuids, []))" in query
    assert "all(eid IN old_mentions WHERE eid IN coalesce(n.episode_uuids, []))" in query
    assert "size(row.candidates) = 1" in query
