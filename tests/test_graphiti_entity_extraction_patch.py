"""Regression tests for the entity-extraction remap patch.

``_patch_graphiti_entity_extraction`` makes Graphiti's ExtractedEntities tolerant of the
shapes local/small LLMs actually emit: alternate name keys (``entity_name``, ``entity``,
and typo variants like ``name-``/``name_``/``Name``), missing ``entity_type_id`` (defaults
to 0), and degenerate single-pair dicts. Failing to remap these fails the whole episode.
"""

from __future__ import annotations

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.utils.maintenance.node_operations as no  # noqa: E402

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_entity_extraction,
)


def _model():
    _patch_graphiti_entity_extraction()  # idempotent
    return no.ExtractedEntities


@pytest.mark.parametrize(
    "raw, expected_name, expected_type",
    [
        ({"name": "Ledger"}, "Ledger", 0),  # name only -> type defaults to 0 (the 17-bucket)
        ({"name": "Bar", "entity_type_id": 2}, "Bar", 2),
        ({"entity_name": "Foo"}, "Foo", 0),  # qwen alt key
        ({"name-": ".agent/x", "entity_type_id": 0}, ".agent/x", 0),  # trailing-hyphen typo
        ({"name_": "Baz", "entity_type_id": 1}, "Baz", 1),
        ({"Name ": "Qux"}, "Qux", 0),  # capitalized + trailing space
    ],
)
def test_name_key_variants_and_type_default(raw, expected_name, expected_type):
    ExtractedEntities = _model()
    e = ExtractedEntities(extracted_entities=[raw]).extracted_entities[0]
    assert e.name == expected_name
    assert e.entity_type_id == expected_type


def test_single_pair_name_to_typeid_dict():
    # {name: type_id} degenerate single-pair dict
    ExtractedEntities = _model()
    e = ExtractedEntities(extracted_entities=[{"Ledger": 3}]).extracted_entities[0]
    assert e.name == "Ledger"
    assert e.entity_type_id == 3
