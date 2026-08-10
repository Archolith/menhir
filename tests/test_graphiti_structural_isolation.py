"""Regression coverage for the Graphiti/structure-graph ownership boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_model_patches import (
    _is_structural_graphiti_candidate,
    _patch_graphiti_structural_candidate_isolation,
    _patch_graphiti_untyped_attribute_preservation,
)


def _node(name: str, **attributes: object) -> SimpleNamespace:
    return SimpleNamespace(name=name, attributes=attributes)


@pytest.mark.unit
def test_structure_role_marks_graphiti_candidate_as_ineligible() -> None:
    assert _is_structural_graphiti_candidate(
        _node("sample-app", structure_role="project", root_path=r"C:\projects\sample-app")
    )
    assert not _is_structural_graphiti_candidate(_node("sample-app", source="project-scan"))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_candidate_collection_excludes_structural_nodes(monkeypatch) -> None:
    import graphiti_core.utils.maintenance.node_operations as node_operations

    structural = _node("sample-app", structure_role="project", structure_path=".")
    semantic = _node("sample-app", source="project-scan")

    async def _collect_candidate_nodes(clients, extracted_nodes, existing_nodes_override):
        del clients, extracted_nodes, existing_nodes_override
        return [[structural, semantic]]

    monkeypatch.setattr(node_operations, "_collect_candidate_nodes", _collect_candidate_nodes)
    monkeypatch.setattr(
        node_operations, "_menhir_structural_candidate_isolation_patched", False, raising=False
    )

    _patch_graphiti_structural_candidate_isolation()

    filtered = await node_operations._collect_candidate_nodes(object(), [object()], None)
    assert filtered == [[semantic]]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type", [None, SimpleNamespace(model_fields={})])
async def test_untyped_hydration_preserves_existing_attributes(monkeypatch, entity_type) -> None:
    import graphiti_core.utils.maintenance.node_operations as node_operations

    async def _unexpected_original(*args, **kwargs):
        del args, kwargs
        raise AssertionError("untyped hydration must not delegate to Graphiti's destructive path")

    monkeypatch.setattr(
        node_operations, "_extract_entity_attributes", _unexpected_original
    )
    monkeypatch.setattr(
        node_operations, "_menhir_untyped_attribute_preservation_patched", False, raising=False
    )

    _patch_graphiti_untyped_attribute_preservation()

    node = _node(
        "sample-app",
        structure_project="sample-app",
        structure_path=".",
        structure_role="project",
        root_path=r"C:\projects\sample-app",
    )
    result = await node_operations._extract_entity_attributes(
        object(), node, None, None, entity_type
    )

    assert result == node.attributes
    assert result is not node.attributes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_typed_hydration_still_delegates(monkeypatch) -> None:
    import graphiti_core.utils.maintenance.node_operations as node_operations

    expected = {"typed": "value"}

    async def _extract_entity_attributes(*args, **kwargs):
        del args, kwargs
        return expected

    monkeypatch.setattr(
        node_operations, "_extract_entity_attributes", _extract_entity_attributes
    )
    monkeypatch.setattr(
        node_operations, "_menhir_untyped_attribute_preservation_patched", False, raising=False
    )

    _patch_graphiti_untyped_attribute_preservation()

    entity_type = SimpleNamespace(model_fields={"typed": object()})
    result = await node_operations._extract_entity_attributes(
        object(), _node("typed"), None, None, entity_type
    )
    assert result is expected
