from __future__ import annotations

import pytest

from menhir.infrastructure.projection_coverage_repository import ProjectionCoverageRepository


class _Neo4j:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, dict(params or {})))
        return list(self.rows)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method", "query_fragment"),
    [
        ("assertions_for_projection_audit", "MATCH (a:TypedAssertion"),
        ("list_scalar_state_views_for_audit", "MATCH (n:Entity"),
    ],
)
def test_default_namespace_uses_canonical_tenant_scope(method, query_fragment):
    neo4j = _Neo4j([{"namespace": ""}])
    repository = ProjectionCoverageRepository(neo4j)

    if method == "assertions_for_projection_audit":
        rows = repository.assertions_for_projection_audit("entity-1", namespace="default")
    else:
        rows = repository.list_scalar_state_views_for_audit(
            subject_uuid="entity-1",
            namespace="default",
        )

    query, params = neo4j.calls[0]
    assert query_fragment in query
    assert "tenant_namespaces" in query
    assert params["tenant_namespaces"] == ["default", ""]
    assert rows[0]["namespace"] == "default"
