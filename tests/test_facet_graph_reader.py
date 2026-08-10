"""Tests for Neo4jFacetGraphReader row->FacetInputs mapping (offline; stubbed executor)."""

from __future__ import annotations

import pytest

from menhir.infrastructure.facet_graph_reader import Neo4jFacetGraphReader


class _FakeNeo4j:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append((query, params))
        return self._rows


@pytest.mark.unit
def test_maps_rows_to_facet_inputs_filtering_nulls() -> None:
    rows = [{
        "uuid": "m1", "content": "fixed the floor", "namespace": "lme-001",
        "conflict_status": None, "created_at": "2026-07-01T00:00:00Z",
        "files": ["recall_service.py", None], "projects": ["menhir"], "symbols": ["recall", None],
    }]
    fi = Neo4jFacetGraphReader(_FakeNeo4j(rows)).fetch_facet_inputs(["m1"])["m1"]
    assert fi.anchored_files == ("recall_service.py",)   # nulls filtered
    assert fi.anchored_symbols == ("recall",)
    assert fi.project == "menhir"
    assert fi.namespace == "lme-001"
    assert fi.belief_bucket is None
    assert fi.valid_time == "2026-07-01T00:00:00Z"


@pytest.mark.unit
def test_multi_project_scope_is_ambiguous_none() -> None:
    rows = [{"uuid": "m", "content": "", "namespace": "default", "conflict_status": None,
             "created_at": None, "files": ["a.py"], "projects": ["menhir", "yawn"], "symbols": []}]
    fi = Neo4jFacetGraphReader(_FakeNeo4j(rows)).fetch_facet_inputs(["m"])["m"]
    assert fi.project is None   # spans two projects -> no unambiguous scope


@pytest.mark.unit
def test_stale_conflict_maps_to_historical_bucket() -> None:
    rows = [{"uuid": "m", "content": "", "namespace": "default", "conflict_status": "superseded",
             "created_at": None, "files": [], "projects": [], "symbols": []}]
    fi = Neo4jFacetGraphReader(_FakeNeo4j(rows)).fetch_facet_inputs(["m"])["m"]
    assert fi.belief_bucket == "historical"


@pytest.mark.unit
def test_empty_pool_short_circuits_without_a_query() -> None:
    fake = _FakeNeo4j([])
    assert Neo4jFacetGraphReader(fake).fetch_facet_inputs([]) == {}
    assert fake.calls == []


@pytest.mark.unit
def test_single_bounded_query_over_the_whole_pool() -> None:
    fake = _FakeNeo4j([])
    Neo4jFacetGraphReader(fake).fetch_facet_inputs(["a", "b", "c"])
    assert len(fake.calls) == 1
    assert fake.calls[0][1] == {"uuids": ["a", "b", "c"]}
