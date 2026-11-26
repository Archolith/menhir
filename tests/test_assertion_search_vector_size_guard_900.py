"""#900: the observation (TypedAssertion) vector search skips stale-dimension embeddings with a
size guard, like `search_episode_embeddings`, instead of letting one old-dimension row fail the lane."""

from __future__ import annotations

import inspect
import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.memory_queries import MemoryQueryRepository

_GUARD = "size(a.name_embedding) = size($query_vector)"


class _Recorder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict[str, Any]]:
        self.queries.append(query)
        return []


@pytest.mark.unit
def test_assertion_scan_query_has_size_guard() -> None:
    rec = _Recorder()
    MemoryQueryRepository(rec).search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=["ns"])
    assert len(rec.queries) == 1
    assert _GUARD in rec.queries[0]
    assert rec.queries[0].index("name_embedding IS NOT NULL") < rec.queries[0].index(_GUARD)
    assert "a.namespace IN $namespaces" in rec.queries[0]


@pytest.mark.unit
def test_assertion_guard_is_in_where_shared_with_index_path() -> None:
    src = inspect.getsource(MemoryQueryRepository.search_assertion_embeddings)
    assert src.count(_GUARD) == 1
    assert "_index_backed(" in src and "where, tail" in src


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"a900-{uuidlib.uuid4().hex[:10]}"
    yield test_neo4j_repo, MemoryQueryRepository(test_neo4j_repo), ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n", {"ns": ns})


@pytest.mark.online
def test_stale_dimension_assertion_does_not_drop_the_lane(env) -> None:
    repo, queries, ns = env
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (a:TypedAssertion {assertion_id: r.id, namespace: $ns, group_id: $ns,
                                  name_embedding: r.vec, stated_span: r.id, operation: 'occurrence',
                                  subject_uuid: 'self', attribute: 'kit_count', scope: '',
                                  value_kind: 'count', unit: '', value: 1, superseded: false,
                                  binding_pending: false})
        """,
        {"ns": ns, "rows": [
            {"id": f"{ns}:new", "vec": [1.0, 0.0, 0.0]},
            {"id": f"{ns}:old", "vec": [1.0, 0.0]},
        ]},
    )
    hits = queries.search_assertion_embeddings([1.0, 0.0, 0.0], limit=10, namespaces=[ns])
    assert [h["assertion_id"] for h in hits] == [f"{ns}:new"]
