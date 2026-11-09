"""Offline tests for Neo4j vector-index similarity search (menhir #574).

No Neo4j and no network: every database is a scripted fake that dispatches on query text.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.search import search_utils
from graphiti_core.search.search_filters import SearchFilters

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_prefilter_cypher
from menhir.domain.recall_visibility import default_recall_visibility_cypher
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.infrastructure import graphiti_vector_search as gvs
from menhir.infrastructure import vector_indexes as vi
from menhir.infrastructure.graphiti_vector_search import MenhirSearchInterface
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.vector_indexes import (
    ANN_MAX_K,
    ENTITY_NAME_EMBEDDING,
    EPISODIC_CONTENT_EMBEDDING,
    EXACT_SCORE_MARGIN,
    QUANTIZED_SCORE_MARGIN,
    RELATES_TO_FACT_EMBEDDING,
    SHOW_VECTOR_INDEXES,
    TYPED_ASSERTION_NAME_EMBEDDING,
    VECTOR_INDEX_SPECS,
    VectorIndexCatalog,
    ann_call,
    ann_result_complete,
    ann_stats,
    ann_stats_query,
    ensure_vector_indexes,
    index_backed_rows_sync,
    initial_k,
    next_k,
    resolve_vector_dimensions,
    statuses_by_spec,
)


def _row(
    spec: vi.VectorIndexSpec,
    *,
    name: str | None = None,
    state: str = "ONLINE",
    dims: int | None = 3,
    similarity: str = "cosine",
    quantized: Any = "absent",
) -> dict[str, Any]:
    config: dict[str, Any] = {"vector.similarity_function": similarity}
    if dims is not None:
        config["vector.dimensions"] = dims
    if quantized != "absent":
        config["vector.quantization.enabled"] = quantized
    return {
        "name": name or spec.name,
        "state": state,
        "entityType": spec.entity_type,
        "labelsOrTypes": [spec.label],
        "properties": [spec.prop],
        "options": {"indexConfig": config},
    }


# ---------------------------------------------------------------------------
# 1. ann_result_complete
# ---------------------------------------------------------------------------


def _complete(**kw: Any) -> bool:
    base: dict[str, Any] = dict(
        raw_count=64, k=64, lowest_index_score=0.55, exact_scores=[], limit=3, min_score=None, margin=0.02
    )
    base.update(kw)
    return ann_result_complete(**base)


def test_complete_when_index_returned_fewer_than_k() -> None:
    assert _complete(raw_count=10, k=64, lowest_index_score=0.99, exact_scores=[], limit=5, min_score=0.0)


def test_complete_when_no_index_score() -> None:
    assert _complete(lowest_index_score=None)


def test_complete_when_bound_not_above_min_score() -> None:
    # bound = 0.55 + 0.02 = 0.57 <= 0.6: no unseen row can pass the threshold.
    assert _complete(lowest_index_score=0.55, margin=0.02, min_score=0.6, exact_scores=[])


def test_incomplete_when_bound_above_min_score_and_too_few_rows() -> None:
    # bound 0.57 > 0.5 and only 2 of 3 rows kept.
    assert not _complete(lowest_index_score=0.55, margin=0.02, min_score=0.5, exact_scores=[0.9, 0.8], limit=3)


def test_complete_when_limit_th_row_reaches_bound() -> None:
    assert _complete(lowest_index_score=0.55, margin=0.02, min_score=0.1, exact_scores=[0.9, 0.8, 0.58], limit=3)


def test_incomplete_when_limit_th_row_below_bound() -> None:
    # An unseen row could score up to 0.57 and outrank the 3rd kept row (0.56).
    assert not _complete(lowest_index_score=0.55, margin=0.02, min_score=0.1, exact_scores=[0.9, 0.8, 0.56], limit=3)


def test_min_score_none_means_no_threshold() -> None:
    # With a threshold of 0.99 this would be complete; without one it must not be.
    assert _complete(lowest_index_score=0.55, margin=0.02, min_score=0.99, exact_scores=[])
    assert not _complete(lowest_index_score=0.55, margin=0.02, min_score=None, exact_scores=[])


# ---------------------------------------------------------------------------
# 2. initial_k / next_k
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit,expected", [(1, 128), (15, 128), (50, 200), (10000, 1024)])
def test_initial_k(limit: int, expected: int) -> None:
    assert initial_k(limit) == expected


def test_next_k_doubles_and_caps() -> None:
    assert next_k(64) == 128
    assert next_k(512) == 1024
    assert next_k(700) == 1024  # clamped to the cap, not 1400
    assert next_k(1024) is None


# ---------------------------------------------------------------------------
# 3. statuses_by_spec / catalog
# ---------------------------------------------------------------------------


def test_row_with_other_name_covering_spec_is_used() -> None:
    found = statuses_by_spec([_row(ENTITY_NAME_EMBEDDING, name="my_custom_idx")])
    assert found[ENTITY_NAME_EMBEDDING].name == "my_custom_idx"
    assert RELATES_TO_FACT_EMBEDDING not in found


def test_own_name_wins_over_other_covering_row() -> None:
    rows = [
        _row(ENTITY_NAME_EMBEDDING, name="aaa_other"),
        _row(ENTITY_NAME_EMBEDDING),
        _row(ENTITY_NAME_EMBEDDING, name="zzz_other"),
    ]
    assert statuses_by_spec(rows)[ENTITY_NAME_EMBEDDING].name == ENTITY_NAME_EMBEDDING.name


def test_entity_type_must_match() -> None:
    row = _row(ENTITY_NAME_EMBEDDING)
    row["entityType"] = "RELATIONSHIP"
    assert ENTITY_NAME_EMBEDDING not in statuses_by_spec([row])


def _catalog_with(*rows: dict[str, Any]) -> VectorIndexCatalog:
    catalog = VectorIndexCatalog()
    catalog.update(rows)
    return catalog


def test_usable_online_matching_dimensions() -> None:
    status = _catalog_with(_row(ENTITY_NAME_EMBEDDING, dims=3)).usable(ENTITY_NAME_EMBEDDING, 3)
    assert status is not None and status.name == ENTITY_NAME_EMBEDDING.name


def test_usable_none_when_populating() -> None:
    assert _catalog_with(_row(ENTITY_NAME_EMBEDDING, state="POPULATING")).usable(ENTITY_NAME_EMBEDDING, 3) is None


def test_usable_none_on_dimension_mismatch() -> None:
    assert _catalog_with(_row(ENTITY_NAME_EMBEDDING, dims=4)).usable(ENTITY_NAME_EMBEDDING, 3) is None


def test_usable_none_for_euclidean() -> None:
    catalog = _catalog_with(_row(ENTITY_NAME_EMBEDDING, similarity="EUCLIDEAN"))
    assert catalog.usable(ENTITY_NAME_EMBEDDING, 3) is None


def test_usable_none_when_spec_missing() -> None:
    assert _catalog_with(_row(ENTITY_NAME_EMBEDDING)).usable(EPISODIC_CONTENT_EMBEDDING, 3) is None


def test_quantized_flag_and_margin() -> None:
    q = _catalog_with(_row(ENTITY_NAME_EMBEDDING, quantized=True)).usable(ENTITY_NAME_EMBEDDING, 3)
    assert q is not None and q.quantized and q.score_margin == QUANTIZED_SCORE_MARGIN
    f = _catalog_with(_row(ENTITY_NAME_EMBEDDING, quantized=False)).usable(ENTITY_NAME_EMBEDDING, 3)
    assert f is not None and not f.quantized and f.score_margin == EXACT_SCORE_MARGIN
    a = _catalog_with(_row(ENTITY_NAME_EMBEDDING)).usable(ENTITY_NAME_EMBEDDING, 3)
    assert a is not None and not a.quantized and a.score_margin == EXACT_SCORE_MARGIN


def test_catalog_ttl_invalidate_and_mark_unavailable() -> None:
    now = [100.0]
    catalog = VectorIndexCatalog(ttl_s=30.0, clock=lambda: now[0])
    assert catalog.needs_refresh()
    catalog.update([_row(ENTITY_NAME_EMBEDDING)])
    assert not catalog.needs_refresh()
    now[0] += 29.9
    assert not catalog.needs_refresh()
    now[0] += 0.2
    assert catalog.needs_refresh()

    catalog.update([_row(ENTITY_NAME_EMBEDDING)])
    assert catalog.usable(ENTITY_NAME_EMBEDDING, 3) is not None
    catalog.invalidate()
    assert catalog.needs_refresh()

    catalog.update([_row(spec) for spec in VECTOR_INDEX_SPECS])
    catalog.mark_unavailable()
    assert not catalog.needs_refresh()
    assert all(catalog.usable(spec, 3) is None for spec in VECTOR_INDEX_SPECS)
    # The next refresh restores them.
    catalog.update([_row(ENTITY_NAME_EMBEDDING)])
    assert catalog.usable(ENTITY_NAME_EMBEDDING, 3) is not None


# ---------------------------------------------------------------------------
# 4. ensure_vector_indexes
# ---------------------------------------------------------------------------


class _Exec:
    """Sync ``execute(query)`` recording every statement."""

    def __init__(self, show_rows: list[dict[str, Any]] | None = None, *, fail_show: bool = False,
                 create_failures: dict[int, Exception] | None = None, fail_all_creates: bool = False) -> None:
        self.show_rows = show_rows or []
        self.fail_show = fail_show
        self.create_failures = create_failures or {}
        self.fail_all_creates = fail_all_creates
        self.creates: list[str] = []
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        if query == SHOW_VECTOR_INDEXES:
            if self.fail_show:
                raise RuntimeError("show broke")
            return self.show_rows
        self.creates.append(query)
        if self.fail_all_creates:
            raise ValueError("no")
        failure = self.create_failures.get(len(self.creates) - 1)
        if failure:
            raise failure
        return []


def test_ensure_creates_every_spec_with_expected_ddl() -> None:
    execute = _Exec()
    report = ensure_vector_indexes(execute, 1536)
    assert report == {spec.name: "created" for spec in VECTOR_INDEX_SPECS}
    assert len(execute.creates) == len(VECTOR_INDEX_SPECS)
    for spec, ddl in zip(VECTOR_INDEX_SPECS, execute.creates):
        assert spec.name in ddl
        assert "IF NOT EXISTS" in ddl
        assert "`vector.dimensions`: 1536" in ddl
        assert "'cosine'" in ddl
        assert "`vector.quantization.enabled`: false" in ddl


def test_ensure_relationship_spec_ddl_shape() -> None:
    execute = _Exec()
    ensure_vector_indexes(execute, 8)
    rel = next(q for q in execute.creates if RELATES_TO_FACT_EMBEDDING.name in q)
    assert "FOR ()-[r:RELATES_TO]-() ON r.fact_embedding" in rel
    node = next(q for q in execute.creates if ENTITY_NAME_EMBEDDING.name in q)
    assert "FOR (n:Entity) ON n.name_embedding" in node


def test_ensure_retries_without_quantization_option() -> None:
    execute = _Exec(create_failures={0: RuntimeError("unsupported option")})
    report = ensure_vector_indexes(execute, 8)
    assert report[VECTOR_INDEX_SPECS[0].name] == "created"
    first, second = execute.creates[0], execute.creates[1]
    assert "quantization" in first
    assert "quantization" not in second
    assert VECTOR_INDEX_SPECS[0].name in second


def test_ensure_both_attempts_failing_reports_error_and_does_not_raise() -> None:
    execute = _Exec(fail_all_creates=True)
    report = ensure_vector_indexes(execute, 8)
    assert report == {spec.name: "error:ValueError" for spec in VECTOR_INDEX_SPECS}
    assert len(execute.creates) == 2 * len(VECTOR_INDEX_SPECS)


def test_ensure_unknown_dimensions_skips() -> None:
    execute = _Exec()
    report = ensure_vector_indexes(execute, None)
    assert execute.creates == []
    assert set(report.values()) == {"skipped:unknown_dimensions"}


def test_ensure_existing_index_not_recreated_and_state_lowercased() -> None:
    execute = _Exec([_row(ENTITY_NAME_EMBEDDING, name="legacy_name", state="POPULATING", dims=8)])
    report = ensure_vector_indexes(execute, 8)
    assert report[ENTITY_NAME_EMBEDDING.name] == "populating"
    assert not any("Entity)" in q and "name_embedding" in q for q in execute.creates)
    assert len(execute.creates) == len(VECTOR_INDEX_SPECS) - 1


def test_ensure_dimension_mismatch_warns_and_never_drops(caplog: pytest.LogCaptureFixture) -> None:
    execute = _Exec([_row(ENTITY_NAME_EMBEDDING, dims=4)])
    with caplog.at_level(logging.WARNING, logger=vi.logger.name):
        report = ensure_vector_indexes(execute, 8)
    assert report[ENTITY_NAME_EMBEDDING.name] == "online"
    assert any(r.levelno == logging.WARNING and "4" in r.getMessage() and "8" in r.getMessage()
               for r in caplog.records)
    assert not any("DROP" in q.upper() for q in execute.queries)


def test_ensure_show_failure_reports_errors_and_creates_nothing() -> None:
    execute = _Exec(fail_show=True)
    report = ensure_vector_indexes(execute, 8)
    assert report == {spec.name: "error:RuntimeError" for spec in VECTOR_INDEX_SPECS}
    assert execute.creates == []


# ---------------------------------------------------------------------------
# 5. resolve_vector_dimensions
# ---------------------------------------------------------------------------


def test_resolve_dimensions_configured_wins_without_query() -> None:
    def boom(_q: str) -> list[dict[str, Any]]:
        raise AssertionError("must not query")

    assert resolve_vector_dimensions(1536, boom) == 1536


def test_resolve_dimensions_samples_stored_embedding() -> None:
    seen: list[str] = []

    def execute(q: str) -> list[dict[str, Any]]:
        seen.append(q)
        return [{"d": 768}]

    assert resolve_vector_dimensions(None, execute) == 768
    assert len(seen) == 1 and "name_embedding" in seen[0]


def test_resolve_dimensions_empty_result_is_none() -> None:
    assert resolve_vector_dimensions(None, lambda _q: []) is None
    assert resolve_vector_dimensions(0, lambda _q: []) is None


# ---------------------------------------------------------------------------
# 6. index_backed_rows_sync
# ---------------------------------------------------------------------------

VEC = [0.1, 0.2, 0.3]


class _SyncDb:
    """Scripted ``execute(query, params)``: SHOW / stats dispatched on query text.

    ``stats`` is ``(raw, lowest)``, a callable ``k -> (raw, lowest)``, or an Exception to raise.
    ``search`` is a list of rows, a callable ``k -> rows``, or an Exception to raise.
    """

    def __init__(self, *, show: list[dict[str, Any]], stats: Any, search: Any = None) -> None:
        self.show = show
        self.stats = stats
        self.search = search if search is not None else []
        self.stats_calls: list[dict[str, Any]] = []
        self.search_calls: list[tuple[str, int]] = []
        self.stats_queries: list[str] = []

    def __call__(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if query == SHOW_VECTOR_INDEXES:
            return self.show
        assert "count(hit)" in query, f"unexpected statement {query!r}"
        self.stats_queries.append(query)
        self.stats_calls.append(params)
        if isinstance(self.stats, Exception):
            raise self.stats
        raw, lowest = self.stats(params["k"]) if callable(self.stats) else self.stats
        return [{"raw": raw, "lowest": lowest}]

    def run_search(self, index_name: str, k: int) -> list[dict[str, Any]]:
        self.search_calls.append((index_name, k))
        if isinstance(self.search, Exception):
            raise self.search
        return self.search(k) if callable(self.search) else self.search


def _run(db: _SyncDb, catalog: VectorIndexCatalog | None = None, *, limit: int = 3,
         min_score: float | None = None, spec: vi.VectorIndexSpec = EPISODIC_CONTENT_EMBEDDING):
    catalog = catalog or VectorIndexCatalog()
    return index_backed_rows_sync(
        execute=db,
        catalog=catalog,
        spec=spec,
        vector=VEC,
        limit=limit,
        hydrate=db.run_search,
        score_key="cosine",
        min_score=min_score,
    ), catalog


def test_ann_stats_query_shape() -> None:
    node_q = ann_stats_query(ENTITY_NAME_EMBEDDING, "search_vector")
    assert node_q.startswith("CALL db.index.vector.queryNodes($index_name, $k, $search_vector)")
    assert "YIELD node AS hit, score AS index_score" in node_q
    assert node_q.endswith("RETURN count(hit) AS raw, min(index_score) AS lowest")
    rel_q = ann_stats_query(RELATES_TO_FACT_EMBEDDING)
    assert rel_q.startswith("CALL db.index.vector.queryRelationships($index_name, $k, $query_vector)")
    assert "YIELD relationship AS hit" in rel_q


def test_ann_stats_row_parsing() -> None:
    assert ann_stats(None) == (0, None)
    assert ann_stats({}) == (0, None)
    assert ann_stats({"raw": 5, "lowest": 0.7}) == (5, 0.7)
    assert ann_stats({"raw": 0, "lowest": None}) == (0, None)
    assert ann_stats({"raw": 3, "lowest": float("nan")}) == (3, None)


def test_sync_no_usable_index_returns_none_without_ann() -> None:
    db = _SyncDb(show=[], stats=(1, 0.9))
    rows, _ = _run(db)
    assert rows is None
    assert db.stats_calls == [] and db.search_calls == []


def test_sync_complete_first_call_returns_search_rows() -> None:
    found = [{"uuid": "u1", "cosine": 0.88}]
    db = _SyncDb(show=[_row(EPISODIC_CONTENT_EMBEDDING)], stats=(1, 0.9), search=found)
    rows, _ = _run(db, limit=5)
    assert rows == found
    assert len(db.stats_calls) == 1
    params = db.stats_calls[0]
    assert params["index_name"] == EPISODIC_CONTENT_EMBEDDING.name
    assert params["k"] == initial_k(5) == 128
    assert params["query_vector"] == VEC
    assert db.search_calls == [(EPISODIC_CONTENT_EMBEDDING.name, 128)]


def test_sync_incomplete_doubles_k_then_completes() -> None:
    # Full at k=128 (and only 1 kept < limit 3, so incomplete); short at k=256 (complete).
    db = _SyncDb(
        show=[_row(EPISODIC_CONTENT_EMBEDDING)],
        stats=lambda k: (128, 0.9) if k == 128 else (140, 0.9),
        search=[{"uuid": "u0", "cosine": 0.9}],
    )
    rows, _ = _run(db, limit=3, min_score=None)
    assert [p["k"] for p in db.stats_calls] == [128, 256]
    assert [k for _, k in db.search_calls] == [128, 256]
    assert rows == [{"uuid": "u0", "cosine": 0.9}]


def test_sync_incomplete_at_cap_returns_none() -> None:
    db = _SyncDb(
        show=[_row(EPISODIC_CONTENT_EMBEDDING)],
        stats=lambda k: (k, 0.9),
        search=[{"uuid": "u0", "cosine": 0.9}],
    )
    rows, _ = _run(db, limit=3)
    assert rows is None
    assert [p["k"] for p in db.stats_calls] == [128, 256, 512, ANN_MAX_K]


def test_sync_vector_query_error_returns_none_and_invalidates() -> None:
    db = _SyncDb(show=[_row(EPISODIC_CONTENT_EMBEDDING)],
                 stats=RuntimeError("Failed to invoke procedure `db.index.vector.queryNodes`"))
    rows, catalog = _run(db)
    assert rows is None
    assert catalog.needs_refresh()


def test_sync_search_statement_error_also_returns_none_and_invalidates() -> None:
    db = _SyncDb(show=[_row(EPISODIC_CONTENT_EMBEDDING)], stats=(1, 0.9),
                 search=RuntimeError("db.index.vector.queryNodes: index is gone"))
    rows, catalog = _run(db)
    assert rows is None
    assert catalog.needs_refresh()


def test_sync_unrelated_error_propagates() -> None:
    db = _SyncDb(show=[_row(EPISODIC_CONTENT_EMBEDDING)], stats=KeyError("boom"))
    with pytest.raises(KeyError):
        _run(db)


def test_sync_zero_raw_skips_search_and_returns_empty() -> None:
    db = _SyncDb(show=[_row(EPISODIC_CONTENT_EMBEDDING)], stats=(0, None))
    rows, _ = _run(db)
    assert rows == []
    assert db.search_calls == []


# ---------------------------------------------------------------------------
# 7. MemoryQueryRepository lanes
# ---------------------------------------------------------------------------


class _RepoNeo4j:
    def __init__(self, show: list[dict[str, Any]] | None = None, stats: tuple[int, float | None] = (1, 0.9),
                 search_rows: list[dict[str, Any]] | None = None) -> None:
        self.show = show or []
        self.stats = stats
        self.search_rows = search_rows or []
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if query == SHOW_VECTOR_INDEXES:
            return self.show
        if "count(hit)" in query:
            return [{"raw": self.stats[0], "lowest": self.stats[1]}]
        if "db.index.vector.query" in query:
            return self.search_rows
        return [{"scan": True}]

    def kinds(self) -> list[str]:
        out = []
        for q, _ in self.calls:
            if q == SHOW_VECTOR_INDEXES:
                out.append("show")
            elif "count(hit)" in q:
                out.append("stats")
            elif "db.index.vector.query" in q:
                out.append("search")
            else:
                out.append("scan")
        return out


def test_lanes_without_catalog_issue_single_scan() -> None:
    neo4j = _RepoNeo4j()
    repo = MemoryQueryRepository(neo4j)  # type: ignore[arg-type]
    repo.search_episode_embeddings([0.1, 0.2], limit=5)
    assert len(neo4j.calls) == 1 and "MATCH (n:Episodic)" in neo4j.calls[0][0]

    neo4j2 = _RepoNeo4j()
    repo2 = MemoryQueryRepository(neo4j2)  # type: ignore[arg-type]
    repo2.search_assertion_embeddings([0.1, 0.2], limit=5)
    assert len(neo4j2.calls) == 1 and "MATCH (a:TypedAssertion)" in neo4j2.calls[0][0]


def test_episode_lane_uses_index_with_same_predicates_as_scan() -> None:
    found = [{"uuid": "u1", "cosine": 0.9}]
    neo4j = _RepoNeo4j(
        show=[_row(EPISODIC_CONTENT_EMBEDDING, dims=2)],
        stats=(1, 0.9),
        search_rows=found,
    )
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    rows = repo.search_episode_embeddings([0.1, 0.2], limit=5, namespace="tenant-a")
    assert rows == found
    assert neo4j.kinds() == ["show", "stats", "search"]
    stats_query, stats_params = neo4j.calls[1]
    assert stats_query == ann_stats_query(EPISODIC_CONTENT_EMBEDDING)
    assert stats_params is not None and stats_params["query_vector"] == [0.1, 0.2]
    hydrate_query, hydrate_params = neo4j.calls[2]
    assert hydrate_query.startswith(ann_call(EPISODIC_CONTENT_EMBEDDING, "n") + " WITH n WHERE ")
    # No second-statement lookup by id: that let the planner pick another index.
    assert "UNWIND $hits" not in hydrate_query and "elementId(" not in hydrate_query
    assert hydrate_params is not None
    assert hydrate_params["index_name"] == EPISODIC_CONTENT_EMBEDDING.name
    assert hydrate_params["k"] == initial_k(5)
    assert hydrate_params["query_vector"] == [0.1, 0.2] and hydrate_params["limit"] == 5
    assert "MATCH (n:Episodic)" not in hydrate_query

    # Same tenant / visibility predicates as the scan, which a catalog-less repo issues.
    scan_neo4j = _RepoNeo4j()
    MemoryQueryRepository(scan_neo4j).search_episode_embeddings(  # type: ignore[arg-type]
        [0.1, 0.2], limit=5, namespace="tenant-a"
    )
    scan_query = scan_neo4j.calls[0][0]
    for predicate in (non_structural_memory_cypher("n"), default_recall_visibility_cypher("n")):
        assert predicate in scan_query
        assert predicate in hydrate_query
    assert tenant_scope_cypher("n") in scan_query
    assert tenant_scope_cypher("n") in hydrate_query
    assert tenant_scope_prefilter_cypher("n") in hydrate_query


def test_assertion_lane_search_starts_at_index_call_with_scan_predicates() -> None:
    found = [{"assertion_id": "a1", "cosine": 0.8}]
    neo4j = _RepoNeo4j(
        show=[_row(TYPED_ASSERTION_NAME_EMBEDDING, dims=2)],
        stats=(1, 0.8),
        search_rows=found,
    )
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    assert repo.search_assertion_embeddings([0.1, 0.2], limit=5, namespaces=["ns"]) == found
    assert neo4j.kinds() == ["show", "stats", "search"]
    query, params = neo4j.calls[2]
    assert query.startswith(ann_call(TYPED_ASSERTION_NAME_EMBEDDING, "a") + " WITH a WHERE ")
    assert "NOT coalesce(a.superseded, false)" in query
    assert "NOT coalesce(a.binding_pending, false)" in query
    assert "a.namespace IN $namespaces" in query
    assert "UNWIND $hits" not in query and "elementId(" not in query and "assertion_id = h" not in query
    assert params is not None and params["namespaces"] == ["ns"]
    assert params["index_name"] == TYPED_ASSERTION_NAME_EMBEDDING.name


def test_lane_zero_raw_never_sends_search() -> None:
    neo4j = _RepoNeo4j(show=[_row(TYPED_ASSERTION_NAME_EMBEDDING, dims=2)], stats=(0, None))
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    assert repo.search_assertion_embeddings([0.1, 0.2], limit=5) == []
    assert neo4j.kinds() == ["show", "stats"]


def test_lane_incomplete_at_cap_falls_back_to_scan() -> None:
    class _Full(_RepoNeo4j):
        def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "count(hit)" in query:
                assert params is not None
                self.calls.append((query, params))
                return [{"raw": params["k"], "lowest": 0.9}]
            return super().execute(query, params)

    neo4j = _Full(show=[_row(TYPED_ASSERTION_NAME_EMBEDDING, dims=2)], search_rows=[])
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    repo.search_assertion_embeddings([0.1, 0.2], limit=5)
    assert neo4j.kinds()[-1] == "scan"
    assert "MATCH (a:TypedAssertion)" in neo4j.calls[-1][0]


def test_episode_lane_with_subject_uuid_scans_without_ann() -> None:
    neo4j = _RepoNeo4j(
        show=[_row(EPISODIC_CONTENT_EMBEDDING, dims=2)],
        stats=(1, 0.9),
    )
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    repo.search_episode_embeddings([0.1, 0.2], limit=5, subject_uuid="s1")
    assert "stats" not in neo4j.kinds() and "search" not in neo4j.kinds()
    assert neo4j.kinds()[-1] == "scan"
    assert "MATCH (n:Episodic)" in neo4j.calls[-1][0]


def test_episode_lane_falls_back_to_scan_when_dimension_mismatch() -> None:
    neo4j = _RepoNeo4j(show=[_row(EPISODIC_CONTENT_EMBEDDING, dims=1536)])
    repo = MemoryQueryRepository(neo4j, vector_catalog=VectorIndexCatalog())  # type: ignore[arg-type]
    repo.search_episode_embeddings([0.1, 0.2], limit=5)
    assert neo4j.kinds() == ["show", "scan"]


# ---------------------------------------------------------------------------
# 8. MenhirSearchInterface
# ---------------------------------------------------------------------------


class _Rec(dict):
    def data(self) -> dict[str, Any]:
        return dict(self)


class _FakeDriver:
    provider = GraphProvider.NEO4J
    search_interface = "SENTINEL"

    def __init__(self, show: list[dict[str, Any]] | None = None, stats: tuple[int, float | None] = (1, 0.9),
                 search: list[dict[str, Any]] | None = None) -> None:
        self.show = show or []
        self.stats = stats
        self.search_records = search or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **kwargs: Any):
        self.calls.append((query, kwargs))
        if query == SHOW_VECTOR_INDEXES:
            return [_Rec(r) for r in self.show], None, None
        if "count(hit)" in query:
            return [_Rec({"raw": self.stats[0], "lowest": self.stats[1]})], None, None
        return [_Rec(r) for r in self.search_records], None, None

    def kinds(self) -> list[str]:
        return [
            "show" if q == SHOW_VECTOR_INDEXES else "stats" if "count(hit)" in q else "search"
            for q, _ in self.calls
        ]


DELEGATED = (
    "edge_fulltext_search",
    "node_fulltext_search",
    "episode_fulltext_search",
    "edge_bfs_search",
    "node_bfs_search",
    "community_fulltext_search",
    "community_similarity_search",
    "get_embeddings_for_communities",
    "node_distance_reranker",
    "episode_mentions_reranker",
)


@pytest.mark.parametrize("name", DELEGATED)
async def test_delegated_methods_call_stock_with_plain_driver_view(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def fake(view: Any, *args: Any, **kwargs: Any) -> str:
        seen["view"], seen["args"], seen["kwargs"] = view, args, kwargs
        return "sentinel-result"

    monkeypatch.setattr(search_utils, name, fake)
    driver = _FakeDriver()
    result = await getattr(MenhirSearchInterface(), name)(driver, "a", 2, key="v")
    assert result == "sentinel-result"
    assert seen["args"] == ("a", 2) and seen["kwargs"] == {"key": "v"}
    assert seen["view"].search_interface is None
    assert driver.search_interface == "SENTINEL"  # the real driver is untouched
    assert seen["view"].provider == GraphProvider.NEO4J
    assert seen["view"].execute_query == driver.execute_query


def _patch_stock_similarity(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple, Any]]:
    calls: list[tuple[str, tuple, Any]] = []

    def make(name: str):
        async def fake(view: Any, *args: Any, **kwargs: Any) -> str:
            calls.append((name, args, view))
            return f"stock-{name}"
        return fake

    monkeypatch.setattr(search_utils, "node_similarity_search", make("node"))
    monkeypatch.setattr(search_utils, "edge_similarity_search", make("edge"))
    return calls


def _online(spec: vi.VectorIndexSpec, dims: int = 3) -> dict[str, Any]:
    return _row(spec, dims=dims)


async def test_node_search_falls_back_when_provider_not_neo4j(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(ENTITY_NAME_EMBEDDING)])
    driver.provider = GraphProvider.FALKORDB
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), ["g"], 5, 0.5)
    assert out == "stock-node" and calls[0][2].search_interface is None
    assert driver.calls == []


async def test_node_search_falls_back_when_no_usable_index(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[])
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), ["g"], 5, 0.5)
    assert out == "stock-node"
    assert calls[0][1][0] == VEC and calls[0][1][2:] == (["g"], 5, 0.5)
    assert driver.kinds() == ["show"]


async def test_node_search_falls_back_on_empty_group_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(ENTITY_NAME_EMBEDDING)])
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), [], 5, 0.5)
    assert out == "stock-node" and len(calls) == 1
    assert driver.calls == []


@pytest.mark.parametrize(
    "search_filter,source,target,group_ids",
    [
        (SearchFilters(edge_uuids=["e1"]), None, None, ["g"]),
        (SearchFilters(edge_source_node_uuids=["s1"]), None, None, ["g"]),
        (SearchFilters(edge_target_node_uuids=["t1"]), None, None, ["g"]),
        (SearchFilters(), "s1", None, ["g"]),
        (SearchFilters(), None, "t1", ["g"]),
        (SearchFilters(), None, None, []),
    ],
)
async def test_edge_search_falls_back(
    monkeypatch: pytest.MonkeyPatch, search_filter: Any, source: Any, target: Any, group_ids: Any
) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(RELATES_TO_FACT_EMBEDDING)])
    out = await MenhirSearchInterface().edge_similarity_search(
        driver, VEC, source, target, search_filter, group_ids, 5, 0.5
    )
    assert out == "stock-edge" and len(calls) == 1
    assert "stats" not in driver.kinds() and "search" not in driver.kinds()


async def test_edge_search_uses_index_when_endpoint_given_but_group_ids_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(RELATES_TO_FACT_EMBEDDING)], stats=(0, None))
    out = await MenhirSearchInterface().edge_similarity_search(
        driver, VEC, "s1", None, SearchFilters(), None, 5, 0.5
    )
    assert out == []
    assert calls == []
    assert driver.kinds() == ["show", "stats"]


async def test_edge_search_query_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    monkeypatch.setattr(gvs, "get_entity_edge_from_record", lambda r, p: ("edge", r["score"]))
    driver = _FakeDriver(show=[_online(RELATES_TO_FACT_EMBEDDING)], stats=(1, 0.9), search=[{"score": 0.8}])
    out = await MenhirSearchInterface().edge_similarity_search(
        driver, VEC, None, None, SearchFilters(), ["g1"], 5, 0.5
    )
    assert out == [("edge", 0.8)] and calls == []
    assert driver.kinds() == ["show", "stats", "search"]
    query, kwargs = driver.calls[2]
    assert query.startswith(ann_call(RELATES_TO_FACT_EMBEDDING, "e", "search_vector") + " MATCH (n:Entity)-[e]->(m:Entity)")
    assert "e.group_id IN $group_ids" in query
    assert "UNWIND $hits" not in query and "elementId(" not in query
    assert kwargs["group_ids"] == ["g1"] and kwargs["min_score"] == 0.5 and kwargs["limit"] == 5
    assert kwargs["index_name"] == RELATES_TO_FACT_EMBEDDING.name and kwargs["k"] == initial_k(5)
    assert kwargs["search_vector"] == VEC


async def test_node_search_index_path_hydrates_entity_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    now = datetime.now(timezone.utc)
    record = {
        "uuid": "n-1", "name": "Alice", "group_id": "g1", "created_at": now, "summary": "s",
        "labels": ["Entity"], "attributes": {"uuid": "n-1", "name": "Alice", "extra": 5}, "score": 0.9,
    }
    driver = _FakeDriver(
        show=[_online(ENTITY_NAME_EMBEDDING)],
        stats=(1, 0.91),
        search=[record],
    )
    nodes = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), ["g1"], 5, 0.5)
    assert calls == []
    assert [type(n).__name__ for n in nodes] == ["EntityNode"]
    assert nodes[0].uuid == "n-1" and nodes[0].name == "Alice" and nodes[0].group_id == "g1"
    assert nodes[0].attributes == {"extra": 5}
    assert driver.kinds() == ["show", "stats", "search"]
    stats_query, stats_kwargs = driver.calls[1]
    assert stats_query == ann_stats_query(ENTITY_NAME_EMBEDDING, "search_vector")
    assert stats_kwargs["index_name"] == ENTITY_NAME_EMBEDDING.name
    assert stats_kwargs["k"] == initial_k(5)
    assert stats_kwargs["search_vector"] == VEC
    query, kwargs = driver.calls[2]
    assert query.startswith(ann_call(ENTITY_NAME_EMBEDDING, "n", "search_vector") + " WITH n WHERE ")
    assert "n.group_id IN $group_ids" in query
    assert "UNWIND $hits" not in query and "elementId(" not in query
    assert kwargs["group_ids"] == ["g1"]
    assert kwargs["min_score"] == 0.5 and kwargs["limit"] == 5
    assert kwargs["index_name"] == ENTITY_NAME_EMBEDDING.name and kwargs["k"] == initial_k(5)
    assert kwargs["search_vector"] == VEC


async def test_node_search_index_path_omits_group_clause_when_group_ids_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(ENTITY_NAME_EMBEDDING)], stats=(1, 0.91), search=[])
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), None, 5, 0.5)
    assert out == []
    query = driver.calls[2][0]
    assert "n.group_id IN $group_ids" not in query
    # No filters: no WHERE between the index call's WITH and the rescore WITH.
    assert " WITH n WITH n, vector.similarity.cosine(n.name_embedding, $search_vector) AS score " in query
    assert "group_ids" not in driver.calls[2][1]


async def test_node_search_zero_raw_never_sends_search_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_stock_similarity(monkeypatch)
    driver = _FakeDriver(show=[_online(ENTITY_NAME_EMBEDDING)], stats=(0, None))
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), ["g"], 5, 0.5)
    assert out == [] and calls == []
    assert driver.kinds() == ["show", "stats"]


async def test_node_search_vector_query_error_invalidates_catalog_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_stock_similarity(monkeypatch)

    class _Boom(_FakeDriver):
        async def execute_query(self, query: str, **kwargs: Any):
            if "count(hit)" in query:
                raise RuntimeError("Failed to invoke procedure `db.index.vector.queryNodes`")
            return await super().execute_query(query, **kwargs)

    iface = MenhirSearchInterface()
    out = await iface.node_similarity_search(_Boom(show=[_online(ENTITY_NAME_EMBEDDING)]), VEC, SearchFilters(), ["g"], 5, 0.5)
    assert out == "stock-node" and len(calls) == 1
    assert iface.catalog.needs_refresh()


async def test_node_search_falls_back_to_stock_when_ann_incomplete_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_stock_similarity(monkeypatch)

    class _Full(_FakeDriver):
        async def execute_query(self, query: str, **kwargs: Any):
            if "count(hit)" in query:
                self.calls.append((query, kwargs))
                return [_Rec({"raw": kwargs["k"], "lowest": 0.9})], None, None
            return await super().execute_query(query, **kwargs)

    driver = _Full(show=[_online(ENTITY_NAME_EMBEDDING)])
    out = await MenhirSearchInterface().node_similarity_search(driver, VEC, SearchFilters(), ["g"], 5, 0.5)
    assert out == "stock-node" and len(calls) == 1
    ann_ks = [kw["k"] for q, kw in driver.calls if "count(hit)" in q]
    assert ann_ks == [128, 256, 512, ANN_MAX_K]
