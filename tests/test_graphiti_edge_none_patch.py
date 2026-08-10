"""Regression test for the EntityEdge None-field coercion patch.

Recurring failure (incl. 2026-07-11): an episode failed enrichment with
``N validation errors for EntityEdge / uuid / group_id / name / fact / ...
Input should be a valid string [input_value=None]`` when dedupe/resolve built an
``EntityEdge`` from a fully-degenerate LLM edge. ``_patch_graphiti_edge_none_fields``
drops an explicit ``uuid=None`` (so the uuid4 default runs) and coerces the other
required-str fields to '' so the whole episode is not lost.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_edge_none_fields,
)

_NOW = datetime.now(timezone.utc)


def test_fully_degenerate_edge_constructs_after_patch():
    _patch_graphiti_edge_none_fields()  # idempotent
    # Every one of these str fields is required (uuid has a default_factory) — before
    # the patch this raised a Pydantic ValidationError and failed the episode.
    # created_at is required and is always supplied by the real construction site.
    edge = EntityEdge(
        uuid=None,
        group_id=None,
        name=None,
        fact=None,
        source_node_uuid=None,
        target_node_uuid=None,
        episodes=None,
        created_at=_NOW,
    )
    assert edge.group_id == ""
    assert edge.name == ""
    assert edge.fact == ""
    assert edge.source_node_uuid == ""
    assert edge.target_node_uuid == ""
    # uuid must be a freshly generated non-empty string, never '' or None.
    assert isinstance(edge.uuid, str) and edge.uuid
    # episodes must fall back to its list default, never None.
    assert edge.episodes == []


def test_real_edge_values_preserved():
    _patch_graphiti_edge_none_fields()
    edge = EntityEdge(
        group_id="g",
        name="WORKS_ON",
        fact="Alice works on menhir",
        source_node_uuid="src",
        target_node_uuid="dst",
        created_at=_NOW,
    )
    assert edge.name == "WORKS_ON"
    assert edge.fact == "Alice works on menhir"
    assert edge.group_id == "g"
    assert isinstance(edge.uuid, str) and edge.uuid


def test_explicit_uuid_is_preserved():
    _patch_graphiti_edge_none_fields()
    edge = EntityEdge(
        uuid="fixed-uuid-123",
        group_id="g",
        name="R",
        fact="f",
        source_node_uuid="s",
        target_node_uuid="t",
        created_at=_NOW,
    )
    assert edge.uuid == "fixed-uuid-123"


def test_patch_is_idempotent():
    _patch_graphiti_edge_none_fields()
    _patch_graphiti_edge_none_fields()
    edge = EntityEdge(
        group_id="g", name=None, fact="f", source_node_uuid="s",
        target_node_uuid="t", created_at=_NOW,
    )
    assert edge.name == ""
    assert getattr(EntityEdge, "_yawn_edge_patched", False) is True
