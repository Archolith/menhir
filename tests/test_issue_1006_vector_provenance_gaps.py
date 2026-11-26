"""#1006: assertion dims are reported not blocking; count cosine size guard; provenance datetimes."""

from __future__ import annotations

import pytest
from menhir.config import MemorySettings
from menhir.infrastructure.count_decision_repository import CountDecisionRepository
from menhir.infrastructure.embedding_dimensions import (
    embedding_dimension_health,
    evaluate_embedding_compatibility,
)
from menhir.infrastructure.memory_queries import MemoryQueryRepository


class _Neo4j:
    """Canned rows by substring; records every query."""

    def __init__(self, responses: dict[str, list[dict]]) -> None:
        self.queries: list[str] = []
        self._responses = responses

    def execute(self, query: str, params: dict | None = None, **kw) -> list[dict]:
        self.queries.append(query)
        for needle, rows in self._responses.items():
            if needle in query:
                return rows
        return []


def _healthy(assertions: list[dict]) -> dict[str, list[dict]]:
    return {
        "t.name_embedding IS NOT NULL": assertions,
        "n.name_embedding IS NOT NULL": [{"dim": 1536, "count": 5}],
        "r.fact_embedding IS NOT NULL": [{"dim": 1536, "count": 3}],
    }


def _settings() -> MemorySettings:
    return MemorySettings(
        graphiti_provider="openai",
        graphiti_embed_provider="openai",
        openai_embed_model="text-embedding-3-small",
    )


@pytest.mark.unit
def test_zero_assertions_reported_empty():
    health = embedding_dimension_health(_Neo4j(_healthy([])), expected_dim=1536)
    assert health["assertion_dims"] == {}
    assert health["wrong_assertion_count"] == 0
    assert health["ok"] is True


@pytest.mark.unit
def test_mixed_assertion_dims_reported_but_not_blocking():
    neo4j = _Neo4j(_healthy([{"dim": 1536, "count": 10}, {"dim": 768, "count": 4}]))
    health = embedding_dimension_health(neo4j, expected_dim=1536)
    assert health["assertion_dims"] == {1536: 10, 768: 4}
    assert health["wrong_assertion_count"] == 4
    assert health["ok"] is True
    assert health["mixed"] is False
    assert health["wrong_entity_count"] == 0
    assert health["zero_entity_count"] == 0

    compat = evaluate_embedding_compatibility(neo4j, _settings())
    assert compat.ok is True
    assert compat.blocking is False
    assert compat.mixed is False


@pytest.mark.unit
def test_zero_length_assertion_vectors_do_not_block():
    neo4j = _Neo4j(_healthy([{"dim": 0, "count": 2}]))
    health = embedding_dimension_health(neo4j)
    assert health["wrong_assertion_count"] == 2
    assert health["ok"] is True
    assert evaluate_embedding_compatibility(neo4j, _settings()).blocking is False


@pytest.mark.unit
def test_existing_blocking_case_unchanged_with_assertions_present():
    neo4j = _Neo4j({
        "t.name_embedding IS NOT NULL": [{"dim": 1536, "count": 3}],
        "n.name_embedding IS NOT NULL": [{"dim": 768, "count": 20000}],
    })
    assert evaluate_embedding_compatibility(neo4j, _settings()).blocking is True


@pytest.mark.unit
def test_cosine_related_counted_guards_vector_size():
    neo4j = _Neo4j({})
    CountDecisionRepository(neo4j).cosine_related_counted(
        "ns", [{"id": "p", "subject_uuid": "s", "value_kind": "count", "vector": [1.0, 0.0]}],
        embed_version="ev", min_cosine=0.5, per_probe=3,
    )
    query = neo4j.queries[0]
    guard = query.index("size(a.count_key_embedding) = size(p.vec)")
    assert guard < query.index("vector.similarity.cosine")


@pytest.mark.unit
def test_result_provenance_compares_datetimes_not_strings():
    neo4j = _Neo4j({})
    MemoryQueryRepository(neo4j).fetch_result_provenance(["u"], "ns")
    query = neo4j.queries[0]
    assert "datetime(toString(coalesce(rec.reference_time, epi.valid_at, epi.created_at)))" in query
    assert "min(first_seen) AS first_seen_dt" in query
    assert "toString(first_seen_dt) AS first_seen_at" in query
    assert "toString(coalesce(rec.reference_time, epi.valid_at, epi.created_at)) AS first_seen" not in query
