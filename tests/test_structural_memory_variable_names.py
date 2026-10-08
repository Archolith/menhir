"""The shared structural predicate must not shadow variables bound by the queries that embed it.

Neo4j 5 rejects a list-comprehension variable that shadows an outer variable inside an EXISTS
subquery. The subject-thread timeline binds `s` and embedded `any(s IN ...)` from this predicate,
which failed every subject-scoped timeline query on a real database.
"""
from __future__ import annotations

import re

import pytest

from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.infrastructure.memory_queries import MemoryQueryRepository

pytestmark = pytest.mark.unit

_COMPREHENSION = re.compile(r"\b(?:any|all|none|single)\(\s*(\w+)\s+IN\b|\[\s*(\w+)\s+IN\b", re.IGNORECASE)
_PATTERN_VAR = re.compile(r"\(\s*(\w+)\s*:")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, query: str, params=None):
        self.calls.append(query)
        return []


def _comprehension_vars(query: str) -> set[str]:
    return {a or b for a, b in _COMPREHENSION.findall(query)}


def _pattern_vars(query: str) -> set[str]:
    return set(_PATTERN_VAR.findall(query))


def test_structural_predicate_uses_no_short_comprehension_names() -> None:
    names = _comprehension_vars(non_structural_memory_cypher("q"))
    assert names
    assert all(name.startswith("structural_") for name in names), names


@pytest.mark.parametrize("namespace", [None, "tenant-a"])
def test_subject_timeline_queries_do_not_shadow_pattern_variables(namespace) -> None:
    neo4j = _Recorder()
    repository = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]
    repository.timeline_page(namespace=namespace, subject_uuid="entity-1", limit=5)
    repository.timeline_anchor(uuid="ep-1", namespace=namespace, subject_uuid="entity-1")
    assert len(neo4j.calls) == 2
    for query in neo4j.calls:
        assert "s" in _pattern_vars(query)
        clash = _comprehension_vars(query) & _pattern_vars(query)
        assert not clash, clash
