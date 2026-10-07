"""Deterministic calendar of the anchored-time resolver (ported from the P0 prototype)."""
from __future__ import annotations

from datetime import date

import pytest

from menhir.infrastructure.anchored_time import _add_months, _calendar, _explicit, compute

pytestmark = pytest.mark.unit

REF = date(2024, 2, 14)  # Wednesday, leap year


def D(text: str) -> date:
    return date.fromisoformat(text)


def W(start: str | None, end: str | None):
    return (D(start) if start else None, D(end) if end else None)


@pytest.mark.parametrize(
    ("cal", "expected"),
    [
        ({"which": "last", "unit": "day"}, W("2024-02-13", "2024-02-13")),
        ({"which": "this", "unit": "day"}, W("2024-02-14", "2024-02-14")),
        ({"which": "next", "unit": "day"}, W("2024-02-15", "2024-02-15")),
        ({"which": "last", "unit": "week"}, W("2024-02-05", "2024-02-11")),
        ({"which": "this", "unit": "week"}, W("2024-02-12", "2024-02-18")),
        ({"which": "next", "unit": "week"}, W("2024-02-19", "2024-02-25")),
        ({"which": "last", "unit": "weekend"}, W("2024-02-10", "2024-02-11")),
        ({"which": "this", "unit": "weekend"}, W("2024-02-17", "2024-02-18")),
        ({"which": "next", "unit": "weekend"}, W("2024-02-24", "2024-02-25")),
        ({"which": "last", "unit": "weekday", "name": "friday"}, W("2024-02-09", "2024-02-09")),
        # one or two days back is ambiguous: this week's or last week's
        ({"which": "last", "unit": "weekday", "name": "tuesday"}, W("2024-02-06", "2024-02-13")),
        ({"which": "last", "unit": "weekday", "name": "wednesday"}, W("2024-02-07", "2024-02-07")),
        ({"which": "next", "unit": "weekday", "name": "friday"}, W("2024-02-16", "2024-02-23")),
        ({"which": "this", "unit": "weekday", "name": "Friday"}, W("2024-02-16", "2024-02-16")),
        ({"which": "past", "unit": "weekday", "name": "saturday"}, W("2024-02-10", "2024-02-10")),
        ({"which": "upcoming", "unit": "weekday", "name": "monday"}, W("2024-02-19", "2024-02-19")),
        ({"which": "last", "unit": "month"}, W("2024-01-01", "2024-01-31")),
        ({"which": "this", "unit": "month"}, W("2024-02-01", "2024-02-29")),
        ({"which": "next", "unit": "month"}, W("2024-03-01", "2024-03-31")),
        ({"which": "last", "unit": "quarter"}, W("2023-10-01", "2023-12-31")),
        ({"which": "last", "unit": "quarter", "count": 2}, W("2023-07-01", "2023-09-30")),
        ({"which": "this", "unit": "quarter"}, W("2024-01-01", "2024-03-31")),
        ({"which": "next", "unit": "quarter"}, W("2024-04-01", "2024-06-30")),
        ({"which": "last", "unit": "year"}, W("2023-01-01", "2023-12-31")),
        ({"which": "next", "unit": "year"}, W("2025-01-01", "2025-12-31")),
        ({"which": "last", "unit": "season", "name": "summer"}, W("2023-06-01", "2023-08-31")),
        ({"which": "last", "unit": "season", "name": "summer", "count": 2}, W("2022-06-01", "2022-08-31")),
        ({"which": "last", "unit": "season", "name": "winter"}, W("2022-12-01", "2023-02-28")),
        ({"which": "this", "unit": "season", "name": "winter"}, W("2023-12-01", "2024-02-29")),
        ({"which": "next", "unit": "season", "name": "spring"}, W("2024-03-01", "2024-05-31")),
        ({"which": "upcoming", "unit": "season", "name": "autumn"}, W("2024-09-01", "2024-11-30")),
        ({"which": "sometime", "unit": "day"}, None),
        ({"which": "last", "unit": "fortnight"}, None),
        ({"which": "last", "unit": "weekday", "name": "funday"}, None),
        ({"which": "last", "unit": "week", "count": "many"}, W("2024-02-05", "2024-02-11")),
    ],
)
def test_calendar_units(cal, expected) -> None:
    assert _calendar(REF, cal) == expected


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("2023-05-06", "point_event", W("2023-05-06", "2023-05-06")),
        ("2023-05", "point_event", W("2023-05-01", "2023-05-31")),
        ("2019", "state", W("2019-01-01", "2019-12-31")),
        ("--03", "point_event", W("2023-03-01", "2023-03-31")),  # never adds a future year for the past
        ("--01", "point_event", W("2024-01-01", "2024-01-31")),
        ("--01", "plan", W("2025-01-01", "2025-01-31")),
        ("--02", "plan", W("2024-02-01", "2024-02-29")),
        ("---20", "point_event", W("2024-01-20", "2024-01-20")),
        ("---20", "plan", W("2024-02-20", "2024-02-20")),
        ("---30", "point_event", W("2024-01-30", "2024-01-30")),  # February has no 30th
        ("--03-07", "point_event", W("2023-03-07", "2023-03-07")),
        ("03-07", "plan", W("2024-03-07", "2024-03-07")),
        ("-03-07", "point_event", W("2023-03-07", "2023-03-07")),
        ("--02-29", "plan", W("2024-02-29", "2024-02-29")),
        ("--02-29", "point_event", None),  # 2024-02-29 is after speech, 2023 has none
        ("2023-02-30", "point_event", None),
        ("March", "point_event", None),
        ("", "point_event", None),
        (None, "point_event", None),
    ],
)
def test_explicit_dates(value, kind, expected) -> None:
    assert _explicit(REF, value, kind) == expected


def test_add_months_clamps_month_end_and_leap_day() -> None:
    assert _add_months(D("2024-03-31"), -1) == D("2024-02-29")
    assert _add_months(D("2023-03-31"), -1) == D("2023-02-28")
    assert _add_months(D("2024-01-31"), 1) == D("2024-02-29")
    assert _add_months(D("2024-02-29"), 12) == D("2025-02-28")
    assert _add_months(D("2024-01-15"), -13) == D("2022-12-15")


def _sr(**offset):
    return {"basis": "speech_relative", "kind": "point_event", "offset": offset}


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (_sr(amount=3, unit="week", direction="before", approx=False), W("2024-01-21", "2024-01-27")),
        (_sr(amount=3, unit="week", direction="before", approx=True), W("2024-01-17", "2024-01-31")),
        (_sr(amount=2, unit="month", direction="before"), W("2023-12-04", "2023-12-24")),
        (_sr(amount=1, unit="year", direction="before", approx=True), W("2022-02-14", "2024-02-14")),
        (_sr(amount=2, unit="day", direction="after"), W("2024-02-16", "2024-02-16")),
        (_sr(amount=None, unit="day", direction="before"), None),
        (_sr(amount=2, unit="hour", direction="before"), None),
        ({"basis": "speech_relative", "offset": None, "calendar": None}, None),
        ({"basis": "explicit_date", "kind": "plan", "date": "--03"}, W("2024-03-01", "2024-03-31")),
        ({"basis": "vague", "expression": "recently"}, None),
        ({"basis": "none"}, None),
    ],
)
def test_compute_single_fact(item, expected) -> None:
    assert compute(REF, {0: item}, 0) == expected


def test_compute_missing_item_is_none() -> None:
    assert compute(REF, {}, 0) is None


def _anchored(anchor, **rest):
    return {"basis": "event_anchored", "kind": "point_event", "anchor": {"fact": anchor, "event": None}, **rest}


LAST_THURSDAY = {"basis": "speech_relative", "kind": "point_event",
                 "calendar": {"which": "last", "unit": "weekday", "name": "thursday"}}


def test_event_anchored_offset_follows_anchor() -> None:
    items = {0: _anchored(1, offset={"amount": 2, "unit": "day", "direction": "after", "approx": False}),
             1: LAST_THURSDAY}
    assert compute(REF, items, 1) == W("2024-02-08", "2024-02-08")
    assert compute(REF, items, 0) == W("2024-02-10", "2024-02-10")


def test_event_anchored_without_amount_is_open() -> None:
    before = {0: LAST_THURSDAY, 1: _anchored(0, offset={"amount": None, "direction": "before"})}
    after = {0: LAST_THURSDAY, 1: _anchored(0, offset={"amount": None, "direction": "after"})}
    assert compute(REF, before, 1) == (None, D("2024-02-08"))
    assert compute(REF, after, 1) == (D("2024-02-08"), None)


def test_event_anchored_unknown_unit_defaults_to_day() -> None:
    items = {0: LAST_THURSDAY, 1: _anchored(0, offset={"amount": 1, "unit": "hour", "direction": "after"})}
    assert compute(REF, items, 1) == W("2024-02-09", "2024-02-09")


def test_event_anchored_calendar_relative_to_anchor() -> None:
    anchor = {"basis": "explicit_date", "kind": "point_event", "date": "2024-02-06"}  # a Tuesday
    later = {0: anchor, 1: _anchored(0, calendar={"which": "this", "unit": "week", "later": True})}
    following = {0: anchor, 1: _anchored(0, calendar={"which": "next", "unit": "month"})}
    bad = {0: anchor, 1: _anchored(0, calendar={"which": "whenever", "unit": "month"})}
    assert compute(REF, later, 1) == W("2024-02-07", "2024-02-11")
    assert compute(REF, following, 1) == W("2024-03-01", "2024-03-31")
    assert compute(REF, bad, 1) is None


def test_event_anchored_later_past_unit_end_runs_a_week() -> None:
    anchor = {"basis": "explicit_date", "kind": "point_event", "date": "2024-02-11"}  # a Sunday
    items = {0: anchor, 1: _anchored(0, calendar={"which": "this", "unit": "week", "later": True})}
    assert compute(REF, items, 1) == W("2024-02-12", "2024-02-18")


def test_event_anchored_rejects_self_bad_and_open_anchors() -> None:
    off = {"amount": 1, "unit": "day", "direction": "after"}
    assert compute(REF, {0: _anchored(0, offset=off)}, 0) is None
    assert compute(REF, {0: _anchored("zero", offset=off)}, 0) is None
    assert compute(REF, {0: _anchored(None, offset=off)}, 0) is None
    assert compute(REF, {0: LAST_THURSDAY, 1: _anchored(0)}, 1) is None  # no offset, no calendar
    open_anchor = {0: LAST_THURSDAY, 1: _anchored(0, offset={"amount": None, "direction": "after"}),
                   2: _anchored(1, offset=off)}
    assert compute(REF, open_anchor, 2) is None


def test_anchor_recursion_depth_is_bounded() -> None:
    off = {"amount": 1, "unit": "day", "direction": "after", "approx": False}
    items = {0: {"basis": "explicit_date", "kind": "point_event", "date": "2024-01-01"}}
    for i in range(1, 6):
        items[i] = _anchored(i - 1, offset=off)
    assert compute(REF, items, 3) == W("2024-01-04", "2024-01-04")
    assert compute(REF, items, 4) is None
    cycle = {0: _anchored(1, offset=off), 1: _anchored(0, offset=off)}
    assert compute(REF, cycle, 0) is None
