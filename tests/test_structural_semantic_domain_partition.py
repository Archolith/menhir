"""The structural/semantic partition, tested where it is ENFORCED rather than where it is declared.

CF-252 (a structural node converted into a memory) and CF-253 (a memory carrying a structural role)
are two directions of one boundary violation: structural graph entities and ordinary memories
participating in the same dedupe/merge domain. The invariant:

    A structural entity and a non-structural memory must never be dedupe/merge candidates for
    one another.

Menhir enforces it in three places, and all three already existed when this file was written:

  1. `_patch_graphiti_structural_candidate_isolation` filters `_collect_candidate_nodes`, the single
     funnel every graphiti dedupe candidate passes through.
  2. `_patch_graphiti_untyped_attribute_preservation` keeps a node's existing properties so
     graphiti's whole-map `SET n = node` rewrites them instead of erasing them.
  3. `CorrelationRepository._INELIGIBLE_ROLE_PREDICATE` vetoes structural participants in menhir's own
     merge.

What was NOT covered is the load-bearing half of each. `test_graphiti_structural_isolation.py`
monkeypatches `_collect_candidate_nodes` and then calls it directly, so it proves the filter filters
-- not that anything routes through it. `test_merge_eligibility.py` hands the policy
`ineligible_role=True` and checks it vetoes, so it proves the policy -- not that the Cypher ever
sets that flag for a structural node. In both cases a second source satisfies the test while the
real path bypasses the guard (ledger T17).

These tests target the routing and the predicates instead, in both directions.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from menhir.infrastructure.graphiti_model_patches import (
    _patch_graphiti_adaptive_dedupe,
    _patch_graphiti_structural_candidate_isolation,
)


def _entity(name: str, uuid: str, **attributes: object):
    from graphiti_core.nodes import EntityNode

    return EntityNode(
        uuid=uuid,
        name=name,
        group_id="",
        labels=["Entity"],
        created_at=datetime.now(timezone.utc),
        summary="",
        attributes=dict(attributes),
    )


@pytest.fixture
def patched_node_operations(monkeypatch):
    """Install the real patch pair, in the order `graphiti_client` installs them."""
    import graphiti_core.utils.bulk_utils as bulk_utils
    import graphiti_core.utils.maintenance.node_operations as node_operations

    for flag in (
        "_menhir_structural_candidate_isolation_patched",
        "_menhir_adaptive_dedupe_patched",
    ):
        monkeypatch.setattr(node_operations, flag, False, raising=False)
    monkeypatch.setattr(
        node_operations, "_collect_candidate_nodes", node_operations._collect_candidate_nodes
    )
    monkeypatch.setattr(
        node_operations, "resolve_extracted_nodes", node_operations.resolve_extracted_nodes
    )
    monkeypatch.setattr(
        bulk_utils, "resolve_extracted_nodes", bulk_utils.resolve_extracted_nodes, raising=False
    )
    return node_operations, bulk_utils


# ---------------------------------------------------------------------------
# Direction 1 (CF-252): a structural node must not become a dedupe target
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_structural_candidate_cannot_resolve_an_extracted_entity(
    patched_node_operations, monkeypatch
):
    """END-TO-END ROUTING, not the filter in isolation.

    The stub stands in for the candidate search and returns a structural node under the SAME name as
    the extracted entity, which is the case exact-name similarity resolves on. If anything captured
    `_collect_candidate_nodes` by value, or reached resolution by another route, the extracted
    entity resolves onto the structural node's uuid and this fails.
    """
    node_operations, _ = patched_node_operations

    structural = _entity("scoring_service.py", "struct-1", structure_role="file")

    async def _stub_collect(clients, extracted_nodes, existing_nodes_override):
        del clients, existing_nodes_override
        return [[structural] for _ in extracted_nodes]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _stub_collect)
    _patch_graphiti_structural_candidate_isolation()
    _patch_graphiti_adaptive_dedupe()

    extracted = _entity("scoring_service.py", "extracted-1")
    resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
        _Clients(), [extracted]
    )

    assert uuid_map == {"extracted-1": "extracted-1"}, (
        "an extracted entity resolved onto a STRUCTURAL node -- the structural/semantic partition "
        "is not being applied on the path resolution actually takes"
    )
    assert [n.uuid for n in resolved] == ["extracted-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_positive_control_shows_the_same_name_really_would_resolve(
    patched_node_operations, monkeypatch
):
    """CONTROL. Without the structural marking, the identical setup MUST resolve.

    Without this, the test above could pass because the stub never produced a resolvable candidate
    -- the vacuity that made three earlier tests in this programme prove nothing.
    """
    node_operations, _ = patched_node_operations

    twin = _entity("scoring_service.py", "semantic-1")  # no structure_role

    async def _stub_collect(clients, extracted_nodes, existing_nodes_override):
        del clients, existing_nodes_override
        return [[twin] for _ in extracted_nodes]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _stub_collect)
    _patch_graphiti_structural_candidate_isolation()
    _patch_graphiti_adaptive_dedupe()

    extracted = _entity("scoring_service.py", "extracted-1")
    _resolved, uuid_map, _pairs = await node_operations.resolve_extracted_nodes(
        _Clients(), [extracted]
    )

    assert uuid_map == {"extracted-1": "semantic-1"}, (
        "the harness never resolves anything, so the isolation test above proves nothing"
    )


@pytest.mark.unit
def test_the_bulk_path_cannot_diverge_from_the_patched_resolver(patched_node_operations):
    """`bulk_utils` imports `resolve_extracted_nodes` BY VALUE at module import.

    So patching only `node_operations` would leave bulk ingestion running graphiti's original
    resolver with no structural filter. Both names must point at the same object.
    """
    node_operations, bulk_utils = patched_node_operations
    _patch_graphiti_structural_candidate_isolation()
    _patch_graphiti_adaptive_dedupe()

    assert bulk_utils.resolve_extracted_nodes is node_operations.resolve_extracted_nodes


@pytest.mark.unit
def test_the_runtime_client_installs_both_boundary_patches():
    """Neither patch defends anything if the client stops calling it.

    Read as source: constructing a real client needs a database and a provider. What must hold is
    that the boundary patches are in the startup sequence at all.
    """
    from menhir.infrastructure import graphiti_client

    source = inspect.getsource(graphiti_client)
    for patch_name in (
        "_patch_graphiti_structural_candidate_isolation()",
        "_patch_graphiti_untyped_attribute_preservation()",
    ):
        assert patch_name in source, f"{patch_name} is no longer applied at client construction"


@pytest.mark.unit
def test_the_filter_still_receives_the_property_it_reads():
    """The isolation filter reads `candidate.attributes['structure_role']`.

    That only works while graphiti hydrates entity nodes with their full property map. If the return
    projection narrows, every candidate arrives with `structure_role` absent, the filter passes
    everything, and NOTHING FAILS -- the guard goes blind rather than loud. This is the same shape as
    CF-252 itself: a predicate defeated by a missing property.
    """
    from graphiti_core.graph_queries import GraphProvider
    from graphiti_core.models.nodes.node_db_queries import get_entity_node_return_query

    projection = get_entity_node_return_query(GraphProvider.NEO4J)
    assert "properties(n) AS attributes" in projection, (
        "graphiti no longer returns the full property map for entity nodes, so "
        "_is_structural_graphiti_candidate can no longer see structure_role"
    )


# ---------------------------------------------------------------------------
# Direction 2 (CF-253): a structural node must not become a merge participant
# ---------------------------------------------------------------------------


@pytest.mark.online
@pytest.mark.parametrize("role_position", ["survivor", "absorbed"])
def test_a_structural_node_is_ineligible_in_either_merge_position(test_neo4j_repo, role_position):
    """The Cypher PREDICATE, executed -- not the domain policy handed a boolean.

    `test_merge_eligibility.py` constructs `NodeSignals(ineligible_role=True)` and checks the policy
    vetoes. That proves the policy and says nothing about whether the Cypher ever sets the flag. This
    runs `_INELIGIBLE_ROLE_PREDICATE` against a real structural node in both positions, because CF-252
    and CF-253 are the two directions of the same violation and a one-sided guard closes one of them.
    """
    from uuid import uuid4

    from menhir.domain import merge_eligibility as me
    from menhir.infrastructure.correlation_queries import CorrelationRepository

    structural_uuid, memory_uuid = str(uuid4()), str(uuid4())
    test_neo4j_repo.execute(
        """
        CREATE (s:Entity {uuid: $s, name: 'src', type: 'SEMANTIC', namespace: 'default',
                          scope: 'PERSISTENT', structure_role: 'directory',
                          structure_project: 'p', structure_path: 'src'})
        CREATE (m:Entity {uuid: $m, name: 'a remembered preference', type: 'SEMANTIC',
                          namespace: 'default', scope: 'PERSISTENT'})
        """,
        {"s": structural_uuid, "m": memory_uuid},
    )

    queries = CorrelationRepository(test_neo4j_repo)
    if role_position == "survivor":
        result = queries.evaluate_merge_eligibility(structural_uuid, memory_uuid)
    else:
        result = queries.evaluate_merge_eligibility(memory_uuid, structural_uuid)

    assert not result.allowed
    assert result.reason_code == me.INELIGIBLE_ROLE, (
        f"a structural node was mergeable as the {role_position}; the partition is one-sided"
    )


@pytest.mark.online
def test_a_node_that_LOST_its_structural_role_is_still_ineligible(test_neo4j_repo):
    """CF-252's 13 nodes, exactly as production holds them.

    They no longer carry `structure_role` -- that is the defect -- so the role half of the predicate
    cannot protect them. The name-shape half still can, and this pins that it does: without it, a
    stripped structural node is freely mergeable and the damage compounds.
    """
    from uuid import uuid4

    from menhir.domain import merge_eligibility as me
    from menhir.infrastructure.correlation_queries import CorrelationRepository

    stripped_uuid, memory_uuid = str(uuid4()), str(uuid4())
    test_neo4j_repo.execute(
        """
        CREATE (s:Entity {uuid: $s, name: 'scoring_service.py', type: 'SEMANTIC',
                          namespace: 'default', scope: 'PERSISTENT'})
        CREATE (m:Entity {uuid: $m, name: 'a remembered preference', type: 'SEMANTIC',
                          namespace: 'default', scope: 'PERSISTENT'})
        """,
        {"s": stripped_uuid, "m": memory_uuid},
    )

    result = CorrelationRepository(test_neo4j_repo).evaluate_merge_eligibility(
        memory_uuid, stripped_uuid
    )
    assert not result.allowed
    assert result.reason_code == me.INELIGIBLE_ROLE


class _Clients:
    """Minimal stand-in: the candidate search is stubbed, so nothing here is reached."""

    llm_client = None
    embedder = None
    driver = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_adaptive_resolver_reads_the_collector_at_call_time(
    patched_node_operations, monkeypatch
):
    """LATE BINDING is what makes the patch ORDER irrelevant, so it is the thing to pin.

    `_patch_graphiti_adaptive_dedupe` replaces `resolve_extracted_nodes` with its own implementation
    that calls `_no_module._collect_candidate_nodes`. Because that is a module-attribute lookup
    performed on every call, it picks up the structural filter no matter which patch is installed
    first. Rebind it to a by-value capture -- `_original = _no_module._collect_candidate_nodes` at
    patch time -- and the filter is bypassed whenever the isolation patch is applied second.

    `graphiti_client` currently installs isolation first, which would mask that regression entirely.
    So this asserts the invariant rather than the ordering: a collector swapped in AFTER both patches
    are installed must still be the one used.
    """
    node_operations, _ = patched_node_operations
    _patch_graphiti_structural_candidate_isolation()
    _patch_graphiti_adaptive_dedupe()

    seen: list[str] = []

    async def _late_collector(clients, extracted_nodes, existing_nodes_override):
        del clients, existing_nodes_override
        seen.append("called")
        return [[] for _ in extracted_nodes]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _late_collector)
    await node_operations.resolve_extracted_nodes(_Clients(), [_entity("x", "x-1")])

    assert seen == ["called"], (
        "the resolver captured _collect_candidate_nodes by value; a collector installed after it "
        "-- which is what the structural filter is when patch order changes -- would be ignored"
    )
