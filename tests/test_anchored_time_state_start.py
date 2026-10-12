"""State-start guard: a time is written only when it dates the fact's own start.

Failure shapes from the frozen eval runs (K04.3, G17.1, G28.0, R29.2), rebuilt on new text: a
causal "since", a state given a co-mentioned event's time, a model year, and a time from another
sentence. The look-alikes that must still write (temporal "since", durations, "started ... ago",
anchors) come from the same runs (L00.2, K08.3, R26.0, R20.2).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import time
from types import SimpleNamespace

import pytest

from menhir.domain.event_time import rendered_event_time
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


@pytest.mark.parametrize(("turn", "fact", "expr", "offset", "written"), [
    ("I have worked as a nurse for 9 years.", "The user works as a nurse.", "for 9 years", _ago(9, "year"),
     _day(2014, 5, 22)),
    ("I've been at Novalink about 4 years now.", "The user works at Novalink.", "about 4 years now",
     _ago(4, "year"), _day(2019, 5, 22)),
    ("I think it all started about 2 months ago when I joined a league.", "The user is getting fitter.",
     "about 2 months ago", _ago(2, "month"), _day(2023, 3, 22)),
    ("My knee started hurting three weeks ago.", "The user's knee hurts.", "three weeks ago", _ago(3, "week"),
     _day(2023, 5, 1)),
    ("Three weeks ago, I started physical therapy.", "The user is doing physical therapy.", "three weeks ago",
     _ago(3, "week"), _day(2023, 5, 1)),
    ("I started yoga and meditation last month.", "The user practices yoga.", "last month",
     _ago(1, "month"), _day(2023, 4, 22)),
])
def test_state_start_markers_keep_the_write(turn, fact, expr, offset, written) -> None:
    items = {0: _item(0, expr, "speech_relative", "state", offset=offset)}
    marked, results = _run(turn, [fact], items, date(2023, 5, 22))
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
        7: _item(7, "my sister", "explicit_date", "state", date_="--05"),
    }
    before = json.loads(json.dumps(items))
    assert state_start_guard(turn, facts, items) == []
    assert json.loads(json.dumps(items)) == before


# ---------------------------------------------------------------- review counterexamples (PR #274)


@pytest.mark.parametrize("main_clause", ["I have a long commute", "I have to drive an hour each way"])
def test_have_without_a_participle_is_not_perfect(main_clause) -> None:
    turn = f"Since I started at the clinic on 2/15, {main_clause}."
    facts = ["The user started at the clinic on 2/15.", "The user commutes."]
    items = {
        0: _item(0, "on 2/15", "explicit_date", "point_event", date_="--02-15"),
        1: _item(1, "Since I started at the clinic on 2/15", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [1] and items[1]["guard"] == "causal_since"
    assert not results[1].written


def test_a_perfect_inside_the_leading_since_clause_is_not_the_main_clause() -> None:
    turn = "Since the workshop had ended early, I'm heading to the gym."
    facts = ["The user is heading to the gym."]
    items = {0: _item(0, "since the workshop", "event_anchored", "state", offset=SINCE_0,
                      anchor={"fact": None, "event": "the workshop"})}
    marked, _ = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [0] and items[0]["guard"] == "causal_since"


def test_ever_before_an_expression_that_starts_at_since() -> None:
    turn = "My knee hurts ever since the half-marathon on April 2nd."
    facts = ["The user ran a half-marathon on April 2nd.", "The user's knee hurts."]
    items = {
        0: _item(0, "on April 2nd", "explicit_date", "point_event", date_="--04-02"),
        1: _item(1, "since the half-marathon", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 5, 1))
    assert marked == [] and items[1]["basis"] == "event_anchored"
    assert results[1].new_valid_at == _day(2023, 4, 2)


def test_a_start_word_about_another_clause_event_does_not_date_the_state() -> None:
    turn = "I started fixing the fence three weeks ago and the goats are safe now that it's done."
    facts = ["The user started fixing the fence three weeks ago.", "The goats are safe."]
    items = {
        0: _item(0, "three weeks ago", "speech_relative", "point_event", offset=_ago(3, "week")),
        1: _item(1, "three weeks ago", "speech_relative", "state", offset=_ago(3, "week")),
    }
    marked, results = _run(turn, facts, items, date(2023, 6, 15))
    assert marked == [1] and items[1]["guard"] == "state_start"
    assert results[0].written and not results[1].written


def test_a_start_that_refers_back_still_dates_the_state() -> None:
    turn = "I've been trying to get fitter lately, and it all started about 2 months ago when I joined a league."
    facts = ["The user has been trying to get fitter lately."]
    items = {0: _item(0, "about 2 months ago", "speech_relative", "state", offset=_ago(2, "month"))}
    marked, results = _run(turn, facts, items, date(2023, 5, 22))
    assert marked == []
    assert results[0].new_valid_at == _day(2023, 3, 22)


def test_a_repeated_expression_is_judged_at_every_occurrence() -> None:
    turn = ("My knee started hurting two weeks ago. "
            "I repaired the chicken coop two weeks ago, and the hens are laying again.")
    facts = ["The user repaired the chicken coop.", "The hens are laying again."]
    items = {
        0: _item(0, "two weeks ago", "speech_relative", "point_event", offset=_ago(2, "week")),
        1: _item(1, "two weeks ago", "speech_relative", "state", offset=_ago(2, "week")),
    }
    marked, results = _run(turn, facts, items, date(2023, 6, 15))
    assert marked == [1] and not results[1].written


def test_a_curly_apostrophe_in_the_turn_still_matches_the_expression() -> None:
    turn = f"Since I started at Mike{chr(0x2019)}s shop on 2/15, I{chr(0x2019)}m hoping to learn welding."
    facts = ["The user started at Mike's shop on 2/15.", "The user hopes to learn welding."]
    items = {
        0: _item(0, "on 2/15", "explicit_date", "point_event", date_="--02-15"),
        1: _item(1, "Since I started at Mike's shop on 2/15", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [1] and items[1]["guard"] == "causal_since"
    assert not results[1].written


@pytest.mark.parametrize(("turn", "expr", "item"), [
    ("We're hosting my parents for Christmas.", "for Christmas",
     {"date_": "--12-25"}),
    ("I'll have the new car a week from now.", "a week from now",
     {"offset": {"amount": 1, "unit": "week", "direction": "after", "approx": False}}),
])
def test_for_and_now_mark_a_start_only_as_a_span_to_now(turn, expr, item) -> None:
    basis = "explicit_date" if "date_" in item else "speech_relative"
    items = {0: _item(0, expr, basis, "state", **item)}
    marked, results = _run(turn, ["The user has a state."], items, date(2023, 6, 1))
    assert marked == [0] and not results[0].written


@pytest.mark.parametrize("main_clause", [
    "I've got a long commute",
    "I've got to drive an hour",
    "I have mixed feelings about it",
    "I have limited options at work",
])
def test_possession_and_ed_adjectives_are_not_perfect(main_clause) -> None:
    turn = f"Since the meeting on 3/4 went badly, {main_clause}."
    facts = ["The meeting on 3/4 went badly.", "The user is unhappy at work."]
    items = {
        0: _item(0, "on 3/4", "explicit_date", "point_event", date_="--03-04"),
        1: _item(1, "Since the meeting on 3/4 went badly", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [1] and not results[1].written


@pytest.mark.parametrize("main_clause", ["I've driven to the coast twice", "I've hardly ever missed a class"])
def test_irregular_participles_and_two_adverbs_are_perfect(main_clause) -> None:
    turn = f"Since I bought the car on 3/4, {main_clause}."
    facts = ["The user bought a car on 3/4.", "The user drives a lot."]
    items = {
        0: _item(0, "on 3/4", "explicit_date", "point_event", date_="--03-04"),
        1: _item(1, "Since I bought the car on 3/4", "event_anchored", "state", offset=SINCE_0,
                 anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 5, 1))
    assert marked == [] and results[1].new_valid_at == _day(2023, 3, 4)


@pytest.mark.parametrize("turn", [
    "Since the gym on 3/4 had closed early I'm running outside.",
    "I'm running outside, since the gym on 3/4 had closed early.",
    "Honestly, since the gym on 3/4 had closed early, I'm running outside.",
    "Since the gym on 3/4, which had closed early, I'm running outside.",
])
def test_a_perfect_inside_the_since_clause_never_makes_it_temporal(turn) -> None:
    facts = ["The user is running outside."]
    items = {0: _item(0, "since the gym on 3/4", "event_anchored", "state", offset=SINCE_0,
                      anchor={"fact": None, "event": "the gym"})}
    marked, _ = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [0] and items[0]["guard"] == "causal_since"


@pytest.mark.parametrize(("turn", "fact"), [
    ("I started a new job last week. My back is sore.", "The user's back is sore."),
    ("It started raining last week, and my sister is visiting.", "The user's sister is visiting."),
    ("I started a new job last week.", "The user is employed."),
])
def test_a_start_word_needs_to_be_about_the_fact(turn, fact) -> None:
    items = {0: _item(0, "last week", "speech_relative", "state", calendar={"which": "last", "unit": "week"})}
    marked, results = _run(turn, [fact], items, date(2023, 6, 15))
    assert marked == [0] and not results[0].written


@pytest.mark.parametrize("expr", [
    "for the past couple of weeks", "for two and a half years", "for 2.5 years", "for half a year",
    "for a while", "for the past few months",
])
def test_durations_running_to_now_keep_the_write(expr) -> None:
    turn = f"I've been learning Spanish {expr}."
    items = {0: _item(0, expr, "speech_relative", "state", offset=_ago(2, "week"))}
    marked, results = _run(turn, ["The user is learning Spanish."], items, date(2023, 6, 15))
    assert marked == [] and results[0].written


@pytest.mark.parametrize(("turn", "expr", "item"), [
    ("I've lived here since 2/15. My rent is due on 12/15.", "2/15", {"date_": "--02-15"}),
    ("I've been on this diet since last week. I went hiking last weekend.", "last week",
     {"calendar": {"which": "last", "unit": "week"}}),
])
def test_an_expression_inside_a_longer_word_is_not_an_occurrence(turn, expr, item) -> None:
    basis = "explicit_date" if "date_" in item else "speech_relative"
    items = {0: _item(0, expr, basis, "state", **item)}
    marked, results = _run(turn, ["The user has a state."], items, date(2023, 6, 15))
    assert marked == [] and results[0].written


def test_the_strictest_occurrence_wins_whatever_its_position() -> None:
    # The later occurrence alone would date the state; the earlier one does not, so no write.
    turn = ("I repaired the chicken coop two weeks ago, and the hens are laying again. "
            "The hens started laying again two weeks ago.")
    facts = ["The user repaired the chicken coop.", "The hens are laying again."]
    items = {
        0: _item(0, "two weeks ago", "speech_relative", "point_event", offset=_ago(2, "week")),
        1: _item(1, "two weeks ago", "speech_relative", "state", offset=_ago(2, "week")),
    }
    marked, results = _run(turn, facts, items, date(2023, 6, 15))
    assert marked == [1] and not results[1].written


def test_a_dotted_capital_i_does_not_hide_the_expression() -> None:
    turn = f"Since I moved to {chr(0x130)}zmir on 3/4, I'm hoping to learn Turkish."
    expr = f"Since I moved to {chr(0x130)}zmir on 3/4"
    facts = ["The user moved on 3/4.", "The user hopes to learn Turkish."]
    items = {
        0: _item(0, "on 3/4", "explicit_date", "point_event", date_="--03-04"),
        1: _item(1, expr, "event_anchored", "state", offset=SINCE_0, anchor={"fact": 0, "event": None}),
    }
    marked, results = _run(turn, facts, items, date(2023, 3, 20))
    assert marked == [1] and not results[1].written


def test_many_repeated_occurrences_stay_cheap() -> None:
    turn = ("2023-05-01 ok. " * 400)[:4000]
    items = {k: _item(k, "2023", "explicit_date", "state", date_="2023") for k in range(40)}
    started = time.perf_counter()
    state_start_guard(turn, [f"Fact {k}." for k in range(40)], items)
    assert time.perf_counter() - started < 2.0  # quadratic sentence rescans took ~9 s


@pytest.mark.parametrize(("valid_at", "shown"), [
    ("2023-05-25T00:00:00Z", "event time unknown (said 2023-06-15)"),  # Graphiti made the same copy
    ("2023-06-15T00:00:00Z", "event time unknown (said 2023-06-15)"),  # speech-date default
    ("2022-01-10T00:00:00Z", "2022-01-10"),  # Graphiti's own date, without the borrowed expression
])
def test_render_never_shows_the_borrowed_time(valid_at, shown) -> None:
    row = {"time_basis": "speech_relative", "time_expression": "three weeks ago", "time_kind": "state",
           "time_granularity": "day", "time_window_start": "2023-05-22", "time_window_end": "2023-05-28",
           "time_outcome": "state_start", "time_speech_date": "2023-06-15", "valid_at": valid_at}
    assert rendered_event_time(row) == shown


def test_overlay_never_shows_a_state_start_window_even_when_graphiti_agrees() -> None:
    # Graphiti's own date inside the borrowed window must not turn it into graphiti_inside_window.
    items = {0: {**_item(0, "two weeks ago", "speech_relative", "state", offset=_ago(2, "week")),
                 "guard": "state_start"}}
    edges = [EdgeTimeInput(uuid="e0", fact="The hens are laying again.", valid_at=_day(2023, 6, 1),
                           invalid_at=None)]
    (result,) = plan_overlay(edges, items, date(2023, 6, 15))
    assert result.reason == "state_start" and not result.written


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
