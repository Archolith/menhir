"""Menhir never writes a RELATES_TO link, or a conflict group, across tenants (online, real Neo4j).

Tenant is ``canonical_tenant_cypher``: ``coalesce(namespace, group_id, '')`` with ``''`` and
``'default'`` the same tenant. A node scoped only by ``group_id`` (no ``namespace`` property) is in
that group's tenant. The production incident: an UNSCOPED ``resolve_conflict`` bridged 135
cross-tenant pairs, because ``bridge_edges_for_nodes(namespace=None)`` applied no tenant predicate.

Every test is a counterexample: it seeds a node in a second tenant next to the pair that is allowed
to link, and asserts the link to the foreign node was NOT written while the same-tenant link WAS
(so a fix that simply writes nothing also fails).

Each test uses its own uuid prefix and deletes what it created.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.cypher import memory_recency_cypher
from menhir.infrastructure.memory_queries import MemoryQueryRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    """The stood-up TEST instance (conftest.test_neo4j_repo), never the operator's real graph."""
    return test_neo4j_repo


@pytest.fixture
def tag(live_repo):
    """A per-test unique prefix; every node a test creates carries it as ``test_tag``."""
    value = f"test-same-tenant-{uuidlib.uuid4().hex[:12]}"
    yield value
    live_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": value})


def _seed(repo, tag: str, nodes: dict[str, dict]) -> dict[str, str]:
    """Create one :Entity per key; returns key -> uuid. ``props`` are written verbatim."""
    ids: dict[str, str] = {}
    for key, props in nodes.items():
        ids[key] = f"{tag}-{key}"
        repo.execute(
            "CREATE (n:Entity) SET n = $p",
            params={"p": {"uuid": ids[key], "name": key, "test_tag": tag, **props}},
        )
    return ids


def _link(repo, src: str, dst: str, **props) -> None:
    repo.execute(
        "MATCH (a:Entity {uuid:$a}), (b:Entity {uuid:$b}) CREATE (a)-[:RELATES_TO $p]->(b)",
        params={"a": src, "b": dst, "p": props},
    )


def _decay_gate(repo, uuid: str) -> dict:
    """Bounds plus the live node's own values as the decay read, so only the node's state decides."""
    row = repo.execute(
        f"""
        MATCH (n:Entity {{uuid:$u}})
        OPTIONAL MATCH (n)-[r]-(p)
        RETURN coalesce(n.type, 'SEMANTIC') AS read_type,
               coalesce(toInteger(n.edge_count), 0) AS read_edge_count,
               toFloat(n.sharpness) AS read_sharpness,
               toString({memory_recency_cypher("n")}) AS read_recency,
               count(r) AS read_rel_count,
               collect(CASE WHEN r IS NULL THEN NULL ELSE type(r) + '|'
                   + CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END + '|'
                   + coalesce(p.uuid, p.name, elementId(p)) END) AS sigs
        """,
        params={"u": uuid},
    )[0]
    return {
        "min_days": 90.0, "max_edge_count": 3, "max_sharpness": 0.1,
        "read_type": row["read_type"], "read_edge_count": row["read_edge_count"],
        "read_sharpness": row["read_sharpness"], "read_recency": row["read_recency"],
        "requires_target_date": False, "read_rel_count": row["read_rel_count"],
        "read_rel_signatures": sorted(set(row["sigs"])),
    }


def _bridged_pairs(repo, origin_uuid: str) -> set[frozenset[str]]:
    rows = repo.execute(
        """
        MATCH (a:Entity)-[r:RELATES_TO {bridged_from: $o}]->(b:Entity)
        RETURN a.uuid AS a, b.uuid AS b
        """,
        params={"o": origin_uuid},
    )
    return {frozenset((r["a"], r["b"])) for r in rows}


def _edge_count_between(repo, a: str, b: str) -> int:
    return repo.execute(
        """
        MATCH (:Entity {uuid:$a})-[r:RELATES_TO]-(:Entity {uuid:$b})
        RETURN count(r) AS c
        """,
        params={"a": a, "b": b},
    )[0]["c"]


# ---------------------------------------------------------------------------
# 1. create_related_to_edge
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_create_related_to_edge_refuses_default_to_archolith(live_repo, tag) -> None:
    """Counterexample: a node in the legacy default silo (group_id '', no namespace) paired with
    an 'archolith' node. Fails if the same-tenant predicate is removed from the MERGE query."""
    ids = _seed(live_repo, tag, {
        "dflt": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "arch": {"namespace": "archolith", "group_id": "archolith",
                 "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    repo = CorrelationRepository(live_repo)

    assert repo.create_related_to_edge(ids["dflt"], ids["arch"], similarity=0.9) is False
    assert repo.create_related_to_edge(ids["arch"], ids["dflt"], similarity=0.9) is False

    assert _edge_count_between(live_repo, ids["dflt"], ids["arch"]) == 0


@pytest.mark.online
def test_create_related_to_edge_treats_empty_group_and_default_namespace_as_one_tenant(
    live_repo, tag,
) -> None:
    """'' and 'default' are the same tenant: namespace='default' vs group_id='' must still link.
    Fails if the predicate compares raw coalesce(namespace, group_id) without canonicalising
    ('' <> 'default'), or if the method refuses every pair."""
    ids = _seed(live_repo, tag, {
        "ns_default": {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "grp_empty": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    repo = CorrelationRepository(live_repo)

    assert repo.create_related_to_edge(ids["ns_default"], ids["grp_empty"], similarity=0.9) is True

    assert _edge_count_between(live_repo, ids["ns_default"], ids["grp_empty"]) == 1


# ---------------------------------------------------------------------------
# 2. bridge_edges_for_nodes, unscoped (the production incident)
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_unscoped_bridge_edges_for_nodes_does_not_bridge_across_tenants(live_repo, tag) -> None:
    """GONE node n (default) with default neighbours d1, d2 and an 'archolith' neighbour x
    (group_id only, no namespace), called with namespace=None. Only d1-d2 may be bridged.
    Fails on the pre-fix query, which bridged d1-x and d2-x as well (3 pairs, not 1)."""
    ids = _seed(live_repo, tag, {
        "n": {"group_id": "", "freshness": "GONE", "scope": "PERSISTENT"},
        "d1": {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "d2": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "x": {"group_id": "archolith", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    for k in ("d1", "d2", "x"):
        _link(live_repo, ids["n"], ids[k], weight=0.5)

    bridged = ConsolidationRepository(live_repo).bridge_edges_for_nodes([ids["n"]], namespace=None)

    assert _bridged_pairs(live_repo, ids["n"]) == {frozenset((ids["d1"], ids["d2"]))}
    assert bridged == 1
    # No edge other than the seeded n-x one touches x.
    assert _edge_count_between(live_repo, ids["d1"], ids["x"]) == 0
    assert _edge_count_between(live_repo, ids["d2"], ids["x"]) == 0


# ---------------------------------------------------------------------------
# 3. bridge_and_delete
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_bridge_and_delete_bridges_only_same_tenant_neighbours(live_repo, tag) -> None:
    """Same shape as above through the decay path. n is a plain PERSISTENT, unflagged, non-View
    node with no source episodes, so harmful_automatic_mutation_allowed_cypher admits it.
    Fails on the pre-fix query (bridged d1-x and d2-x too, reporting 3)."""
    ids = _seed(live_repo, tag, {
        "n": {"namespace": "default", "scope": "PERSISTENT", "freshness": "COMPRESSED",
              "edge_count": 0, "sharpness": 0.05,
              "last_accessed": "2024-01-01T00:00:00Z", "created_at": "2024-01-01T00:00:00Z"},
        "d1": {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "d2": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "x": {"group_id": "archolith", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    for k in ("d1", "d2", "x"):
        _link(live_repo, ids["n"], ids[k], weight=0.5)

    result = ConsolidationRepository(live_repo).bridge_and_delete(
        ids["n"], **_decay_gate(live_repo, ids["n"]))

    assert (result["edges_bridged"], result["deleted"]) == (1, 1)
    assert len(result["bridge_uuids"]) == 1 and "reason" not in result
    assert _bridged_pairs(live_repo, ids["n"]) == {frozenset((ids["d1"], ids["d2"]))}
    assert _edge_count_between(live_repo, ids["d1"], ids["x"]) == 0
    assert _edge_count_between(live_repo, ids["d2"], ids["x"]) == 0
    gone = live_repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN count(n) AS c", params={"u": ids["n"]}
    )[0]["c"]
    assert gone == 0


# ---------------------------------------------------------------------------
# 4. merge_entity bridge
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_merge_does_not_bridge_survivor_to_a_foreign_tenant_neighbour(live_repo, tag) -> None:
    """Survivor and absorbed are both 'alpha' (eligible: ACTIVE, PERSISTENT, unflagged, same
    namespace). The absorbed node has a plain RELATES_TO to an alpha node and one to a beta node.
    After the merge the survivor has a bridge to the alpha neighbour and none to the beta one.
    Fails on the pre-fix Phase-2 bridge, which bridged every neighbour regardless of tenant."""
    common = {"namespace": "alpha", "scope": "PERSISTENT", "freshness": "ACTIVE",
              "source": "claude-code", "source_confidence": 0.6}
    ids = _seed(live_repo, tag, {
        "surv": {**common, "summary": "survivor summary", "content": "survivor content"},
        "abs": {**common,
                "summary": "a much longer absorbed summary that is clearly richer than the survivor",
                "content": "a much longer absorbed content body clearly exceeding the survivor "
                           "by well over twenty percent"},
        "nb_alpha": {"namespace": "alpha", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "nb_beta": {"namespace": "beta", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    _link(live_repo, ids["abs"], ids["nb_alpha"], weight=0.4)
    _link(live_repo, ids["abs"], ids["nb_beta"], weight=0.4)

    result = CorrelationRepository(live_repo).merge_entity(
        ids["surv"], ids["abs"], similarity=0.97,
    )

    assert result["merged"] == 1, result  # the merge really ran; otherwise nothing below proves much
    assert _edge_count_between(live_repo, ids["surv"], ids["nb_alpha"]) == 1
    assert _edge_count_between(live_repo, ids["surv"], ids["nb_beta"]) == 0
    assert _bridged_pairs(live_repo, ids["abs"]) == {frozenset((ids["surv"], ids["nb_alpha"]))}
    assert result["edges_bridged"] == 1


# ---------------------------------------------------------------------------
# 5. set_conflict
# ---------------------------------------------------------------------------

def _conflict_group(repo, uuid: str):
    return repo.execute(
        "MATCH (n:Entity {uuid:$u}) RETURN n.conflict_group_id AS g", params={"u": uuid}
    )[0]["g"]


@pytest.mark.online
def test_set_conflict_refuses_a_cross_tenant_pair(live_repo, tag) -> None:
    """default vs 'archolith' -> (new_group_id, 0) and neither node is stamped. Fails if the
    same-tenant predicate is dropped from set_conflict (it would return 2 and stamp both)."""
    ids = _seed(live_repo, tag, {
        "a": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "b": {"group_id": "archolith", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    grp = f"{tag}-grp"

    result = ConsolidationRepository(live_repo).set_conflict(ids["a"], ids["b"], grp)

    assert result == (grp, 0)
    assert _conflict_group(live_repo, ids["a"]) is None
    assert _conflict_group(live_repo, ids["b"]) is None


@pytest.mark.online
def test_set_conflict_still_groups_a_same_tenant_pair(live_repo, tag) -> None:
    """namespace='default' vs group_id='' is one tenant, so the pair is grouped. Fails if the
    guard over-refuses (e.g. compares raw '' to 'default')."""
    ids = _seed(live_repo, tag, {
        "a": {"namespace": "default", "scope": "PERSISTENT", "freshness": "ACTIVE"},
        "b": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE"},
    })
    grp = f"{tag}-grp"

    result = ConsolidationRepository(live_repo).set_conflict(ids["a"], ids["b"], grp)

    assert result == (grp, 2)
    assert _conflict_group(live_repo, ids["a"]) == grp
    assert _conflict_group(live_repo, ids["b"]) == grp


# ---------------------------------------------------------------------------
# 6. candidate queries report the canonical tenant
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_decay_candidates_report_group_id_tenant(live_repo, tag) -> None:
    """A group_id-only node reports 'archolith', not the old coalesce(namespace,'default') =
    'default'. A legacy '' node reports 'default'. Fails on the old projection."""
    ids = _seed(live_repo, tag, {
        "arch": {"group_id": "archolith", "scope": "PERSISTENT", "freshness": "ACTIVE",
                 "edge_count": 0},
        "legacy": {"group_id": "", "scope": "PERSISTENT", "freshness": "ACTIVE", "edge_count": 0},
    })
    live_repo.execute(
        "MATCH (n:Entity) WHERE n.test_tag = $t SET n.last_accessed = datetime('2020-01-01T00:00:00Z')",
        params={"t": tag},
    )

    rows = ConsolidationRepository(live_repo).fetch_decay_candidates(
        "ACTIVE", min_days_since_accessed=1, max_edge_count=5,
    )
    by_uuid = {r["uuid"]: r["namespace"] for r in rows if r["uuid"] in ids.values()}

    assert by_uuid == {ids["arch"]: "archolith", ids["legacy"]: "default"}


@pytest.mark.online
def test_session_entities_report_group_id_tenant(live_repo, tag) -> None:
    """Same counterexample through fetch_session_entities (SESSION scope)."""
    ids = _seed(live_repo, tag, {
        "arch": {"group_id": "archolith", "scope": "SESSION"},
        "legacy": {"group_id": "", "scope": "SESSION"},
    })

    rows = ConsolidationRepository(live_repo).fetch_session_entities()
    by_uuid = {r["uuid"]: r["namespace"] for r in rows if r["uuid"] in ids.values()}

    assert by_uuid == {ids["arch"]: "archolith", ids["legacy"]: "default"}


# ---------------------------------------------------------------------------
# 7. fetch_adjacency_pairs tenant scope
# ---------------------------------------------------------------------------

def _pair_sets(rows) -> set[frozenset[str]]:
    return {frozenset((r["source"], r["target"])) for r in rows}


@pytest.mark.online
def test_adjacency_pairs_match_group_id_only_nodes(live_repo, tag) -> None:
    """namespace='archolith' must return an edge between two nodes scoped only by group_id (no
    namespace property). Fails on the old coalesce(a.namespace, 'default') = $namespace, which
    read those nodes as 'default' and returned nothing."""
    ids = _seed(live_repo, tag, {
        "a": {"group_id": "archolith"},
        "b": {"group_id": "archolith"},
    })
    _link(live_repo, ids["a"], ids["b"], weight=0.5)

    rows = MemoryQueryRepository(live_repo).fetch_adjacency_pairs(
        [ids["a"], ids["b"]], namespace="archolith",
    )

    assert _pair_sets(rows) == {frozenset((ids["a"], ids["b"]))}


@pytest.mark.online
def test_adjacency_pairs_default_silo_matches_every_spelling_and_excludes_other_tenants(
    live_repo, tag,
) -> None:
    """namespace='default' returns the edge between a '' group_id node and a namespace='default'
    node, and also one involving a legacy namespace='' node (which the old equality against
    'default' missed). namespace='archolith' must not return any of the default pairs. Fails if
    the default silo is matched on a single spelling, or if the scope is not applied."""
    ids = _seed(live_repo, tag, {
        "grp_empty": {"group_id": ""},
        "ns_default": {"namespace": "default"},
        "ns_empty": {"namespace": ""},
    })
    _link(live_repo, ids["grp_empty"], ids["ns_default"], weight=0.5)
    _link(live_repo, ids["grp_empty"], ids["ns_empty"], weight=0.5)
    uuids = list(ids.values())
    repo = MemoryQueryRepository(live_repo)

    default_rows = repo.fetch_adjacency_pairs(uuids, namespace="default")
    assert _pair_sets(default_rows) == {
        frozenset((ids["grp_empty"], ids["ns_default"])),
        frozenset((ids["grp_empty"], ids["ns_empty"])),
    }

    assert repo.fetch_adjacency_pairs(uuids, namespace="archolith") == []
