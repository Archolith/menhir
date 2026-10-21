"""Two-tier self identity, live half: the graph-side protections for the inferred tier.

Against a real Neo4j (the stood-up TEST instance behind ``conftest.test_neo4j_repo``; run with
``--run-online`` and ``MENHIR_TEST_NEO4J_URI``), prove that

* the anchored self, the inferred self and an ordinary node can never be merged in any direction,
  at BOTH the preflight classifier and the mutation-time precondition that guards DETACH DELETE;
* fork detection never reports the inferred node as a ``user`` fork to consolidate;
* the structural ``INFERRED_VIEW_OF`` link is created only when both tiers exist, and only once.

Nodes are created directly with Cypher; every fixture node carries a ``test_tag`` so teardown can
purge it independently of the fixture's own wipe of the scratch instance.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_eligibility as me
from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import (
    inferred_self_uuid_for_namespace,
    self_uuid_for_namespace,
)
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    """The stood-up TEST instance (conftest.test_neo4j_repo), never the operator's real graph."""
    return test_neo4j_repo


@pytest.fixture
def make_node(live_repo):
    """Factory for an :Entity with a per-test tag, purged on teardown."""
    tag = f"test-two-tier-{uuidlib.uuid4()}"
    counter = {"n": 0}

    def _factory(namespace: str = "default", *, uuid: str | None = None, **props) -> str:
        counter["n"] += 1
        node_uuid = uuid or f"{tag}-{counter['n']}"
        live_repo.execute(
            "CREATE (n:Entity) SET n = $props",
            params={
                "props": {
                    "uuid": node_uuid,
                    "test_tag": tag,
                    "namespace": namespace,
                    "group_id": namespace_to_group_id(namespace),
                    "freshness": "ACTIVE",
                    "scope": "PERSISTENT",
                    **props,
                }
            },
        )
        return node_uuid

    yield _factory
    live_repo.execute("MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag})


def _exists(live_repo, node_uuid: str) -> bool:
    rows = live_repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN count(n) AS c", params={"u": node_uuid}
    )
    return int(rows[0]["c"]) > 0


def _mutation_precondition_matches(live_repo, survivor: str, absorbed: str) -> int:
    """Row count of the mutation-time WHERE, exactly as ``merge_entity`` emits it."""
    rows = live_repo.execute(
        "MATCH (survivor:Entity {uuid: $s}) MATCH (absorbed:Entity {uuid: $a}) WHERE "
        + me.mutable_eligibility_cypher()
        + " RETURN count(*) AS c",
        params={**me.MUTABLE_PREDICATE_PARAMS, "s": survivor, "a": absorbed},
    )
    return int(rows[0]["c"])


_ANCHORED = {"name": "user", "is_self": True, "entity_role": "self"}
_INFERRED_FULL = {
    "name": "user-inferred", "entity_role": "inferred_self", "self_tier": "inferred",
}
_ORDINARY = {"name": "plain node"}


# --------------------------------------------------------------------------- L1. merge refusal


@pytest.mark.online
@pytest.mark.parametrize(
    "inferred_props",
    [
        pytest.param(_INFERRED_FULL, id="role-and-tier"),
        pytest.param({"name": "user-inferred", "entity_role": "inferred_self"}, id="role-only"),
        pytest.param({"name": "user-inferred", "self_tier": "inferred"}, id="tier-only"),
    ],
)
@pytest.mark.parametrize(
    "other_props, other_id",
    [(_ANCHORED, "anchored"), (_ORDINARY, "ordinary")],
)
@pytest.mark.parametrize("inferred_is_survivor", [True, False], ids=["inferred-survives", "inferred-absorbed"])
def test_inferred_node_never_merges_in_either_direction(
    live_repo, make_node, inferred_props, other_props, other_id, inferred_is_survivor
):
    inferred = make_node(**inferred_props)
    other = make_node(**other_props)
    survivor, absorbed = (inferred, other) if inferred_is_survivor else (other, inferred)
    repo = CorrelationRepository(live_repo)

    # preflight classifier
    decision = repo.evaluate_merge_eligibility(survivor, absorbed)
    assert decision.allowed is False
    assert decision.reason_code == me.INELIGIBLE_ROLE
    assert repo.check_ineligible_node_veto(survivor, absorbed) is True

    # the precondition inside the mutation matches nothing, so DETACH DELETE is never reached
    assert _mutation_precondition_matches(live_repo, survivor, absorbed) == 0

    # and the real mutation abstains without touching either node
    result = repo.merge_entity(survivor, absorbed, similarity=0.99)
    assert result["merged"] == 0
    assert _exists(live_repo, survivor) and _exists(live_repo, absorbed)


@pytest.mark.online
def test_two_ordinary_nodes_still_merge_so_the_refusals_above_are_not_vacuous(live_repo, make_node):
    """Positive control: a predicate that vetoed everything would satisfy every test above."""
    survivor = make_node(name="plain a")
    absorbed = make_node(name="plain b")
    repo = CorrelationRepository(live_repo)

    assert repo.evaluate_merge_eligibility(survivor, absorbed).allowed is True
    assert _mutation_precondition_matches(live_repo, survivor, absorbed) == 1


@pytest.mark.online
def test_two_inferred_nodes_are_also_refused(live_repo, make_node):
    """Two inferred-tier nodes (a corrupted duplicate) are not collapsed automatically either."""
    a = make_node(**_INFERRED_FULL)
    b = make_node(**_INFERRED_FULL)
    repo = CorrelationRepository(live_repo)

    assert repo.evaluate_merge_eligibility(a, b).reason_code == me.INELIGIBLE_ROLE
    assert _mutation_precondition_matches(live_repo, a, b) == 0


# --------------------------------------------------------------------------- L2. fork detection


@pytest.mark.online
def test_detect_self_forks_ignores_the_inferred_node_but_still_reports_an_ordinary_user(
    live_repo, make_node
):
    namespace = f"ns-two-tier-{uuidlib.uuid4().hex[:8]}"
    anchored_uuid = self_uuid_for_namespace(namespace)
    ordinary_fork = make_node(namespace, name="user")
    inferred_both = make_node(namespace, name="user", entity_role="inferred_self", self_tier="inferred")
    inferred_role = make_node(namespace, name="user", entity_role="inferred_self")
    inferred_tier = make_node(namespace, name="user", self_tier="inferred")
    # other namespace: never reported for this one
    make_node(f"{namespace}-other", name="user")
    repo = EpisodeLifecycleRepository()
    repo.neo4j = live_repo

    forks = repo.detect_self_forks(namespace=namespace, self_uuid=anchored_uuid)

    assert ordinary_fork in forks, "an ordinary 'user' duplicate must still be reported"
    for protected in (inferred_both, inferred_role, inferred_tier):
        assert protected not in forks, "the inferred tier is not a fork to consolidate"
    assert forks == [ordinary_fork]


# --------------------------------------------------------------------------- L3. link_self_tiers


def _view_edges(live_repo, namespace: str) -> int:
    rows = live_repo.execute(
        "MATCH (a:Entity {uuid: $a})-[r:INFERRED_VIEW_OF]->(i:Entity {uuid: $i}) "
        "RETURN count(r) AS c",
        params={
            "a": self_uuid_for_namespace(namespace),
            "i": inferred_self_uuid_for_namespace(namespace),
        },
    )
    return int(rows[0]["c"])


def _all_view_edges(live_repo, tag_nodes: list[str]) -> int:
    rows = live_repo.execute(
        "MATCH (n:Entity)-[r:INFERRED_VIEW_OF]-() WHERE n.uuid IN $uuids RETURN count(DISTINCT r) AS c",
        params={"uuids": tag_nodes},
    )
    return int(rows[0]["c"])


@pytest.mark.online
@pytest.mark.parametrize("present", ["anchored", "inferred"])
def test_link_self_tiers_with_one_tier_returns_false_and_creates_nothing(
    live_repo, make_node, present
):
    namespace = f"ns-link-{uuidlib.uuid4().hex[:8]}"
    anchored = self_uuid_for_namespace(namespace)
    inferred = inferred_self_uuid_for_namespace(namespace)
    if present == "anchored":
        make_node(namespace, uuid=anchored, **_ANCHORED)
    else:
        make_node(namespace, uuid=inferred, **_INFERRED_FULL)
    repo = EpisodeLifecycleRepository()
    repo.neo4j = live_repo
    before = live_repo.execute("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]

    assert repo.link_self_tiers(namespace) is False

    assert _all_view_edges(live_repo, [anchored, inferred]) == 0
    # MATCH-only: it never creates the missing tier
    assert live_repo.execute("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"] == before
    assert _exists(live_repo, anchored) is (present == "anchored")
    assert _exists(live_repo, inferred) is (present == "inferred")


@pytest.mark.online
def test_link_self_tiers_creates_exactly_one_edge_and_is_idempotent(live_repo, make_node):
    namespace = f"ns-link-{uuidlib.uuid4().hex[:8]}"
    anchored = make_node(namespace, uuid=self_uuid_for_namespace(namespace), **_ANCHORED)
    inferred = make_node(namespace, uuid=inferred_self_uuid_for_namespace(namespace), **_INFERRED_FULL)
    repo = EpisodeLifecycleRepository()
    repo.neo4j = live_repo

    assert repo.link_self_tiers(namespace) is True
    assert _view_edges(live_repo, namespace) == 1

    # second call: still reports the link exists, creates no duplicate
    assert repo.link_self_tiers(namespace) is True
    assert _view_edges(live_repo, namespace) == 1
    assert _all_view_edges(live_repo, [anchored, inferred]) == 1

    # direction: anchored -> inferred, never the reverse
    rows = live_repo.execute(
        "MATCH (a:Entity)-[:INFERRED_VIEW_OF]->(i:Entity) WHERE a.uuid = $a RETURN i.uuid AS u",
        params={"a": anchored},
    )
    assert [r["u"] for r in rows] == [inferred]


@pytest.mark.online
def test_link_self_tiers_does_not_link_across_physical_groups(live_repo, make_node):
    """A node holding the deterministic uuid in another partition is not this namespace's tier."""
    namespace = f"ns-link-{uuidlib.uuid4().hex[:8]}"
    make_node(namespace, uuid=self_uuid_for_namespace(namespace), **_ANCHORED)
    make_node(
        namespace, uuid=inferred_self_uuid_for_namespace(namespace), **_INFERRED_FULL,
    )
    live_repo.execute(
        "MATCH (i:Entity {uuid: $i}) SET i.group_id = 'foreign-partition'",
        params={"i": inferred_self_uuid_for_namespace(namespace)},
    )
    repo = EpisodeLifecycleRepository()
    repo.neo4j = live_repo

    assert repo.link_self_tiers(namespace) is False
    assert _view_edges(live_repo, namespace) == 0
