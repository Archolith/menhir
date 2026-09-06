"""End-to-end coverage for the mutation-time merge-eligibility gate (remediation plan Phase 3).

merge_entity now rechecks eligibility on the CURRENT graph immediately before mutating (invariant 7)
and repeats the mutable predicates inside the mutation itself. These tests prove, against real Neo4j,
that an ineligible pair ABSTAINS with the right reason code AND that the absorbed node is left fully
intact -- no partial merge. A positive control confirms an eligible pair still merges.

Option A semantics (owner-approved 2026-07-13): unstamped freshness is treated as ACTIVE and SESSION
scope is allowed, so the gate is pure tightening over the prior always-merge behavior.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain import merge_eligibility as me
from menhir.infrastructure.correlation_queries import CorrelationRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    """The stood-up TEST instance (conftest.test_neo4j_repo), never the operator's real graph."""
    return test_neo4j_repo


@pytest.fixture
def make_pair(live_repo):
    """Factory: create a survivor+absorbed :Entity pair with per-node property overrides.

    Returns (survivor_uuid, absorbed_uuid). All fixture nodes carry a shared test_tag so the
    teardown can purge them regardless of whether a merge occurred.
    """
    tag = f"test-merge-elig-{uuidlib.uuid4()}"

    def _factory(*, survivor_props: dict | None = None, absorbed_props: dict | None = None) -> tuple[str, str]:
        s_uuid = f"{tag}-survivor"
        a_uuid = f"{tag}-absorbed"
        live_repo.execute(
            "CREATE (s:Entity) SET s = $sp CREATE (a:Entity) SET a = $ap",
            params={
                "sp": {"uuid": s_uuid, "name": "survivor node", "test_tag": tag,
                       "namespace": "default", **(survivor_props or {})},
                "ap": {"uuid": a_uuid, "name": "absorbed node", "test_tag": tag,
                       "namespace": "default", **(absorbed_props or {})},
            },
        )
        return s_uuid, a_uuid

    yield _factory
    live_repo.execute("MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag})


def _absorbed_exists(live_repo, absorbed_uuid: str) -> bool:
    rows = live_repo.execute(
        "MATCH (n:Entity {uuid: $u}) RETURN count(n) AS c", params={"u": absorbed_uuid}
    )
    return int(rows[0]["c"]) > 0


# --------------------------------------------------------------------------- positive control

@pytest.mark.online
def test_eligible_pair_merges(live_repo, make_pair):
    s, a = make_pair(
        survivor_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
        absorbed_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
    )
    repo = CorrelationRepository(live_repo)
    result = repo.merge_entity(s, a, similarity=0.97)
    assert result["merged"] == 1
    assert not _absorbed_exists(live_repo, a), "an eligible merge must absorb (delete) the node"


# --------------------------------------------------------------------------- abstain matrix

@pytest.mark.online
@pytest.mark.parametrize(
    "survivor_props, absorbed_props, expected_reason",
    [
        # COMPRESSED / GONE must rehydrate first (invariant 8).
        ({}, {"freshness": "COMPRESSED"}, me.NON_ACTIVE_FRESHNESS),
        ({"freshness": "GONE"}, {}, me.NON_ACTIVE_FRESHNESS),
        # PROMOTED is operator-curated and merge-immune.
        ({"scope": "PROMOTED"}, {}, me.PROMOTED_SCOPE),
        # User-protected node.
        ({}, {"user_flagged": True}, me.USER_FLAGGED),
        # Under active conflict review.
        ({}, {"conflict_status": "unresolved"}, me.PROTECTED_CONFLICT),
        # Cross-namespace identities never merge.
        ({"namespace": "proj-a"}, {"namespace": "proj-b"}, me.NAMESPACE_MISMATCH),
        # Path-shaped name -> ineligible role.
        ({}, {"name": "src/menhir/foo.py"}, me.INELIGIBLE_ROLE),
        # Canonical self is an endpoint authority and must never be absorbed or used as survivor.
        ({"is_self": True, "entity_role": "self"}, {}, me.INELIGIBLE_ROLE),
        ({}, {"is_self": True, "entity_role": "self"}, me.INELIGIBLE_ROLE),
        # Either marker independently protects legacy or partially stamped canonical nodes.
        ({"entity_role": "SELF"}, {}, me.INELIGIBLE_ROLE),
        ({}, {"is_self": True}, me.INELIGIBLE_ROLE),
    ],
)
def test_ineligible_pair_abstains_and_preserves_node(
    live_repo, make_pair, survivor_props, absorbed_props, expected_reason
):
    s, a = make_pair(survivor_props=survivor_props, absorbed_props=absorbed_props)
    repo = CorrelationRepository(live_repo)

    result = repo.merge_entity(s, a, similarity=0.99)

    assert result["merged"] == 0
    assert result.get("reason") == expected_reason
    # The load-bearing safety property: an abstention never mutates. The absorbed node survives.
    assert _absorbed_exists(live_repo, a), "abstain must not delete the absorbed node"


@pytest.mark.online
def test_missing_node_abstains(live_repo, make_pair):
    s, _a = make_pair()
    repo = CorrelationRepository(live_repo)
    # Absorbed uuid that was never created -> stale discovery.
    result = repo.merge_entity(s, "does-not-exist-uuid", similarity=0.99)
    assert result["merged"] == 0
    assert result.get("reason") == me.NODE_MISSING


# --------------------------------------------------------------------------- CF-149 / CF-159
# One predicate, two call sites. The eligibility gate and the standalone veto had drifted: the
# veto hand-wrote its own copy and had lost `is_view`, and NEITHER copy carried `is_quantstate`,
# so a pre-View counter register was merge-eligible at the mutation boundary.


@pytest.mark.online
@pytest.mark.parametrize(
    "derived_props",
    [
        pytest.param({"is_quantstate": True}, id="legacy-counter-register"),
        pytest.param({"is_view": True}, id="derived-view"),
        pytest.param({"view_kind": "counter"}, id="kinded-view"),
    ],
)
def test_derived_nodes_are_ineligible_at_both_call_sites(live_repo, make_pair, derived_props):
    s, a = make_pair(
        survivor_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
        absorbed_props={"freshness": "ACTIVE", "scope": "PERSISTENT", **derived_props},
    )
    repo = CorrelationRepository(live_repo)

    # the standalone veto (classification path)
    assert repo.check_ineligible_node_veto(s, a) is True
    # the mutation-boundary gate -- the one that actually guards DETACH DELETE
    assert repo.evaluate_merge_eligibility(s, a).allowed is False


@pytest.mark.online
@pytest.mark.parametrize(
    "canonical_props",
    [
        pytest.param({"is_self": True}, id="is-self-marker"),
        pytest.param({"entity_role": "self"}, id="entity-role-marker"),
    ],
)
def test_canonical_self_is_ineligible_at_both_call_sites(
    live_repo, make_pair, canonical_props
):
    s, a = make_pair(
        survivor_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
        absorbed_props={
            "freshness": "ACTIVE", "scope": "PERSISTENT", **canonical_props,
        },
    )
    repo = CorrelationRepository(live_repo)

    assert repo.check_ineligible_node_veto(s, a) is True
    decision = repo.evaluate_merge_eligibility(s, a)
    assert decision.allowed is False
    assert decision.reason_code == me.INELIGIBLE_ROLE


@pytest.mark.online
def test_ordinary_pair_is_still_eligible_at_both_call_sites(live_repo, make_pair):
    """Positive control for the test above. Without it, a predicate that vetoed EVERYTHING would
    satisfy every assertion there while breaking merge entirely."""
    s, a = make_pair(
        survivor_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
        absorbed_props={"freshness": "ACTIVE", "scope": "PERSISTENT"},
    )
    repo = CorrelationRepository(live_repo)

    assert repo.check_ineligible_node_veto(s, a) is False
    assert repo.evaluate_merge_eligibility(s, a).allowed is True
