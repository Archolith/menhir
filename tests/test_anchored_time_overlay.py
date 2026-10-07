"""valid_at overlay rules (P1 plan sections 6 and 10) as a table over the pure ``plan_overlay``."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, _place, compute, plan_overlay

pytestmark = pytest.mark.unit

SPEECH = date(2024, 2, 14)
LAST_MONTH = {"expression": "last month", "basis": "speech_relative", "kind": "point_event",
              "calendar": {"which": "last", "unit": "month"}}  # window 2024-01-01..2024-01-31
MID = datetime(2024, 1, 16, tzinfo=timezone.utc)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def edge(valid_at=None, invalid_at=None, uuid="e0", fact="The user moved."):
    return EdgeTimeInput(uuid=uuid, fact=fact, valid_at=valid_at, invalid_at=invalid_at)


@pytest.mark.parametrize(
    ("item", "valid_at", "invalid_at", "reason", "new_valid_at"),
    [
        # Graphiti None + closed non-plan window: write the midpoint
        (LAST_MONTH, None, None, "written", MID),
        # Graphiti inside the window: keep it
        (LAST_MONTH, utc("2024-01-20T15:00:00"), None, "graphiti_inside_window", None),
        (LAST_MONTH, utc("2024-01-31T23:59:00"), None, "graphiti_inside_window", None),
        # naive and non-UTC values are compared as UTC dates
        (LAST_MONTH, datetime(2024, 1, 20), None, "graphiti_inside_window", None),
        (LAST_MONTH, datetime(2024, 2, 1, 1, 0, tzinfo=timezone(timedelta(hours=5))), None,
         "graphiti_inside_window", None),
        # Graphiti at its speech-date default (in UTC or in the speech tz): write the midpoint
        (LAST_MONTH, utc("2024-02-14T10:00:00"), None, "written", MID),
        (LAST_MONTH, datetime(2024, 2, 14, 23, 0, tzinfo=timezone(timedelta(hours=-5))), None, "written", MID),
        # Graphiti resolved its own date outside the window: keep it (L3 L18.0, held-out 3 K19.0)
        (LAST_MONTH, utc("2023-12-31T23:00:00"), None, "graphiti_resolved", None),
        (LAST_MONTH, utc("2024-03-01T00:00:00"), utc("2024-03-05T00:00:00"), "graphiti_resolved", None),
        # plan: keep Graphiti
        ({**LAST_MONTH, "kind": "plan"}, None, None, "plan", None),
        # undated, vague, guard-dropped, no item: keep Graphiti (incl. None)
        ({"expression": None, "basis": "none"}, None, None, "undated", None),
        ({"expression": "recently", "basis": "vague"}, utc("2024-02-14T00:00:00"), None, "undated", None),
        ({**LAST_MONTH, "basis": "none", "calendar": None, "guard": "clause"}, None, None, "guard", None),
        (None, utc("2024-02-14T00:00:00"), None, "no_item", None),
        # unplaceable or open window: keep Graphiti
        ({"basis": "speech_relative", "offset": None, "calendar": None}, None, None, "open_or_unplaceable", None),
        ({"basis": "explicit_date", "date": "sometime"}, None, None, "open_or_unplaceable", None),
        # invalid_at at or before the new value: no inverted interval
        (LAST_MONTH, None, utc("2024-01-10T00:00:00"), "would_invert_interval", None),
        (LAST_MONTH, None, MID, "would_invert_interval", None),
        (LAST_MONTH, None, datetime(2024, 1, 10), "would_invert_interval", None),
        (LAST_MONTH, None, utc("2024-01-16T00:00:01"), "written", MID),
        (LAST_MONTH, utc("2024-02-14T00:00:00"), utc("2024-03-05T00:00:00"), "written", MID),
        # explicit date that changes the month its expression names: unplaceable
        ({"expression": "on Feb 10", "basis": "explicit_date", "date": "--03-10"}, None, None,
         "open_or_unplaceable", None),
        # a month the date dropped is put back from the expression
        ({"expression": "on February 10th", "basis": "explicit_date", "date": "---10"}, None, None,
         "written", datetime(2024, 2, 10, tzinfo=timezone.utc)),
        ({"expression": "in May 2019", "basis": "explicit_date", "date": "2019"}, None, None,
         "written", datetime(2019, 5, 16, tzinfo=timezone.utc)),
        # ...but only with the year in the expression too
        ({"expression": "in May", "basis": "explicit_date", "date": "2019"}, None, None,
         "written", datetime(2019, 7, 2, tzinfo=timezone.utc)),
        ({"expression": "on February 10th", "basis": "explicit_date", "date": "--02-10"}, None, None,
         "written", datetime(2024, 2, 10, tzinfo=timezone.utc)),
        ({"expression": "on the 10th", "basis": "explicit_date", "date": "---10"}, None, None,
         "written", datetime(2024, 2, 10, tzinfo=timezone.utc)),
        # two candidate days ("last Tuesday" said on Wednesday 02-14): no midpoint between them
        ({"expression": "last Tuesday", "basis": "speech_relative", "kind": "point_event",
          "calendar": {"which": "last", "unit": "weekday", "name": "tuesday"}}, None, None, "two_options", None),
        ({"expression": "last Friday", "basis": "speech_relative", "kind": "point_event",
          "calendar": {"which": "last", "unit": "weekday", "name": "friday"}}, None, None,
         "written", datetime(2024, 2, 9, tzinfo=timezone.utc)),
    ],
)
def test_overlay_table(item, valid_at, invalid_at, reason, new_valid_at) -> None:
    items = {0: dict(item)} if item is not None else {}
    (result,) = plan_overlay([edge(valid_at, invalid_at)], items, SPEECH)
    assert result.reason == reason
    assert result.new_valid_at == new_valid_at
    assert result.written is (new_valid_at is not None)
    assert result.graphiti_valid_at == valid_at


@pytest.mark.parametrize(
    ("calendar", "expected"),
    [
        # count = the n-th unit back or ahead (SPEECH is Wed 2024-02-14, week of Mon 02-12)
        ({"which": "last", "unit": "week", "count": 2}, (date(2024, 1, 29), date(2024, 2, 4))),
        ({"which": "last", "unit": "week", "count": 6}, (date(2024, 1, 1), date(2024, 1, 7))),
        ({"which": "last", "unit": "weekend", "count": 2}, (date(2024, 2, 3), date(2024, 2, 4))),
        ({"which": "last", "unit": "day", "count": 3}, (date(2024, 2, 11), date(2024, 2, 11))),
        ({"which": "last", "unit": "month", "count": 2}, (date(2023, 12, 1), date(2023, 12, 31))),
        ({"which": "next", "unit": "month", "count": 2}, (date(2024, 4, 1), date(2024, 4, 30))),
        ({"which": "last", "unit": "year", "count": 2}, (date(2022, 1, 1), date(2022, 12, 31))),
        # "this" ignores count; a missing count is 1; an unreadable one is not guessed
        ({"which": "this", "unit": "week", "count": 3}, (date(2024, 2, 12), date(2024, 2, 18))),
        ({"which": "last", "unit": "week", "count": "x"}, None),
        ({"which": "last", "unit": "week", "count": 1.5}, None),
        ({"which": "last", "unit": "week", "count": True}, None),
        ({"which": "last", "unit": "week", "count": "2"}, (date(2024, 1, 29), date(2024, 2, 4))),
        ({"which": "last", "unit": "week", "count": None}, (date(2024, 2, 5), date(2024, 2, 11))),
        # more seasons back than the calendar holds: unplaceable, not an IndexError
        ({"which": "last", "unit": "season", "name": "summer", "count": 40}, None),
    ],
)
def test_calendar_count(calendar, expected) -> None:
    it = {"expression": "x", "basis": "speech_relative", "kind": "point_event", "calendar": calendar}
    assert compute(SPEECH, {0: it}, 0) == expected


MINUS5 = timezone(timedelta(hours=-5))


@pytest.mark.parametrize(
    ("valid_at", "speech_tz", "reason"),
    [
        # speech 2024-02-14 22:00-05:00, stored by Graphiti as 02-15 03:00 UTC: its default, so write
        (utc("2024-02-15T03:00:00"), MINUS5, "written"),
        (utc("2024-02-15T03:00:00"), None, "graphiti_resolved"),  # no speech tz: UTC only, as before
        # 2024-01-31 evening local, stored as 02-01 UTC: inside "last month" in the speech tz
        (utc("2024-02-01T03:00:00"), MINUS5, "graphiti_inside_window"),
        (utc("2024-02-01T03:00:00"), None, "graphiti_resolved"),
        # a real other date stays Graphiti's
        (utc("2024-02-16T12:00:00"), MINUS5, "graphiti_resolved"),
    ],
)
def test_graphiti_value_read_in_the_speech_timezone(valid_at, speech_tz, reason) -> None:
    (result,) = plan_overlay([edge(valid_at)], {0: dict(LAST_MONTH)}, SPEECH, speech_tz)
    assert result.reason == reason
    assert result.written is (reason == "written")


def test_midpoint_is_midnight_utc_for_odd_and_single_day_windows() -> None:
    items = {
        0: {"basis": "explicit_date", "kind": "point_event", "date": "2023-05-06"},
        1: {"basis": "speech_relative", "kind": "point_event", "calendar": {"which": "last", "unit": "week"}},
        2: {"basis": "explicit_date", "kind": "state", "date": "2019"},
    }
    results = plan_overlay([edge(uuid=f"e{i}") for i in range(3)], items, SPEECH)
    assert [r.new_valid_at for r in results] == [
        datetime(2023, 5, 6, tzinfo=timezone.utc),
        datetime(2024, 2, 8, tzinfo=timezone.utc),  # 02-05..02-11
        datetime(2019, 7, 2, tzinfo=timezone.utc),  # 01-01 + 182 days
    ]
    assert all(r.new_valid_at.tzinfo is timezone.utc for r in results)


def test_result_records_the_contract_fields() -> None:
    items = {
        0: {"expression": "last Thursday", "basis": "speech_relative", "kind": "point_event",
            "calendar": {"which": "last", "unit": "weekday", "name": "thursday"}},
        1: {"expression": "two days after the outage", "basis": "event_anchored", "kind": "point_event",
            "offset": {"amount": 2, "unit": "day", "direction": "after", "approx": False},
            "anchor": {"fact": 0, "event": None}},
        2: {"expression": "a week after the move", "basis": "event_anchored", "kind": "plan",
            "offset": {"amount": 1, "unit": "week", "direction": "after"}, "anchor": {"fact": None, "event": "move"}},
        3: {"expression": "right after", "basis": "event_anchored", "kind": "point_event",
            "offset": {"amount": 0, "unit": "day", "direction": "after"}, "anchor": {"fact": 3, "event": None}},
    }
    edges = [edge(uuid=f"e{i}", fact=f"fact {i}") for i in range(4)]
    r0, r1, r2, r3 = plan_overlay(edges, items, SPEECH)
    assert (r0.edge_uuid, r0.fact_index, r0.granularity) == ("e0", 0, "day")
    assert (r0.window_start, r0.window_end) == (date(2024, 2, 8), date(2024, 2, 8))
    assert r0.anchor_ref is None and r0.anchor_offset == items[0]["calendar"]
    assert r1.anchor_ref == "e0" and r1.anchor_offset == items[1]["offset"]
    assert r1.new_valid_at == datetime(2024, 2, 10, tzinfo=timezone.utc)
    assert r2.anchor_ref == "move" and r2.reason == "open_or_unplaceable" and r2.planned_window is None
    assert r3.anchor_ref is None and r3.reason == "open_or_unplaceable"  # self-anchor


def test_plan_keeps_its_window_for_p2() -> None:
    plan = {**LAST_MONTH, "kind": "plan", "calendar": {"which": "next", "unit": "month"}}
    (result,) = plan_overlay([edge()], {0: plan}, SPEECH)
    assert result.reason == "plan" and not result.written
    assert result.planned_window == (date(2024, 3, 1), date(2024, 3, 31))


def test_overlay_is_pure_and_deterministic() -> None:
    items = {0: dict(LAST_MONTH), 1: {"basis": "none"}}
    snapshot = copy.deepcopy(items)
    edges = [edge(uuid="a"), edge(utc("2024-01-05T00:00:00"), uuid="b")]
    first = plan_overlay(edges, items, SPEECH)
    second = plan_overlay(edges, items, SPEECH)
    assert first == second
    assert items == snapshot
    assert edges[1].valid_at == utc("2024-01-05T00:00:00")


def test_items_beyond_the_edge_list_are_ignored() -> None:
    results = plan_overlay([edge()], {0: dict(LAST_MONTH), 7: dict(LAST_MONTH)}, SPEECH)
    assert len(results) == 1 and results[0].written


LAST_TUESDAY = {"expression": "last Tuesday", "basis": "speech_relative", "kind": "point_event",
                "calendar": {"which": "last", "unit": "weekday", "name": "tuesday"}}  # 02-06 or 02-13


def _after(anchor, amount=1, unit="day", **extra):
    return {"expression": "after that", "basis": "event_anchored", "kind": "point_event",
            "offset": {"amount": amount, "unit": unit, "direction": "after", "approx": False},
            "anchor": {"fact": anchor, "event": None}, **extra}


def test_ambiguity_propagates_through_anchors() -> None:
    # review case 1: "one day after" a two-candidate "last Tuesday" is itself two candidates
    items = {0: dict(LAST_TUESDAY), 1: _after(0), 2: _after(1, amount=2)}
    r0, r1, r2 = plan_overlay([edge(uuid=f"e{i}") for i in range(3)], items, SPEECH)
    assert (r0.reason, r1.reason, r2.reason) == ("two_options", "ambiguous_anchor", "ambiguous_anchor")
    assert not (r0.written or r1.written or r2.written)
    # a calendar step from the ambiguous anchor ("the following month") is not written either
    items[1] = {**_after(0), "offset": None, "calendar": {"which": "this", "unit": "week", "later": True}}
    assert plan_overlay([edge(uuid=f"e{i}") for i in range(2)], {0: items[0], 1: items[1]},
                        SPEECH)[1].written is False
    # an unambiguous anchor still writes ("last Friday" said on Wednesday = 02-09)
    items = {0: {**LAST_TUESDAY, "calendar": {"which": "last", "unit": "weekday", "name": "friday"}},
             1: _after(0)}
    assert plan_overlay([edge(uuid="a"), edge(uuid="b")], items, SPEECH)[1].new_valid_at == \
        datetime(2024, 2, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("anchor", [99, 1, -1, True])
def test_anchor_outside_the_fact_list_is_never_used(anchor) -> None:
    # review case 2: a returned row 99 (or any index past the facts) must not date fact 0
    items = {0: _after(anchor, amount=2), 1: {"basis": "explicit_date", "date": "2024-01-01"},
             99: {"basis": "explicit_date", "date": "2024-01-01"},
             -1: {"basis": "explicit_date", "date": "2024-01-01"}}
    (result,) = plan_overlay([edge()], items, SPEECH)
    assert result.reason == "open_or_unplaceable" and not result.written
    assert compute(SPEECH, items, 0, n_facts=1) is None


@pytest.mark.parametrize(
    "offset",
    [
        {"amount": 1, "unit": "hour", "direction": "after"},      # review case 3
        {"amount": 1, "unit": "minute", "direction": "before"},
        {"amount": 1, "unit": None, "direction": "after"},
        {"amount": 1, "unit": "day", "direction": "sideways"},
        {"amount": 1, "unit": "day"},
        {"amount": "two", "unit": "day", "direction": "after"},
        {"amount": -2, "unit": "day", "direction": "after"},
        {"amount": True, "unit": "day", "direction": "after"},
        {"amount": float("nan"), "unit": "day", "direction": "after"},
    ],
)
def test_unusable_offsets_keep_graphiti(offset) -> None:
    yesterday = {"basis": "speech_relative", "kind": "point_event", "calendar": {"which": "last", "unit": "day"}}
    anchored = {0: yesterday, 1: {**_after(0), "offset": offset}}
    r = plan_overlay([edge(uuid="a"), edge(uuid="b")], anchored, SPEECH)[1]
    assert r.reason == "open_or_unplaceable" and not r.written
    (s,) = plan_overlay([edge()], {0: {"basis": "speech_relative", "kind": "point_event", "offset": offset}},
                        SPEECH)
    assert s.reason == "open_or_unplaceable" and not s.written


def test_later_that_week_after_a_sunday_is_not_written() -> None:
    # review case 4: Sunday 02-11 anchor; the week has no days left
    items = {0: {"basis": "explicit_date", "kind": "point_event", "date": "2024-02-11"},
             1: {**_after(0), "offset": None, "calendar": {"which": "this", "unit": "week", "later": True}}}
    r = plan_overlay([edge(uuid="a"), edge(uuid="b")], items, SPEECH)[1]
    assert r.reason == "open_or_unplaceable" and not r.written


def _on(expression, value, kind="point_event"):
    return {"expression": expression, "basis": "explicit_date", "kind": kind, "date": value}


@pytest.mark.parametrize(
    ("item", "reason", "new_valid_at"),
    [
        # gate run 1 R29.2: "on February 5th" said Feb 3 as a past state -> was written a year back
        (_on("on February 19th", "--02-19", "state"), "year_ambiguous", None),
        # gate run 1 R17.1: "in June" said May 15 as a state -> was written as the prior June
        (_on("in March", "--03", "state"), "year_ambiguous", None),
        # other occurrence within a month (31 days) vs just beyond it (32 days)
        (_on("on March 16th", "--03-16"), "year_ambiguous", None),
        (_on("on March 17th", "--03-17"), "written", utc("2023-03-17T00:00:00")),
        # far other occurrence: the kind's past placement stands (held-out 2 G08: May 1st said Nov 29)
        (_on("on May 1st", "--05-01"), "written", utc("2023-05-01T00:00:00")),
        # the past occurrence is the nearer one: unchanged
        (_on("on January 20th", "--01-20"), "written", utc("2024-01-20T00:00:00")),
        # day of month only: "on the 20th" as past said the 14th -> Jan 20 while Feb 20 is 6 days off
        (_on("on the 20th", "---20"), "year_ambiguous", None),
        # a full date is never year-ambiguous
        (_on("on February 19th, 2023", "2023-02-19"), "written", utc("2023-02-19T00:00:00")),
    ],
)
def test_yearless_date_with_a_near_other_occurrence_is_not_written(item, reason, new_valid_at) -> None:
    (r,) = plan_overlay([edge()], {0: item}, SPEECH)
    assert (r.reason, r.new_valid_at) == (reason, new_valid_at)
    assert r.written is (reason == "written")


def test_yearless_plan_with_a_just_past_occurrence_is_year_ambiguous() -> None:
    assert _place(SPEECH, {0: _on("on February 10th", "--02-10", "plan")}, 0, 1)[1] == "year_ambiguous"
    assert _place(SPEECH, {0: _on("on February 10th", "--02-10", "plan")}, 0, 1)[0] == \
        (date(2025, 2, 10), date(2025, 2, 10))
    assert _place(SPEECH, {0: _on("in December", "--12", "plan")}, 0, 1)[1] is None


def test_yearless_holiday_with_a_near_other_occurrence_is_not_written() -> None:
    # Presidents' Day 2024 is 02-19, five days after the speech date; past kind picks 2023-02-20
    item = {"expression": "on Presidents Day", "basis": "event_anchored", "kind": "point_event",
            "offset": {"amount": 0, "unit": "day", "direction": "after", "approx": False},
            "anchor": {"fact": None, "event": "presidents day"}}
    (r,) = plan_overlay([edge()], {0: item}, SPEECH)
    assert r.reason == "year_ambiguous" and not r.written
    # "last Presidents Day" names the year: written
    (r,) = plan_overlay([edge()], {0: {**item, "anchor": {"fact": None, "event": "last presidents day"}}}, SPEECH)
    assert r.written
    # MLK Day (2024-01-15) is the nearer occurrence: written
    (r,) = plan_overlay([edge()], {0: {**item, "anchor": {"fact": None, "event": "mlk day"}}}, SPEECH)
    assert r.written and r.new_valid_at.date() == date(2024, 1, 15)


def test_year_ambiguity_propagates_through_anchors() -> None:
    items = {0: _on("on February 19th", "--02-19"), 1: _after(0)}
    r0, r1 = plan_overlay([edge(uuid="a"), edge(uuid="b")], items, SPEECH)
    assert (r0.reason, r1.reason) == ("year_ambiguous", "ambiguous_anchor")
    assert not (r0.written or r1.written)
