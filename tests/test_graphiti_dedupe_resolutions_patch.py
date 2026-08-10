"""Regression test for the NodeResolutions degenerate-entry patch.

Recurring failure (incl. 2026-07-11): an episode failed enrichment with
``N validation errors for NodeResolutions / entity_resolutions.N.id / Field required``
when the dedupe LLM returned a degenerate resolution like ``{'': ''}``. The construction
``NodeResolutions(**llm_response)`` fails in Pydantic *before* Graphiti's own downstream
skip-logic runs. ``_patch_graphiti_dedupe_resolutions`` drops entries without a usable
integer id so the valid resolutions still apply.

Targets the Graphiti >=0.29 ``NodeDuplicate`` shape only (``duplicate_candidate_id:int``,
``-1`` = no duplicate) per the 2026-07-12 dependency upgrade
(.agent/reviews/menhir-graphiti-0.29-dependency-probe-2026-07-12.md) — single-user
deployment, no compatibility shim kept for the pre-0.29 ``duplicate_name:str`` shape.
"""

from __future__ import annotations

import pytest

pytest.importorskip("graphiti_core")

import graphiti_core.utils.maintenance.node_operations as no  # noqa: E402

from menhir.infrastructure.graphiti_patches import (  # noqa: E402
    _patch_graphiti_dedupe_resolutions,
)


def _resolutions():
    _patch_graphiti_dedupe_resolutions()  # idempotent
    return no.NodeResolutions


def test_degenerate_entry_is_dropped():
    NodeResolutions = _resolutions()
    r = NodeResolutions(
        entity_resolutions=[
            {"id": 0, "name": "Ledger", "duplicate_candidate_id": -1},
            {"": ""},  # degenerate — no id/name, must be dropped, not fatal
            {"id": 2, "name": "Menhir", "duplicate_candidate_id": 1},
        ]
    )
    ids = [e.id for e in r.entity_resolutions]
    assert ids == [0, 2]  # degenerate entry dropped, valid ones kept


def test_missing_name_and_duplicate_candidate_id_defaulted():
    NodeResolutions = _resolutions()
    # id present but name/duplicate_candidate_id missing -> kept with fail-safe defaults
    r = NodeResolutions(entity_resolutions=[{"id": 3}])
    assert len(r.entity_resolutions) == 1
    e = r.entity_resolutions[0]
    assert e.id == 3 and e.name == "" and e.duplicate_candidate_id == -1


def test_non_integer_duplicate_candidate_id_defaults_to_no_duplicate():
    NodeResolutions = _resolutions()
    # a malformed (non-integer-coercible) duplicate_candidate_id must fail safe to -1,
    # never raise and never silently pick an arbitrary existing entity
    r = NodeResolutions(entity_resolutions=[{"id": 4, "name": "X", "duplicate_candidate_id": "bogus"}])
    e = r.entity_resolutions[0]
    assert (e.id, e.name, e.duplicate_candidate_id) == (4, "X", -1)


def test_all_valid_preserved():
    NodeResolutions = _resolutions()
    r = NodeResolutions(
        entity_resolutions=[{"id": 1, "name": "A", "duplicate_candidate_id": 7}]
    )
    e = r.entity_resolutions[0]
    assert (e.id, e.name, e.duplicate_candidate_id) == (1, "A", 7)


def test_all_degenerate_yields_empty_not_error():
    NodeResolutions = _resolutions()
    r = NodeResolutions(entity_resolutions=[{"": ""}, {"foo": "bar"}])
    assert r.entity_resolutions == []
