"""Titled-list membership: parse `title:` + short items into MEMBER_OF edges.

WHY: a turn like "agents names below:\\nAdmon\\nMagdy\\n..." states membership through SYNTAX, not a
verb, so combined extraction returns names with NO relation between them. Graphiti then orphan-prunes
every node for want of an edge and the whole episode collapses -- the extraction was correct and the
content is lost anyway. Measured on the LME multismoke corpus: the agents roster failed permanently
and its 7 names only survived because a later assistant turn happened to restate them.

THE RISK BEING GUARDED IS THE OPPOSITE ONE. Refusing a real list costs one enrichment that behaves
exactly as it does today. Accepting PROSE mints membership edges that are silently wrong, which is
far worse than a loud failure. So the negative cases below are the primary contract, and the parser
is a whitelist: one non-conforming item refuses the whole block.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.graphiti_extraction_patches import (
    parse_titled_list,
    _sanitize_combined_payload,
    CombinedExtractionReceipt,
)

#: VERBATIM from the LongMemEval item that motivated this (question_id 7161e7e2), including the
#: missing newline after the colon. An earlier version of this file invented `below:\nAdmon`, and
#: every test passed against a shape that does not occur in the data while the parser returned None
#: on the real thing. Take fixtures FROM the corpus; do not write them from memory.
ROSTER = "user: agents names below:Admon\nMagdy\nEhab\nSara\nMostafa\nNemr\nAdam"

#: The same list typed with a line break after the colon. Both spellings must parse identically --
#: whether a human pressed Enter is not a semantic difference.
ROSTER_NEWLINE = "user: agents names below:\nAdmon\nMagdy\nEhab\nSara\nMostafa\nNemr\nAdam"


# --------------------------------------------------------------------- MUST NOT parse (the contract)

@pytest.mark.parametrize("text", [
    # prose containing a colon -- the single most likely false positive
    "user: here is the thing:\nI went to the store today and bought milk\nthen I came home\nit was fine",
    # a colon with only two items: too few to distinguish from ordinary punctuation
    "user: my picks:\nAdmon\nMagdy",
    # sentence punctuation in an item => prose, refuse the WHOLE block even though most lines fit
    "user: notes:\nAdmon\nMagdy\nI think Ehab is unavailable.\nSara",
    # an over-long item is a clause, not a name
    "user: agents:\nAdmon\nMagdy\nthe rest of the team will be assigned later this week somehow",
    # no colon at all: nothing marks this as a list
    "user: agents\nAdmon\nMagdy\nEhab\nSara",
    # single line: no items
    "user: agents names below: Admon, Magdy, Ehab",
    # empty
    "",
])
def test_refuses_non_lists(text):
    assert parse_titled_list(text) is None


def test_one_bad_item_refuses_the_whole_block():
    """No partial parse: a half-read list would assert membership for some names and silently drop
    others, which is harder to notice than refusing outright."""
    assert parse_titled_list("user: agents:\nAdmon\nMagdy\nEhab\nhe said he would call back later.") is None


# ------------------------------------------------------------------------------- MUST parse

@pytest.mark.parametrize("text", [ROSTER, ROSTER_NEWLINE], ids=["first-item-on-title-line", "newline-after-colon"])
def test_parses_the_roster_that_motivated_this(text):
    """Both spellings, because the REAL turn has no newline after the colon."""
    parsed = parse_titled_list(text)
    assert parsed is not None
    title, items = parsed
    assert title == "agents names below"
    assert items == ["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]


def test_parses_the_string_read_from_the_actual_corpus_fixture():
    """Anti-self-consistency guard: read the turn OFF DISK rather than trusting a literal in this
    file. A hand-written fixture that drifts from the corpus makes every other test here vacuous --
    which is exactly what happened before this test existed."""
    import json
    from pathlib import Path

    fixture = (Path(__file__).resolve().parents[2]
               / "archolith-bench/scripts/longmemeval/fixtures/roster-smoke-7161e7e2.json")
    if not fixture.exists():
        pytest.skip(f"corpus fixture not present: {fixture}")
    items = json.loads(fixture.read_text(encoding="utf-8"))
    turns = [t["content"] for s in items[0]["haystack_sessions"] for t in s
             if "Admon" in str(t.get("content", ""))]
    roster_turn = next(t for t in turns if t.strip().lower().startswith("agents names"))
    assert roster_turn == ROSTER.split("user: ", 1)[1], "literal drifted from the corpus"

    parsed = parse_titled_list(f"user: {roster_turn}")
    assert parsed is not None
    assert parsed[1] == ["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]


@pytest.mark.parametrize("bullet", ["- ", "* ", "1. ", "2) "])
def test_bullet_and_number_decoration_is_stripped(bullet):
    text = f"user: agents:\n{bullet}Admon\n{bullet}Magdy\n{bullet}Ehab"
    parsed = parse_titled_list(text)
    assert parsed is not None
    assert parsed[1] == ["Admon", "Magdy", "Ehab"]


def test_works_without_a_role_prefix():
    parsed = parse_titled_list("shopping list:\nmilk\neggs\nbread")
    assert parsed == ("shopping list", ["milk", "eggs", "bread"])


# ------------------------------------------------------- edge emission through the sanitation path

def _payload(entity_names, edges=None):
    return {
        "extracted_entities": [{"name": n, "entity_type_id": 0} for n in entity_names],
        "edges": edges or [],
    }


def test_membership_edges_emitted_for_a_parsed_roster():
    receipt = CombinedExtractionReceipt()
    names = ["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]
    out = _sanitize_combined_payload(_payload(names), receipt, ROSTER)
    assert receipt.list_membership_edges_added == 7
    assert all(e["relation_type"] == "MEMBER_OF" for e in out["edges"])
    assert {e["source_entity_name"] for e in out["edges"]} == set(names)
    assert {e["target_entity_name"] for e in out["edges"]} == {"agents names below"}
    # the container must exist as an entity or the edges have no endpoint
    assert any(e["name"] == "agents names below" for e in out["extracted_entities"])


def test_requires_the_extractor_to_have_seen_the_items():
    """The parse decides they are a LIST; the extractor decides they are ENTITIES. Requiring both
    means a mis-parse of prose cannot mint nodes on its own."""
    receipt = CombinedExtractionReceipt()
    out = _sanitize_combined_payload(_payload(["Admon"]), receipt, ROSTER)
    assert receipt.list_membership_edges_added == 0
    assert out["edges"] == []


def test_never_competes_with_relations_the_model_did_state():
    """If the turn stated real relations they are its truth; a synthetic membership edge must not
    be added alongside them."""
    receipt = CombinedExtractionReceipt()
    stated = [{
        "relation_type": "WORKS_SHIFT", "source_entity_name": "Admon",
        "target_entity_name": "Night Shift", "fact": "Admon works nights",
    }]
    out = _sanitize_combined_payload(
        _payload(["Admon", "Magdy", "Ehab", "Sara", "Night Shift"], stated), receipt, ROSTER)
    assert receipt.list_membership_edges_added == 0
    assert all(e["relation_type"] != "MEMBER_OF" for e in out["edges"])


def test_prose_produces_no_membership_edges_end_to_end():
    receipt = CombinedExtractionReceipt()
    prose = "user: here is the thing:\nI went to the store and bought milk\nthen home\nit was fine"
    out = _sanitize_combined_payload(_payload(["store", "milk"]), receipt, prose)
    assert receipt.list_membership_edges_added == 0
    assert out["edges"] == []


# ------------------------------------------------------------------ shape parity with real edges
#
# These tests exist because the rest of this file has a self-consistency trap: the implementation and
# its expected output were written together, so a wrong edge shape would satisfy both. The guard is to
# compare a SYNTHETIC edge against one produced from a MODEL-supplied row through the same sanitizer,
# rather than against a literal written by hand.

def test_synthetic_edge_has_the_same_field_set_as_a_model_edge():
    """A synthetic membership edge must be indistinguishable in SHAPE from a real one. Missing
    `episode_indices` happens to survive today only because graphiti's pydantic default is [0];
    relying on that couples us to an upstream default that is free to change."""
    receipt = CombinedExtractionReceipt()
    names = ["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]
    synthetic = _sanitize_combined_payload(_payload(names), receipt, ROSTER)["edges"]

    model_row = [{
        "relation_type": "WORKS_SHIFT", "source_entity_name": "Admon",
        "target_entity_name": "Night Shift", "fact": "Admon works nights",
    }]
    real = _sanitize_combined_payload(
        _payload(["Admon", "Night Shift"], model_row), CombinedExtractionReceipt(),
        "user: Admon works nights",
    )["edges"]

    assert synthetic and real
    assert set(synthetic[0].keys()) == set(real[0].keys())


def test_synthetic_edge_carries_episode_indices():
    """graphiti maps an edge to its source episode and picks its reference time from this field."""
    receipt = CombinedExtractionReceipt()
    out = _sanitize_combined_payload(
        _payload(["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]), receipt, ROSTER)
    assert all(e.get("episode_indices") == [0] for e in out["edges"])


def test_synthetic_edge_survives_the_real_sanitizer():
    """Round-trip: feeding a synthetic edge back through _sanitize_combined_edge must not drop it,
    which is what would happen if any indispensable field were missing or blank."""
    from menhir.infrastructure.graphiti_extraction_patches import _sanitize_combined_edge
    receipt = CombinedExtractionReceipt()
    out = _sanitize_combined_payload(
        _payload(["Admon", "Magdy", "Ehab", "Sara", "Mostafa", "Nemr", "Adam"]), receipt, ROSTER)
    for edge in out["edges"]:
        assert _sanitize_combined_edge(dict(edge)) is not None
