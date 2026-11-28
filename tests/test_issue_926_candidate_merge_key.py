"""#926: add_candidate without a namespace must not MERGE over another silo's candidate."""

from __future__ import annotations

from menhir.infrastructure.candidate_repository import CandidateRepository


class _Neo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        return [{"uuid": "u", "scope": "CANDIDATE", "created_at": (params or {}).get("now")}]


def _create(namespace):
    neo4j = _Neo4j()
    CandidateRepository(neo4j).create_candidate(
        content="c", source="painscan", cluster_id="P1", label="l", namespace=namespace
    )
    return neo4j.calls[0]


def test_unscoped_candidate_merge_key_includes_default_namespace() -> None:
    for ns in (None, "", "   ", "default"):
        query, params = _create(ns)
        assert "namespace: $stamped_namespace}" in query.split("ON CREATE")[0]
        assert params["stamped_namespace"] == "default"


def test_scoped_candidate_merge_key_uses_its_namespace() -> None:
    query, params = _create("yawn")
    assert "namespace: $stamped_namespace}" in query.split("ON CREATE")[0]
    assert params["stamped_namespace"] == "yawn"
