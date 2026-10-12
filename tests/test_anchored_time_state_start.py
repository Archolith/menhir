"""State-start guard: a time is written only when it dates the fact's own start.

Failure shapes from the frozen eval runs (K04.3, G17.1, G28.0, R29.2), rebuilt on new text: a
causal "since", a state given a co-mentioned event's time, a model year, and a time from another
sentence. The look-alikes that must still write (temporal "since", durations, "started ... ago",
anchors) come from the same runs (L00.2, K08.3, R26.0, R20.2).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, clause_guard, plan_overlay, state_start_guard

pytestmark = pytest.mark.unit


def _item(i, expr, basis, kind, *, offset=None, calendar=None, date_=None, anchor=None):
    return {"i": i, "expression": expr, "basis": basis, "kind": kind, "offset": offset,
            "calendar": calendar, "date": date_, "anchor": anchor}


def _ago(amount, unit):
    return {"amount": amount, "unit": unit, "direction": "before", "approx": False}


SINCE_0 = {"amount": 0, "unit": "day", "direction": "after", "approx": False}


def _run(turn, facts, items, speech):
    """Production order: clause guard, state-start guard, overlay over Graphiti's speech-date default."""
    clause_guard(turn, facts, items)
    marked = state_start_guard(turn, facts, items)
    at = datetime(speech.year, speech.month, speech.day, tzinfo=timezone.utc)
    edges = [EdgeTimeInput(uuid=f"e{i}", fact=f, valid_at=at, invalid_at=None) for i, f in enumerate(facts)]
    return marked, plan_overlay(edges, items, speech)


def _day(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ---------------------------------------------------------------- "since"


def test_causal_since_is_dropped_for_every_kind() -> None:
    turn = "Since I signed up at the climbing gym on 3/4, I'm hoping to find a weekday partner."
    facts = ["The user signed up at the climbing gym on 3/4.",
             "The user hopes to find a weekday climbing partner.",
             "The user is looking for a partner."]
    items = {
        0: _item(0, "on 3/4", "explicit_date", "point_event", date_="--03-04"),
        1: _item(1, "Since I signed up at the climbing gym on 3/4", "event_anchored", "state",
                 offset=SINCE_0, anchor={"fact": 0, "event": None}),
        2: _item(2, "Since I signed up at the climbing gym on 3/4", "event_anchored", "point_event",
                 offset=SINCE_0, anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [1, 2]
    assert items[1]["basis"] == items[2]["basis"] == "none"
    assert items[1]["guard"] == items[2]["guard"] == "causal_since"
    assert [r.written for r in results] == [True, False, False]
    assert results[0].new_valid_at == _day(2023, 3, 4)


@pytest.mark.parametrize("main_clause", [
    "I've been practicing my presentations every week",
    "I\u2019ve been practicing my presentations every week",
    "I haven't missed a single practice session",
    "my team has noticed the difference",
])
def test_since_with_a_perfect_main_clause_is_temporal(main_clause) -> None:
    turn = f"Since I attended a speaking workshop on January 10th, {main_clause}."
    facts = ["The user attended a speaking workshop on January 10th.", "The user practices presenting."]
    items = {
        0: _item(0, "on January 10th", "explicit_date", "point_event", date_="--01-10"),
        1: _item(1, "Since I attended a speaking workshop on January 10th", "event_anchored", "state",
                 offset=SINCE_0, anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 1))
    assert marked == []
    assert results[1].written and results[1].new_valid_at == _day(2023, 1, 10)


def test_ever_since_is_temporal_without_a_perfect() -> None:
    turn = "Ever since I adopted Biscuit in May, I walk two miles every morning."
    facts = ["The user adopted Biscuit in May.", "The user walks two miles every morning."]
    items = {
        0: _item(0, "in May", "explicit_date", "point_event", date_="--05"),
        1: _item(1, "Ever since I adopted Biscuit in May", "event_anchored", "state",
                 offset=SINCE_0, anchor={"fact": 0, "event": None}),
    }
    marked, _ = _run(turn, facts, items, date(2023, 9, 1))
    assert marked == []
    assert items[1]["basis"] == "event_anchored"


def test_a_date_inside_a_causal_since_clause_dates_that_clause_event() -> None:
    turn = "Since I moved to Denver in May, I'm looking for hiking groups."
    facts = ["The user moved to Denver in May.", "The user is looking for hiking groups."]
    items = {
        0: _item(0, "Since I moved to Denver in May", "explicit_date", "point_event", date_="--05"),
        1: _item(1, "Since I moved to Denver in May", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 9, 1))
    assert marked == [1] and items[1]["guard"] == "causal_since"
    assert results[0].written and results[0].window_start == date(2023, 5, 1)
    assert not results[1].written


def test_a_date_right_after_since_is_a_since_phrase() -> None:
    turn = "I've been starting seeds under grow lights since February 20th."
    facts = ["The user starts seeds under grow lights."]
    items = {0: _item(0, "February 20th", "explicit_date", "state", date_="--02-20")}
    marked, results = _run(turn, facts, items, date(2023, 4, 10))
    assert marked == []
    assert results[0].new_valid_at == _day(2023, 2, 20)

    causal = "Since February 20th is my deadline, I need a plan."
    items = {0: _item(0, "February 20th", "explicit_date", "state", date_="--02-20")}
    marked, results = _run(causal, ["The user needs a plan."], items, date(2023, 2, 1))
    assert marked == [0] and items[0]["guard"] == "causal_since"
    assert not results[0].written


# ---------------------------------------------------------------- states


def test_state_given_a_co_mentioned_event_time_is_not_written() -> None:
    turn = "I repaired the chicken coop two weeks ago, and the hens are laying again."
    facts = ["The user repaired the chicken coop.", "The hens are laying again."]
    items = {
        0: _item(0, "two weeks ago", "speech_relative", "point_event", offset=_ago(2, "week")),
        1: _item(1, "two weeks ago", "speech_relative", "state", offset=_ago(2, "week")),
    }
    marked, results = _run(turn, facts, items, date(2023, 6, 15))
    assert marked == [1]
    assert items[1]["guard"] == "state_start" and items[1]["basis"] == "speech_relative"
    assert results[0].written and results[0].new_valid_at == _day(2023, 6, 1)
    assert not results[1].written and results[1].reason == "state_start"


def test_model_year_is_not_a_state_start() -> None:
    turn = "What tires would you recommend for my 2016 Subaru Outback?"
    facts = ["The user owns a 2016 Subaru Outback."]
    items = {0: _item(0, "2016", "explicit_date", "state", date_="2016")}
    marked, results = _run(turn, facts, items, date(2023, 6, 28))
    assert marked == [0]
    assert not results[0].written and results[0].reason == "state_start"


def test_time_from_another_sentence_is_not_written_on_any_speech_date() -> None:
    # The year rule blocks this shape only when the speech date is within 31 days of the date.
    turn = ("I'll ask Priya about the bakery she took me to at the fair on March 5th. "
            "She said they make amazing sourdough.")
    facts = ["Priya took the user to a bakery at the fair on March 5th.",
             "The bakery makes amazing sourdough."]
    items = {
        0: _item(0, "on March 5th", "explicit_date", "point_event", date_="--03-05"),
        1: _item(1, "on March 5th", "explicit_date", "state", date_="--03-05"),
    }
    marked, results = _run(turn, facts, items, date(2023, 6, 1))
    assert marked == [1]
    assert results[0].new_valid_at == _day(2023, 3, 5)
    assert not results[1].written and results[1].reason == "state_start"


@pytest.mark.parametrize(("turn", "expr", "offset", "written"), [
    ("I have worked as a nurse for 9 years.", "for 9 years", _ago(9, "year"), _day(2014, 5, 22)),
    ("I've been at Novalink about 4 years now.", "about 4 years now", _ago(4, "year"), _day(2019, 5, 22)),
    ("I think it all started about 2 months ago when I joined a league.", "about 2 months ago",
     _ago(2, "month"), _day(2023, 3, 22)),
    ("My knee started hurting three weeks ago.", "three weeks ago", _ago(3, "week"), _day(2023, 5, 1)),
])
def test_state_start_markers_keep_the_write(turn, expr, offset, written) -> None:
    items = {0: _item(0, expr, "speech_relative", "state", offset=offset)}
    marked, results = _run(turn, ["The user has a state."], items, date(2023, 5, 22))
    assert marked == []
    assert results[0].new_valid_at == written


def test_a_marked_anchor_still_places_its_dependents() -> None:
    turn = "I've been posting in a hiking group on Facebook since I joined it yesterday."
    facts = ["The user joined a hiking group on Facebook yesterday.", "The user posts in the hiking group."]
    items = {
        0: _item(0, "yesterday", "speech_relative", "state", calendar={"which": "last", "unit": "day"}),
        1: _item(1, "since I joined it", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 5, 25))
    assert marked == [0] and items[0]["guard"] == "state_start"
    assert not results[0].written and results[0].reason == "state_start"
    assert results[1].written and results[1].new_valid_at == _day(2023, 5, 24)


def test_items_outside_the_turn_or_undated_are_left_alone() -> None:
    turn = "Can you suggest a gift for my sister?"
    facts = ["In early May, the user bought a lamp.", "The user wants a gift idea."]
    items = {
        0: _item(0, "In early May", "explicit_date", "state", date_="--05"),
        1: _item(1, None, "none", "state"),
        7: _item(7, "for my sister", "explicit_date", "state", date_="--05"),
    }
    before = json.loads(json.dumps(items))
    assert state_start_guard(turn, facts, items) == []
    assert json.loads(json.dumps(items)) == before


# ---------------------------------------------------------------- production hook


@pytest.mark.asyncio
async def test_hook_does_not_write_a_borrowed_state_time() -> None:
    pytest.importorskip("graphiti_core")
    from graphiti_core.edges import EntityEdge

    import menhir.infrastructure.graphiti_extraction_policy as policy
    from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver

    speech = datetime(2023, 6, 15, 18, 30, tzinfo=timezone.utc)
    turn = "I repaired the chicken coop two weeks ago, and the hens are laying again."
    output = json.dumps({"facts": [
        _item(0, "two weeks ago", "speech_relative", "point_event", offset=_ago(2, "week")),
        _item(1, "two weeks ago", "speech_relative", "state", offset=_ago(2, "week")),
    ], "missing_events": []})

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=output))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    resolver = AnchoredTimeResolver(client, base_url="https://api.openai.com/v1", model="gpt-6-luna",
                                    timeout_s=5.0)
    edges = [
        EntityEdge(uuid=f"e{i}", group_id="ns", source_node_uuid="n-user", target_node_uuid=f"n-{i}",
                   created_at=speech, name="RELATES_TO", fact=fact, episodes=["ep-1"], valid_at=speech,
                   invalid_at=None, reference_time=speech)
        for i, fact in enumerate(["The user repaired the chicken coop.", "The hens are laying again."])
    ]
    receipt = policy.begin_extraction_receipt("ep-1", turn, turn_role="user")
    try:
        await policy._apply_anchored_time(resolver, receipt, SimpleNamespace(uuid="g-1", valid_at=speech), edges)
        report = receipt.anchored_time
    finally:
        policy.clear_extraction_receipt()
    assert report.status == "ok" and report.guard_drops == (1,)
    assert edges[0].valid_at == _day(2023, 6, 1)
    assert edges[1].valid_at == speech
    assert [r.reason for r in report.results] == ["written", "state_start"]
