"""#564 part 2: newer mentions of a different value for a View's slot, found in user turns.

The 852ce960 case: the user stated a $350,000 pre-approval on 2023-08-11, and on 2023-11-30 asked
"remember when I got pre-approved for $400,000 from Wells Fargo?". The perceiver rightly does not
extract the presupposed $400,000; recall should still show it beside the View, never as its value.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from menhir.domain.newer_mentions import attribute_stems, find_amounts, newer_mentions

STATED = (
    "I'm actually buying a $325,000 house, and I got pre-approved for $350,000 from Wells Fargo. "
    "What would be the estimated closing costs for my situation?"
)
ASKED = (
    "I'm planning to move into my new home soon and I need to set up cable and TV services. "
    "By the way, it's been a long process, but it'll be worth it. "
    "Do you remember when I got pre-approved for $400,000 from Wells Fargo?"
)
VIEW = dict(attribute="mortgage_preapproval_amount", value_kind="money", unit="usd",
            value="350000", valid_at="2023-08-11T05:59:00Z")


def episode(content, *, when="2023-11-30T12:13:00Z", source="user", uuid="e-asked"):
    return {"uuid": uuid, "content": content, "reference_time": when, "source": source}


def test_852ce960_question_mention_is_found():
    out = newer_mentions(**VIEW, episodes=[episode(STATED, when="2023-08-11T05:59:00Z",
                                                   uuid="e-stated"), episode(ASKED)])
    assert len(out) == 1
    m = out[0]
    assert m.value == "400000"
    assert m.mood == "question"
    assert m.said_at == "2023-11-30T12:13:00Z"
    assert m.episode_uuid == "e-asked"
    assert "pre-approved for $400,000" in m.quote


@pytest.mark.parametrize("attribute", ["preapproval_amount", "pre_approval_amount",
                                       "mortgage_preapproval_amount"])
def test_attribute_spellings_all_match(attribute):
    out = newer_mentions(**{**VIEW, "attribute": attribute}, episodes=[episode(ASKED)])
    assert [m.value for m in out] == ["400000"]


def test_clause_window_keeps_the_house_price_out():
    # The STATED sentence again, newer than a View holding another value: only the clause that
    # says "pre-approved" counts, not the $325,000 house in the same sentence.
    view = {**VIEW, "value": "999999"}
    out = newer_mentions(**view, episodes=[episode(STATED)])
    assert [m.value for m in out] == ["350000"], "the $325,000 house is not a preapproval amount"


def test_stated_mood():
    text = "Update: I got pre-approved for $410,000 now."
    out = newer_mentions(**VIEW, episodes=[episode(text)])
    assert [(m.value, m.mood) for m in out] == [("410000", "stated")]


@pytest.mark.parametrize("ep", [
    episode(ASKED, when="2023-08-01T00:00:00Z"),        # older than the View
    episode(ASKED, when="2023-08-11T05:59:00Z"),        # same instant
    episode(ASKED, when="not a time"),                  # unparseable time never matches
    episode(ASKED, when=None),
    episode(ASKED, source="claude-code"),               # agent-written, not the user
    episode(ASKED.replace("$400,000", "$350,000")),     # same value
    episode(ASKED.replace("$400,000", "€400,000")),  # other unit
    episode("I spent $400,000 on the renovation, remember?"),  # unrelated clause
])
def test_non_matches(ep):
    assert newer_mentions(**VIEW, episodes=[ep]) == ()


@pytest.mark.parametrize("override", [
    {"value_kind": "count"}, {"value": "n/a"}, {"valid_at": None}, {"unit": ""},
    {"attribute": "amount"},
])
def test_view_without_a_usable_slot_yields_nothing(override):
    assert newer_mentions(**{**VIEW, **override}, episodes=[episode(ASKED)]) == ()


def test_duplicate_turns_collapse():
    out = newer_mentions(**VIEW, episodes=[episode(ASKED, uuid="a"), episode(ASKED, uuid="b")])
    assert len(out) == 1


def test_amount_parsing():
    got = [(a.value, a.unit) for a in find_amounts(
        "It was $1,250.50, then $2k, then £3 million, then 400,000 dollars.")]
    assert got == [(Decimal("1250.50"), "usd"), (Decimal("2000"), "usd"),
                   (Decimal("3000000"), "gbp"), (Decimal("400000"), "usd")]


def test_attribute_stems_drop_generic_parts():
    assert "preapprov" in attribute_stems("pre_approval_amount")
    assert attribute_stems("amount") == frozenset()
