"""Structural/semantic partition enforced through the fork's CandidateFilterHook."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes

from menhir.infrastructure.graphiti_resolution_policy import MenhirCandidateFilterHook


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


class _Clients:
    """Minimal stand-in: the candidate search is stubbed, so nothing here is reached."""

    llm_client = None
    embedder = None
    driver = None


def _stub_collect(monkeypatch, nodes):
    import graphiti_core.utils.maintenance.node_operations as node_operations

    async def _stub(clients, extracted_nodes, existing_nodes_override):
        del clients, existing_nodes_override
        return [list(nodes) for _ in extracted_nodes]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _stub)


# ---------------------------------------------------------------------------
# Direction 1 (CF-252): a structural node must not become a dedupe target
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_structural_candidate_cannot_resolve_an_extracted_entity(monkeypatch):
    """END-TO-END ROUTING, not the filter in isolation.

    The stub stands in for the candidate search and returns a structural node under the SAME name as
    the extracted entity, which is the case exact-name similarity resolves on. The Menhir candidate
    filter hook must remove it before deterministic resolution runs.
    """
    structural = _entity("scoring_service.py", "struct-1", structure_role="file")
    _stub_collect(monkeypatch, [structural])

    extracted = _entity("scoring_service.py", "extracted-1")
    resolved, uuid_map, _pairs = await resolve_extracted_nodes(
        _Clients(), [extracted], candidate_filter_hook=MenhirCandidateFilterHook()
    )

    assert uuid_map == {"extracted-1": "extracted-1"}, (
        "an extracted entity resolved onto a STRUCTURAL node -- the structural/semantic partition "
        "is not being applied on the path resolution actually takes"
    )
    assert [n.uuid for n in resolved] == ["extracted-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_positive_control_shows_the_same_name_really_would_resolve(monkeypatch):
    """CONTROL. Without the structural marking, the identical setup MUST resolve."""
    twin = _entity("scoring_service.py", "semantic-1")  # no structure_role
    _stub_collect(monkeypatch, [twin])

    extracted = _entity("scoring_service.py", "extracted-1")
    _resolved, uuid_map, _pairs = await resolve_extracted_nodes(
        _Clients(), [extracted], candidate_filter_hook=MenhirCandidateFilterHook()
    )

    assert uuid_map == {"extracted-1": "semantic-1"}, (
        "the harness never resolves anything, so the isolation test above proves nothing"
    )


@pytest.mark.unit
def test_the_runtime_client_installs_the_candidate_filter_hook():
    """The hook defends nothing if the client stops wiring it at construction."""
    import inspect

    from menhir.infrastructure import graphiti_client

    source = inspect.getsource(graphiti_client)
    assert "candidate_filter_hook=MenhirCandidateFilterHook()" in source, (
        "the Menhir candidate filter hook is no longer installed at client construction"
    )


@pytest.mark.unit
def test_the_filter_still_receives_the_property_it_reads():
    """The isolation filter reads `candidate.attributes['structure_role']`.

    That only works while graphiti hydrates entity nodes with their full property map. If the return
    projection narrows, every candidate arrives with `structure_role` absent, the filter passes
    everything, and NOTHING FAILS -- the guard goes blind rather than loud.
    """
    from graphiti_core.graph_queries import GraphProvider
    from graphiti_core.models.nodes.node_db_queries import get_entity_node_return_query

    projection = get_entity_node_return_query(GraphProvider.NEO4J)
    assert "properties(n) AS attributes" in projection, (
        "graphiti no longer returns the full property map for entity nodes, so "
        "_is_structural_graphiti_candidate can no longer see structure_role"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_hook_composes_per_extracted_node_without_mutating_pools(monkeypatch):
    """Exclusions are per (extracted node, candidate); the caller's lists are untouched."""
    structural = _entity("scoring_service.py", "struct-1", structure_role="file")
    semantic = _entity("scoring_service.py", "semantic-1")
    _stub_collect(monkeypatch, [structural, semantic])

    extracted = _entity("scoring_service.py", "extracted-1")
    resolved, uuid_map, _pairs = await resolve_extracted_nodes(
        _Clients(), [extracted], candidate_filter_hook=MenhirCandidateFilterHook()
    )

    # The structural candidate is excluded, so the exact-name match lands on the
    # ordinary semantic twin rather than the structural node.
    assert uuid_map == {"extracted-1": "semantic-1"}
    assert [node.uuid for node in resolved] == ["semantic-1"]

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
