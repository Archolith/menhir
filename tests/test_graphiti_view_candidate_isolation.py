"""Issue #94: Menhir View nodes must never be Graphiti identity-resolution targets.

Views are `:Entity` nodes with a name embedding, so Graphiti's semantic candidate search
returns them like any memory node. Live graph evidence (2026-09-12, `--keep` run): after
"Alice owns 37 coins." produced a counter View named "alice's coins: 37 ...", the next
extraction resolved its "coins" entity ONTO that View -- Graphiti wrote
`(:Episodic)-[:MENTIONS]->(view)` (edges carrying Graphiti's group_id/scope/weight/uuid) and
`(Alice)-[:OWNS {fact: "Alice owns 37 coins."}]->(view)`. The View's provenance parity gate
then refused every refresh, correctly, and no scalar_state View could ever materialize.

The structural isolation patch already excludes `structure_role` candidates for the same
reason. This is its twin for Views.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_model_patches import (
    _is_structural_graphiti_candidate,
    _is_view_graphiti_candidate,
    _patch_graphiti_structural_candidate_isolation,
)

pytestmark = pytest.mark.unit


def _node(name: str, **attributes: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, attributes=attributes)


# What the View writer actually stamps (view_write_repository.py:400) -- all three markers.
COUNTER_VIEW = _node(
    "alice's coins: 37. coins = 37", is_view=True, view_kind="counter",
    view_class="FACT", qs_current=True, view_current=True,
)


@pytest.mark.parametrize("attrs", [
    {"is_view": True},
    {"view_kind": "scalar_state"},
    {"view_kind": "counter"},
    {"view_class": "FACT"},
    {"is_view": True, "view_kind": "timeline", "view_class": "FACT"},
])
def test_any_view_marker_makes_a_candidate_ineligible(attrs: dict) -> None:
    assert _is_view_graphiti_candidate(_node("whatever", **attrs))


@pytest.mark.parametrize("attrs", [
    {},
    {"source": "user"},
    {"is_view": False},
    {"structure_role": "project"},   # structural, handled by its own predicate
    {"summary": "Alice's coins"},    # a memory node whose NAME resembles a View is still eligible
])
def test_ordinary_memory_nodes_stay_eligible(attrs: dict) -> None:
    assert not _is_view_graphiti_candidate(_node("Alice's coins", **attrs))


def test_missing_or_non_dict_attributes_are_not_views() -> None:
    assert not _is_view_graphiti_candidate(SimpleNamespace(name="x"))
    assert not _is_view_graphiti_candidate(SimpleNamespace(name="x", attributes=None))
    assert not _is_view_graphiti_candidate(SimpleNamespace(name="x", attributes="is_view"))


def test_the_two_predicates_are_independent() -> None:
    """A View is not structural and a structural node is not a View; both must be excluded."""
    assert _is_view_graphiti_candidate(COUNTER_VIEW)
    assert not _is_structural_graphiti_candidate(COUNTER_VIEW)
    structural = _node("sample-app", structure_role="project")
    assert _is_structural_graphiti_candidate(structural)
    assert not _is_view_graphiti_candidate(structural)


@pytest.mark.asyncio
async def test_candidate_collection_excludes_view_nodes(monkeypatch) -> None:
    """The #94 shape: the extracted 'coins' entity must not be offered the counter View."""
    import graphiti_core.utils.maintenance.node_operations as node_operations

    semantic_coins = _node("Alice's coins", source="user")
    structural = _node("sample-app", structure_role="project")

    async def _collect_candidate_nodes(clients, extracted_nodes, existing_nodes_override):
        del clients, extracted_nodes, existing_nodes_override
        # Graphiti's own search ranks the View FIRST: its name literally contains the mention.
        return [[COUNTER_VIEW, semantic_coins, structural]]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect_candidate_nodes)
    monkeypatch.setattr(
        node_operations, "_menhir_structural_candidate_isolation_patched", False, raising=False
    )
    _patch_graphiti_structural_candidate_isolation()

    filtered = await node_operations._collect_candidate_nodes(object(), [object()], None)
    assert filtered == [[semantic_coins]]
