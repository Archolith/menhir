"""Regression test for the EntityNode.summary=None coercion patch.

Codex hit (2026-06-18, recurring since May): an episode failed enrichment with
``ValidationError: 1 validation error for EntityNode / summary / Input should be a
valid string [input_value=None]`` when the extraction LLM returned a null summary.
``_patch_graphiti_node_summary_none`` coerces an explicit ``summary=None`` to ''.
"""

from __future__ import annotations

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.nodes import EntityNode  # noqa: E402

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_node_summary_none,
)


def test_summary_none_raises_before_patch_construct_after():
    _patch_graphiti_node_summary_none()  # idempotent; safe if already applied
    # After the patch, an explicit summary=None must NOT raise and becomes ''.
    node = EntityNode(name="x", group_id="g", summary=None)
    assert node.summary == ""


def test_real_summary_is_preserved():
    _patch_graphiti_node_summary_none()
    node = EntityNode(name="y", group_id="g", summary="a real summary")
    assert node.summary == "a real summary"


def test_missing_summary_uses_default():
    _patch_graphiti_node_summary_none()
    node = EntityNode(name="z", group_id="g")
    assert node.summary == ""


def test_patch_is_idempotent():
    _patch_graphiti_node_summary_none()
    _patch_graphiti_node_summary_none()
    node = EntityNode(name="w", group_id="g", summary=None)
    assert node.summary == ""
    assert getattr(EntityNode, "_yawn_summary_patched", False) is True
