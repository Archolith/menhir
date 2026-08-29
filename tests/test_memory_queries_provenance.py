"""Unit tests for memory_queries.fetch_candidate_provenance query changes."""

from __future__ import annotations

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository


class FakeNeo4j:
    """Minimal fake neo4j driver for testing query structure."""

    def __init__(self):
        self.last_query = None
        self.last_params = None

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        """Record the query and return canned provenance result."""
        self.last_query = query
        self.last_params = params or {}
        # Return a canned provenance row with both anchor_projects and anchor_paths
        return [
            {
                "uuid": "u1",
                "evidence_node_kinds": ["direct_mention"],
                "anchor_projects": ["my_project"],
                "anchor_paths": ["src/example.py"],
                "episode_sources": ["manual_input"],
            }
        ]


@pytest.mark.unit
def test_fetch_candidate_provenance_includes_anchor_paths():
    """Verify that fetch_candidate_provenance query includes anchor_paths comprehension."""
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    result = repo.fetch_candidate_provenance(["u1"])

    # Verify the query was executed
    assert fake_neo4j.last_query is not None
    query = fake_neo4j.last_query

    # Verify structure_path is mentioned (the new comprehension)
    assert "structure_path" in query, "Query should include structure_path comprehension"

    # Verify anchor_paths field is in the return
    assert "anchor_paths" in query, "Query should return anchor_paths field"

    # Verify the old anchor_projects field is still there
    assert "anchor_projects" in query, "Query should still include anchor_projects field"

    # Verify the result includes the anchor_paths key
    assert len(result) == 1
    assert result[0]["anchor_paths"] == ["src/example.py"], "Result should include anchor_paths"
    assert result[0]["anchor_projects"] == ["my_project"], "Result should still include anchor_projects"


@pytest.mark.unit
def test_fetch_candidate_provenance_handles_empty_list():
    """Verify that empty node_uuids list returns empty result without querying."""
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    result = repo.fetch_candidate_provenance([])

    # Should return empty and not execute query
    assert result == []
    assert fake_neo4j.last_query is None, "Should not execute query for empty list"


@pytest.mark.unit
def test_namespace_delete_removes_namespace_keyed_episode_lifecycle_rows():
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    repo.delete_namespace_with_scalar_cascade(
        "eval-ns",
        "eval-ns",
        operation_id="delete-op",
    )

    assert "(n:Episodic AND n.namespace = $namespace)" in fake_neo4j.last_query
    assert fake_neo4j.last_params == {
        "group_id": "eval-ns",
        "namespace": "eval-ns",
        "namespace_key": "eval-ns",
        "operation_id": "delete-op",
    }


@pytest.mark.unit
def test_namespace_delete_removes_durable_event_log_and_watermark():
    # The namespace deletion must cover the durable event log: every :TypedEventAssertion in the
    # namespace, plus event heads tied ONLY to that namespace. :EventConsolidationWatermark is
    # namespace-keyed in group_id, so it is caught by the group partition; the explicit clause makes
    # the contract visible.
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    repo.delete_namespace_with_scalar_cascade(
        "eval-ns",
        "eval-ns",
        operation_id="delete-op",
    )

    query = fake_neo4j.last_query
    assert "(n:TypedEventAssertion AND n.namespace = $namespace)" in query
    assert "(n:EventConsolidationWatermark AND n.group_id = $namespace)" in query
    assert "(n:TypedEventAssertionHead" in query
    assert "EXISTS { MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion)" in query
    assert "ev.namespace = $namespace" in query


@pytest.mark.unit
def test_namespace_delete_preserves_shared_event_head():
    # A head that HAS_VERSION to an assertion in the target namespace AND to any assertion outside it
    # must NOT be deleted (shared-head preservation guard). Only a head with NO outside-namespace
    # assertion is a doomed head. The guard must also require at least one in-namespace assertion.
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    repo.delete_namespace_with_scalar_cascade(
        "eval-ns",
        "eval-ns",
        operation_id="delete-op",
    )

    query = fake_neo4j.last_query
    assert "AND EXISTS { MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion)" in query
    assert "ev.namespace = $namespace" in query
    assert "AND NOT EXISTS { MATCH (n)-[:HAS_VERSION]->(ev2:TypedEventAssertion)" in query
    assert "ev2.namespace <> $namespace" in query
    # the scalar repair path is unchanged: receipts still keyed on NAMESPACE_DELETE + namespace.
    assert "NAMESPACE_DELETE" in query
    assert "ScalarProjectionRepair" in query
    assert "RETURN size(doomed) AS deleted, repairs" in query


@pytest.mark.unit
def test_fetch_candidate_provenance_passes_uuids_param():
    """Verify that the query receives uuids in params."""
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    repo.fetch_candidate_provenance(["u1", "u2"])

    assert fake_neo4j.last_params is not None
    assert "uuids" in fake_neo4j.last_params
    assert fake_neo4j.last_params["uuids"] == ["u1", "u2"]


@pytest.mark.unit
def test_fetch_candidate_provenance_uses_optional_schema_tokens_dynamically():
    """An absent frontier schema must not flood Neo4j's notification log."""
    fake_neo4j = FakeNeo4j()
    repo = MemoryQueryRepository(fake_neo4j)

    repo.fetch_candidate_provenance(["u1"])

    assert "[:SUPPORTED_BY]" not in fake_neo4j.last_query
    assert "type(supported_by) = $supported_by_type" in fake_neo4j.last_query
    assert "ev[$evidence_kind_key]" in fake_neo4j.last_query
    assert fake_neo4j.last_params["supported_by_type"] == "SUPPORTED_BY"
    assert fake_neo4j.last_params["evidence_kind_key"] == "kind"
