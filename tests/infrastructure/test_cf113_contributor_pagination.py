"""CF-113 — contributor pagination must not materialise every row before slicing.

Both `fetch_scalar_authority_contributors` and `list_scalar_history_entries` used to
`collect()` every contributor/history row server-side and slice afterwards
(`all_rows[$offset..($offset + $limit)]`, `size(all_rows)`), so `$limit`/`$offset` bounded
only the wire, not the database work. We assert the QUERY SHAPE (no database here):
`SKIP`/`LIMIT` are pushed before `collect()`, `total` comes from a `count()`, the ordering is
total, the namespace predicate survives, and the returned dict shape is unchanged.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin


class _RecordingNeo4j:
    """Routes each executed Cypher by substring and records every call."""

    def __init__(self, routes, default=None):
        self._routes = list(routes)
        self.default = [] if default is None else default
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.calls.append((query, params or {}))
        for sub, rows in self._routes:
            if sub in query:
                return rows
        return self.default


class _Repo(ScalarViewRepositoryMixin):
    def __init__(self, neo4j) -> None:
        self.neo4j = neo4j


def _page_routes(*, total, contributors=None, entries=None):
    rows = [{"total": total}]
    routes = [("RETURN count(*) AS total", rows)]
    if contributors is not None:
        routes.append(("RETURN contributors", [{"contributors": contributors}]))
    if entries is not None:
        routes.append(("RETURN entries", [{"entries": entries}]))
    return routes


def _emitted(fake):
    """The full text of every query the repository executed this call."""
    return "\n".join(q for q, _ in fake.calls)


def test_both_queries_push_skip_limit_before_collect_and_drop_the_slice() -> None:
    """The finding: SKIP/LIMIT must precede collect() and the `all_rows[$offset..` slice is gone."""
    fake = _RecordingNeo4j(_page_routes(total=1, contributors=[], entries=[]))
    repo = _Repo(fake)
    repo.fetch_scalar_authority_contributors(view_uuid="v1", limit=8, offset=2, namespace="ns")
    repo.list_scalar_history_entries(view_uuid="v1", limit=8, offset=2, namespace="ns")

    for query, _ in fake.calls:
        assert "all_rows[$offset" not in query
        assert "size(all_rows)" not in query
        if "SKIP" in query:
            # SKIP and LIMIT must appear BEFORE the page-building collect() aggregation. (A
            # preceding collect(DISTINCT ...) -- e.g. in the history query -- must stay before the
            # pagination, so we anchor on the LAST collect, which is the projection.)
            skip = query.index("SKIP $offset")
            limit = query.index("LIMIT $limit")
            collect = query.rindex("collect(")
            assert skip < collect and limit < collect, "SKIP/LIMIT must precede collect()"
            # Both pagination tokens survive on the page query.
            assert "SKIP $offset" in query and "LIMIT $limit" in query

    # Every call is either the count query or the paged query.
    assert len(fake.calls) == 4


def test_total_is_a_real_count_not_the_size_of_the_collected_list() -> None:
    fake = _RecordingNeo4j(_page_routes(total=17, contributors=[], entries=[]))
    _Repo(fake).fetch_scalar_authority_contributors(view_uuid="v1", limit=8, offset=0, namespace="ns")

    emitted = _emitted(fake)
    assert "count(*)" in emitted
    assert "count(" in emitted
    assert "size(all_rows)" not in emitted


def test_the_order_is_total_before_the_pagination() -> None:
    fake = _RecordingNeo4j(_page_routes(total=1, contributors=[], entries=[]))
    _Repo(fake).fetch_scalar_authority_contributors(view_uuid="v1", limit=8, offset=0, namespace="ns")
    _Repo(fake).list_scalar_history_entries(view_uuid="v1", limit=8, offset=0, namespace="ns")

    for query, _ in fake.calls:
        if "SKIP $offset" not in query:
            continue
        # The ORDER BY must precede the pagination and include a unique field.
        assert query.index("ORDER BY") < query.index("SKIP $offset")
        assert "assertion_id" in query or "he.ordinal" in query


def test_namespace_predicate_is_preserved() -> None:
    """POSITIVE CONTROL: a rewrite that dropped the namespace predicate would be a tenant leak."""
    fake = _RecordingNeo4j(_page_routes(total=1, contributors=[], entries=[]))
    repo = _Repo(fake)
    repo.fetch_scalar_authority_contributors(view_uuid="v1", limit=8, offset=0, namespace="ns")
    repo.list_scalar_history_entries(view_uuid="v1", limit=8, offset=0, namespace="ns")

    for query, params in fake.calls:
        assert "$namespace" in query
        assert "v.group_id = $namespace" in query
        assert params.get("namespace") == "ns"


def test_contributors_returned_dict_shape_is_unchanged() -> None:
    """POSITIVE CONTROL: the caller-facing dict keeps every key it always returned."""
    contributor = {
        "assertion_id": "assert-current", "relation": "CURRENT_ANCHOR",
        "operation": "absolute", "value": 37, "stated_span": "I own 37 rare coins",
        "valid_at": "2026-07-02T00:00:00+00:00", "evidence_tier": "user",
        "episode_uuid": "ep-current",
    }
    fake = _RecordingNeo4j(_page_routes(total=3, contributors=[contributor]))
    out = _Repo(fake).fetch_scalar_authority_contributors(
        view_uuid="v1", limit=1, offset=1, namespace="ns")

    assert set(out) == {"contributors", "total", "next_offset"}
    assert out["total"] == 3
    assert out["next_offset"] == 2
    assert len(out["contributors"]) == 1
    for key in ("assertion_id", "relation", "operation", "value", "stated_span",
                "valid_at", "evidence_tier", "episode_uuid"):
        assert out["contributors"][0][key] == contributor[key]


def test_history_entries_returned_dict_shape_is_unchanged() -> None:
    entry = {
        "assertion_id": "assert-1", "ordinal": 0, "operation": "absolute",
        "value": "37", "stated_span": "I owned 37 rare coins", "valid_at": "2026-07-02",
        "evidence_tier": "user", "episode_uuid": "ep-1", "source_episode_uuid": "ep-1",
        "turn_id": "turn-1",
    }
    fake = _RecordingNeo4j(_page_routes(total=5, entries=[entry, entry]))
    out = _Repo(fake).list_scalar_history_entries(view_uuid="v1", limit=2, offset=0, namespace="ns")

    assert set(out) == {"entries", "total", "next_offset"}
    assert out["total"] == 5
    assert out["next_offset"] == 2
    assert len(out["entries"]) == 2
    for key in ("assertion_id", "ordinal", "operation", "value", "stated_span",
                "valid_at", "evidence_tier", "episode_uuid", "source_episode_uuid", "turn_id"):
        assert out["entries"][0][key] == entry[key]


@pytest.mark.unit
def test_parameters_are_still_named_the_same() -> None:
    fake = _RecordingNeo4j(_page_routes(total=1, contributors=[], entries=[]))
    _Repo(fake).fetch_scalar_authority_contributors(
        view_uuid="v1", limit=8, offset=2, namespace="ns")

    page_query = [q for q, _ in fake.calls if "SKIP $offset" in q][0]
    page_params = dict(fake.calls[[i for i, (q, _) in enumerate(fake.calls) if "SKIP" in q][0]][1])
    assert "offset" in page_params and "limit" in page_params
    assert page_params["offset"] == 2 and page_params["limit"] == 8
    assert "$offset" in page_query and "$limit" in page_query and "$view_uuid" in page_query
