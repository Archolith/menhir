"""Live proof of the scalar View drill-down helper against real Cypher: contributor paging,
history fallback, namespace scoping and the counting kill switch through the real adapter.

Same fixture as test_counting_review_fixes_live.py: fresh namespace per test, removed afterwards.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.scalar_view_items import fetch_scalar_view_items
from tests.test_counting_review_fixes_live import _seed_view_graph


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"svi-{uuidlib.uuid4().hex[:10]}"
    yield test_neo4j_repo, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace STARTS WITH $ns OR n.group_id STARTS WITH $ns "
        "OR n.namespace_key STARTS WITH $ns DETACH DELETE n", {"ns": ns})


def _ids(items):
    return [i["assertion_id"].split(":", 1)[1] for i in items]


def _seed_history_only(repo, ns):
    view = f"{ns}-hist"
    repo.execute(
        """
        CREATE (v:Entity {uuid: $view, group_id: $ns, name: 'h'})
        CREATE (a:TypedAssertion {assertion_id: $ns + ':h0', operation: 'absolute', namespace: $ns,
                                  value: 3, stated_span: 'I have 3', valid_at: datetime('2026-07-01T00:00:00Z')})
        CREATE (b:TypedAssertion {assertion_id: $ns + ':h1', operation: 'absolute', namespace: $ns,
                                  value: 4, stated_span: 'now 4', valid_at: datetime('2026-07-05T00:00:00Z')})
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 0, operation: 'absolute'}]->(a)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 1, operation: 'absolute'}]->(b)
        """,
        {"view": view, "ns": ns},
    )
    return view


@pytest.mark.online
@pytest.mark.parametrize("counting_enabled,expected", [(False, ["abs", "delta"]),
                                                       (True, ["abs", "delta", "occ"])])
def test_state_view_items_follow_the_kill_switch(env, counting_enabled, expected):
    # Fails if the helper bypasses the adapter's include_occurrences default.
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    adapter = MemoryGraphAdapter(neo4j=repo, counting_enabled=counting_enabled)
    out = fetch_scalar_view_items(adapter, view_uuid=view, namespace=ns)
    assert out["kind"] == "scalar_state"
    assert sorted(_ids(out["items"])) == sorted(expected)
    assert out["total"] == len(expected)
    assert {i["relation"] for i in out["items"]} <= {"CURRENT_ANCHOR", "CONTRIBUTED_TO"}


@pytest.mark.online
def test_state_view_items_page(env):
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    adapter = MemoryGraphAdapter(neo4j=repo, counting_enabled=True)
    p1 = fetch_scalar_view_items(adapter, view_uuid=view, namespace=ns, limit=1)
    p2 = fetch_scalar_view_items(adapter, view_uuid=view, namespace=ns, offset=1, limit=50)
    assert p1["total"] == 3 and p1["next_offset"] == 1 and len(p1["items"]) == 1
    assert sorted(_ids(p1["items"]) + _ids(p2["items"])) == ["abs", "delta", "occ"]


@pytest.mark.online
def test_history_view_falls_back_to_entries(env):
    repo, ns = env
    view = _seed_history_only(repo, ns)
    adapter = MemoryGraphAdapter(neo4j=repo)
    out = fetch_scalar_view_items(adapter, view_uuid=view, namespace=ns)
    assert out["kind"] == "scalar_history"
    assert _ids(out["items"]) == ["h0", "h1"]
    assert [i["quote"] for i in out["items"]] == ["I have 3", "now 4"]
    assert {i["relation"] for i in out["items"]} == {"HISTORY_ENTRY"}


@pytest.mark.online
def test_view_in_another_namespace_returns_nothing(env):
    # Fails if the helper reads unscoped (namespace None or dropped).
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    adapter = MemoryGraphAdapter(neo4j=repo, counting_enabled=True)
    for other in (f"{ns}-other", None):
        out = fetch_scalar_view_items(adapter, view_uuid=view, namespace=other)
        assert out == {"view_uuid": view, "kind": None, "total": 0, "offset": 0,
                       "next_offset": None, "items": []}
