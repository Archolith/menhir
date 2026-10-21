"""Clause guard: a time distributed over a list must not scope back over earlier members.

Ports every assertion of the P0 prototype's check_guard.py (dev_set3 turns are fixtures).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from menhir.infrastructure.anchored_time import clause_guard

pytestmark = pytest.mark.unit

DEV3 = {t["id"]: t for t in json.loads(
    (Path(__file__).parent / "fixtures" / "anchored_time" / "dev_set3.json").read_text(encoding="utf-8"))}


def _dist(tid, expr, idx, basis="speech_relative"):
    t = DEV3[tid]
    items = {i: {"i": i, "expression": expr, "basis": basis, "kind": "point_event",
                 "calendar": {"which": "last", "unit": "day"}} for i in idx}
    return clause_guard(t["turn"], t["facts"], items)


@pytest.mark.parametrize(
    ("tid", "expr", "idx", "basis", "dropped"),
    [
        # list-final time owned by one member: the others are dropped
        ("F1", "yesterday", [0, 1, 2], "speech_relative", [0, 1]),
        ("F2", "last Sunday", [0, 1, 2], "speech_relative", [0, 1]),
        ("F6", "on January 30th", [0, 1, 2], "explicit_date", [0, 1]),
        ("F8", "back in 2015", [0, 1], "explicit_date", [0]),
        # controls: a time that scopes over several facts is kept
        ("F7", "Last month", [0, 1, 2], "speech_relative", []),  # sentence-initial
        ("F5", "October", [0, 1], "explicit_date", []),  # clause names no member
        ("F3", "back in August", [0, 1], "explicit_date", []),  # anaphoric "That was ..."
        # a single attachment is never touched
        ("F1", "yesterday", [0], "speech_relative", []),
    ],
)
def test_dev3_distribution_cases(tid, expr, idx, basis, dropped) -> None:
    assert _dist(tid, expr, idx, basis) == dropped


def _guard(turn, facts, expr, idx, basis="speech_relative"):
    items = {i: {"expression": expr, "basis": basis} for i in idx}
    return clause_guard(turn, facts, items), items


def test_fact_text_containing_the_expression_keeps_it() -> None:
    dropped, _ = _guard("I read Dune and Emma, and finished Ulysses yesterday.",
                        ["The user read Dune.", "The user finished reading Ulysses yesterday.", "The user read Emma."],
                        "yesterday", (0, 1, 2))
    assert dropped == [0, 2]


def test_elaboration_after_the_dated_clause_inherits_the_time() -> None:
    dropped, _ = _guard("We went to the harvest fair on Saturday, where my daughter won a pie contest.",
                        ["The user went to the harvest fair.", "The user's daughter won a pie contest at the fair."],
                        "on Saturday", (0, 1))
    assert dropped == []


def test_anchor_named_inside_the_expression_keeps_it() -> None:
    dropped, _ = _guard("I bought the lamp from a thrift shop a week before I moved into the flat.",
                        ["The user bought a lamp from a thrift shop.", "The user moved into the flat."],
                        "a week before I moved into the flat", (0, 1), "event_anchored")
    assert dropped == []


def test_abbreviation_is_not_a_sentence_end_and_relative_clause_keeps_antecedent() -> None:
    dropped, _ = _guard("I met Dr. Patel, Dr. Ruiz, and Dr. Okafor, who I saw on Monday.",
                        ["The user met Dr. Patel.", "The user met Dr. Ruiz.", "The user saw Dr. Okafor."],
                        "on Monday", (0, 1, 2))
    assert dropped == [0, 1]


def test_dropped_items_lose_their_time_and_are_marked() -> None:
    t = DEV3["F1"]
    items = {i: {"i": i, "expression": "yesterday", "basis": "speech_relative", "kind": "point_event",
                 "calendar": {"which": "last", "unit": "day"}} for i in (0, 1, 2)}
    original = items[0]
    assert clause_guard(t["turn"], t["facts"], items) == [0, 1]
    assert items[0]["basis"] == "none" and items[0]["guard"] == "clause"
    assert items[0]["calendar"] is None and items[0]["expression"] == "yesterday"
    assert original["basis"] == "speech_relative"  # replaced, not mutated
    assert items[2]["basis"] == "speech_relative" and "guard" not in items[2]


def test_guard_ignores_out_of_range_and_undated_items() -> None:
    turn = "I read Dune and Emma yesterday."
    facts = ["The user read Dune.", "The user read Emma."]
    items = {5: {"expression": "yesterday", "basis": "speech_relative"},
             0: {"expression": "yesterday", "basis": "none"},
             1: {"expression": None, "basis": "speech_relative"}}
    assert clause_guard(turn, facts, items) == []
