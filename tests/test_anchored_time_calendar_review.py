"""Calendar counterexamples from the PR #244 review: fractional offsets, "this <season>", Feb 29."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, compute, plan_overlay

pytestmark = pytest.mark.unit


def _offset(amount, unit: str, kind: str = "point_event") -> dict[int, dict]:
    return {0: {"basis": "speech_relative", "kind": kind, "expression": f"{amount} {unit}s ago",
                "offset": {"amount": amount, "unit": unit, "direction": "before"}}}


def _overlay(items: dict[int, dict], speech: date):
    speech_dt = datetime(speech.year, speech.month, speech.day, tzinfo=timezone.utc)
    return plan_overlay([EdgeTimeInput("e", "fact", speech_dt, None)], items, speech)[0]


def test_one_and_a_half_years_is_eighteen_months() -> None:
    decision = _overlay(_offset(1.5, "year"), date(2024, 7, 1))

    assert decision.reason == "written"
    assert decision.new_valid_at.date() == date(2023, 1, 1)


def test_half_a_month_is_about_fifteen_days_not_zero() -> None:
    start, end = compute(date(2024, 7, 1), _offset(0.5, "month"), 0)

    assert start < date(2024, 6, 20) and end < date(2024, 7, 1)


@pytest.mark.parametrize("amount", [1, 2, 2.0])
def test_whole_offsets_keep_calendar_month_arithmetic(amount) -> None:
    start, end = compute(date(2024, 3, 31), _offset(amount, "month"), 0)
    centre = start + (end - start) / 2

    assert centre == date(2024, 3 - int(amount), 29 if int(amount) == 1 else 31)


def _this_season(kind: str) -> dict[int, dict]:
    return {0: {"basis": "speech_relative", "kind": kind, "expression": "this summer",
                "calendar": {"which": "this", "unit": "season", "name": "summer"}}}


def test_this_summer_said_in_may_is_the_coming_summer_for_a_plan() -> None:
    decision = _overlay(_this_season("plan"), date(2024, 5, 15))

    assert (decision.window_start, decision.window_end) == (date(2024, 6, 1), date(2024, 8, 31))
    assert decision.reason == "plan"


def test_this_summer_said_just_before_summer_is_not_written_as_last_year() -> None:
    decision = _overlay(_this_season("point_event"), date(2024, 5, 15))

    assert decision.new_valid_at is None
    assert decision.reason == "year_ambiguous"


def test_this_summer_said_in_october_is_the_summer_just_gone() -> None:
    decision = _overlay(_this_season("point_event"), date(2024, 10, 2))

    assert (decision.window_start, decision.window_end) == (date(2024, 6, 1), date(2024, 8, 31))
    assert decision.reason == "written"


def test_this_summer_inside_summer_is_the_current_one() -> None:
    assert compute(date(2024, 7, 10), _this_season("plan"), 0) == (date(2024, 6, 1), date(2024, 8, 31))


def _leap(kind: str) -> dict[int, dict]:
    return {0: {"basis": "explicit_date", "kind": kind, "expression": "February 29",
                "date": "--02-29"}}


def test_past_feb_29_from_a_non_leap_year_is_the_previous_leap_day() -> None:
    assert compute(date(2025, 3, 1), _leap("point_event"), 0) == (date(2024, 2, 29),) * 2


def test_planned_feb_29_from_a_leap_year_eve_is_found() -> None:
    assert compute(date(2027, 3, 1), _leap("plan"), 0) == (date(2028, 2, 29),) * 2


def test_invalid_month_day_is_still_unplaceable() -> None:
    bad = {0: {"basis": "explicit_date", "kind": "point_event", "expression": "the 31st",
               "date": "--04-31"}}

    assert compute(date(2025, 6, 1), bad, 0) is None
