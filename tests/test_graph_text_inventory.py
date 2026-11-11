"""R1.1: the Neo4j text inventory's classification rules (offline)."""

from __future__ import annotations

from menhir.infrastructure.anchored_time_persist import CONTRACT_PROPERTIES
from menhir.infrastructure.graph_text_inventory import (
    ENTITY_KNOWN_TEXT_PROPERTIES,
    ENTITY_STRUCTURAL_PROPERTIES,
    GRAPH_TEXT_INVENTORY,
    EraseAction,
    HolderSelector,
    entity_text_properties,
    inventoried_pairs,
    is_text_property,
)


def test_entity_is_text_by_default() -> None:
    # A property nobody has argued is structural is text, so a new Graphiti attribute is caught.
    assert is_text_property("Entity", "some_new_custom_attribute")
    assert not is_text_property("Entity", "last_accessed")
    assert entity_text_properties({"uuid", "summary", "x_attr"}) == {"summary", "x_attr"}


def test_known_entity_text_is_never_allowlisted_as_structural() -> None:
    assert not set(ENTITY_KNOWN_TEXT_PROPERTIES) & ENTITY_STRUCTURAL_PROPERTIES


def test_audit_additions_are_inventoried() -> None:
    pairs = inventoried_pairs()
    for pair in [
        ("Entity", "original_content"),  # CF-330
        ("Entity", "view_summary"),
        ("RELATES_TO", "time_expression"),  # CF-330
        ("EdgeInvalidationConflict", "kept_fact"),  # CF-331
        ("EdgeInvalidationConflict", "cause_fact"),
        ("TurnEvidence", "text"),
        ("TypedAssertion", "stated_span"),
        ("Episodic", "content"),
    ]:
        assert pair in pairs, pair
    raw = [h for h in GRAPH_TEXT_INVENTORY if h.discriminator == "raw_capture_for"]
    assert raw and raw[0].action is EraseAction.DELETE  # CF-327


def test_only_the_quoting_anchored_time_prop_is_text() -> None:
    # time_expression quotes the memory (anchored_time_persist.py writes result.expression);
    # anchor_ref is an edge uuid, outcome a reason enum, contract a schema/model tag.
    assert "time_expression" in CONTRACT_PROPERTIES
    assert is_text_property("RELATES_TO", "time_expression")
    for prop in ("time_anchor_ref", "time_outcome", "time_contract", "time_kind"):
        assert not is_text_property("RELATES_TO", prop), prop


def test_every_holder_names_a_selector_and_properties() -> None:
    for holder in GRAPH_TEXT_INVENTORY:
        assert holder.text_properties, holder
        assert holder.selectors, holder
        assert HolderSelector.UNCLASSIFIED not in holder.selectors or holder.finding, holder
        assert holder.kind in {"node", "relationship"}, holder
