"""Live proof of the counting review fixes (unit 3a): occurrence-hiding contributor / history
Cypher (page and total agree), the create-only flag-marker backfill in the sweep, and the
stated-after-counted scalar_basis clear on an unchanged-value refresh.

A FakeNeo4j cannot evaluate the WHERE/count/SET semantics, so these run online against the TEST
instance (same fixture and marker as test_counting_fence_live.py). Every test uses a fresh
namespace and removes everything it created.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_repository import ViewRepository


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"crf-{uuidlib.uuid4().hex[:10]}"
    yield test_neo4j_repo, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace STARTS WITH $ns OR n.group_id STARTS WITH $ns "
        "OR n.namespace_key STARTS WITH $ns DETACH DELETE n", {"ns": ns})
    test_neo4j_repo.execute(
        "MATCH (h:TypedAssertionHead) WHERE h.head_key STARTS WITH $ns DETACH DELETE h", {"ns": ns})


# --------------------------------------------------------------------------- 7. contributors / history

def _seed_view_graph(repo, ns):
    """A View with three provenance targets: a CURRENT_ANCHOR absolute, a CONTRIBUTED_TO delta and
    a CONTRIBUTED_TO occurrence; plus the same three as HISTORY_ENTRY rows (ordinals 0..2)."""
    view = f"{ns}-view"
    repo.execute(
        """
        CREATE (v:Entity {uuid: $view, group_id: $ns, name: 'v'})
        CREATE (abs:TypedAssertion {assertion_id: $ns + ':abs', operation: 'absolute', namespace: $ns,
                                    value: 5, valid_at: datetime('2026-07-01T00:00:00Z')})
        CREATE (dl:TypedAssertion {assertion_id: $ns + ':delta', operation: 'delta', namespace: $ns,
                                   value: 1, valid_at: datetime('2026-07-02T00:00:00Z')})
        CREATE (oc:TypedAssertion {assertion_id: $ns + ':occ', operation: 'occurrence', namespace: $ns,
                                   value: 1, valid_at: datetime('2026-07-03T00:00:00Z')})
        CREATE (v)-[:CURRENT_ANCHOR]->(abs)
        CREATE (v)-[:CONTRIBUTED_TO]->(dl)
        CREATE (v)-[:CONTRIBUTED_TO]->(oc)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 0, operation: 'absolute'}]->(abs)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 1, operation: 'delta'}]->(dl)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 2, operation: 'occurrence'}]->(oc)
        """,
        {"view": view, "ns": ns},
    )
    return view


def _ids(entries, ns):
    return [e["assertion_id"].split(":", 1)[1] for e in entries]


@pytest.mark.online
def test_contributors_omit_occurrences_and_total_agrees_when_excluded(env):
    # Fails if the include_occurrences predicate is dropped from either the page or the count query
    # (page would list the occurrence, or total would still say 3 for a 2-row page).
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    views = ViewRepository(repo)

    off = views.fetch_scalar_authority_contributors(view_uuid=view, namespace=ns,
                                                    include_occurrences=False)
    assert _ids(off["contributors"], ns) == ["abs", "delta"]
    assert off["total"] == 2 and off["next_offset"] is None

    on = views.fetch_scalar_authority_contributors(view_uuid=view, namespace=ns,
                                                   include_occurrences=True)
    assert sorted(_ids(on["contributors"], ns)) == ["abs", "delta", "occ"]
    assert on["total"] == 3

    # paging stays consistent with the filtered total
    p1 = views.fetch_scalar_authority_contributors(view_uuid=view, namespace=ns, limit=1,
                                                   include_occurrences=False)
    assert p1["total"] == 2 and p1["next_offset"] == 1


@pytest.mark.online
def test_history_entries_omit_occurrences_and_total_agrees_when_excluded(env):
    # Fails if the predicate on he.operation is dropped from the page or the count query.
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    views = ViewRepository(repo)

    off = views.list_scalar_history_entries(view_uuid=view, namespace=ns, include_occurrences=False)
    assert _ids(off["entries"], ns) == ["abs", "delta"]
    assert off["total"] == 2 and off["next_offset"] is None

    on = views.list_scalar_history_entries(view_uuid=view, namespace=ns, include_occurrences=True)
    assert _ids(on["entries"], ns) == ["abs", "delta", "occ"]
    assert on["total"] == 3


@pytest.mark.online
@pytest.mark.parametrize("counting_enabled,expected", [(False, ["abs", "delta"]),
                                                       (True, ["abs", "delta", "occ"])])
def test_adapter_default_applies_the_kill_switch_end_to_end(env, counting_enabled, expected):
    # Fails if the adapter stops defaulting include_occurrences from counting_enabled.
    repo, ns = env
    view = _seed_view_graph(repo, ns)
    adapter = MemoryGraphAdapter(neo4j=repo, counting_enabled=counting_enabled)
    hist = adapter.list_scalar_history_entries(view_uuid=view, namespace=ns)
    assert _ids(hist["entries"], ns) == expected and hist["total"] == len(expected)
    contrib = adapter.fetch_scalar_authority_contributors(view_uuid=view, namespace=ns)
    assert sorted(_ids(contrib["contributors"], ns)) == sorted(expected)


# --------------------------------------------------------------------------- 8. P5 flag-marker backfill

def _seed_head(repo, ns, name, *, operation="occurrence", ordinal=1, current=True):
    repo.execute(
        """
        CREATE (a:TypedAssertion {assertion_id: $aid, namespace: $ns, operation: $op,
                                  claim_ordinal: $ord})
        CREATE (h:TypedAssertionHead {head_key: $aid})
        FOREACH (_ IN CASE WHEN $current THEN [1] ELSE [] END | CREATE (h)-[:CURRENT]->(a))
        """,
        {"aid": f"{ns}:{name}", "ns": ns, "op": operation, "ord": ordinal, "current": current},
    )


def _markers(repo, namespaces):
    rows = repo.execute(
        "MATCH (m:CountingFlagState) WHERE m.namespace_key IN $ns "
        "RETURN m.namespace_key AS ns, m.enabled AS enabled", {"ns": namespaces})
    return {r["ns"]: r["enabled"] for r in rows}


@pytest.mark.online
def test_sweep_targets_creates_missing_marker_for_current_occurrences_and_never_overwrites(env):
    # Fails if the backfill is removed (no marker -> the off-sweep can never find a legacy counted
    # namespace), if it overwrites an existing marker (a failed re-enable sweep's False flipped to
    # True), or if it matches non-current / non-counting-lane / non-occurrence assertions.
    repo, ns = env
    legacy, marked = f"{ns}-legacy", f"{ns}-marked"
    not_cur, other_lane, plain = f"{ns}-notcur", f"{ns}-lane0", f"{ns}-plain"
    _seed_head(repo, legacy, "o")
    _seed_head(repo, marked, "o")
    _seed_head(repo, not_cur, "o", current=False)
    _seed_head(repo, other_lane, "o", ordinal=0)
    _seed_head(repo, plain, "a", operation="absolute", ordinal=1)
    repo.execute(
        "CREATE (:CountingFlagState {namespace_key: $ns, group_id: $ns, enabled: false})",
        {"ns": marked})
    adapter = MemoryGraphAdapter(neo4j=repo)
    scope = [legacy, marked, not_cur, other_lane, plain]

    targets = adapter.counting_flag_sweep_targets(enabled=False, namespaces=scope, limit=100)

    markers = _markers(repo, scope)
    assert markers == {legacy: True, marked: False}          # created create-only, nothing else
    assert targets == [legacy]                                # enabled=True marker needs an off-sweep

    again = adapter.counting_flag_sweep_targets(enabled=False, namespaces=scope, limit=100)
    assert again == [legacy] and _markers(repo, scope) == {legacy: True, marked: False}


# --------------------------------------------------------------------------- 9. P7 live basis clear

@pytest.mark.online
def test_unchanged_value_refresh_clears_a_stale_counted_basis(env):
    # Fails if a stated rebuild no longer sends scalar_basis=None, or the refresh path stops
    # removing the property: the View would keep scalar_basis='counted' and stay hidden/labelled
    # as counted after counting is turned off.
    repo, ns = env
    views = ViewRepository(repo)
    subj = f"{ns}-subj"
    repo.execute("MERGE (n:Entity {uuid: $u}) SET n.name = $u, n.group_id = $ns", {"u": subj, "ns": ns})
    slot = dict(subject="me", subject_uuid=subj, attribute="yoga_count", scope="", value_kind="count",
                unit="", value=3, namespace=ns, valid_at="2026-07-01T00:00:00+00:00")

    def basis():
        rows = repo.execute(
            "MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
            "WHERE coalesce(n.view_current, true) RETURN n.scalar_basis AS b, n.uuid AS uuid",
            {"u": subj})
        assert len(rows) == 1
        return rows[0]["b"], rows[0]["uuid"]

    views.record_scalar_state(**slot, audit={"scalar_contributors": ["o1"], "scalar_basis": "counted"})
    b1, uuid1 = basis()
    assert b1 == "counted"

    views.record_scalar_state(**slot, audit={"scalar_contributors": ["a1"], "scalar_basis": None})
    b2, uuid2 = basis()
    assert uuid2 == uuid1          # same value -> refreshed in place, not a new version
    assert b2 is None
