"""#887: group_id-keyed build-count queries must bind the translated group_id."""

from __future__ import annotations

import re

from menhir.infrastructure.build_report_queries import BuildReportQueries


class _Neo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, dict(params or {})))
        return [{"c": 0, "admitted": 0, "unprojected": 0, "pending": 0}]


def _calls(namespace):
    neo4j = _Neo4j()
    q = BuildReportQueries(neo4j)
    q.namespace_build_counts(namespace, submitted_epoch_millis=[1])
    return neo4j.calls


def _group_keyed(calls):
    """(query, bound value) for every `group_id:$x` match."""
    out = []
    for text, params in calls:
        for name in re.findall(r"group_id:\$(\w+)", text):
            out.append((text, params[name]))
    return out


def test_default_namespace_binds_empty_group_id() -> None:
    keyed = _group_keyed(_calls("default"))
    assert len(keyed) >= 4
    assert all(value == "" for _, value in keyed)


def test_named_namespace_binds_the_name() -> None:
    keyed = _group_keyed(_calls("yawn"))
    assert keyed and all(value == "yawn" for _, value in keyed)


def test_namespace_keyed_queries_still_use_the_name() -> None:
    calls = _calls("default")
    assert any("TurnEvidence {namespace:$ns}" in q and p["ns"] == "default" for q, p in calls)


def test_cross_namespace_query_compares_group_id_to_translated_value() -> None:
    cross = [(q, p) for q, p in _calls("default") if "$gl" in q]
    assert cross and cross[0][1]["nl"] == "default" and cross[0][1]["gl"] == ""
