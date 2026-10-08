"""Round-2 calendar counterexamples (R2-2 "this <season>", R2-3 Feb 29 horizon)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, _place, compute, plan_overlay

pytestmark = pytest.mark.unit


def _season(name: str, kind: str) -> dict[int, dict]:
    return {0: {"basis": "speech_relative", "kind": kind, "expression": f"this {name}",
                "calendar": {"which": "this", "unit": "season", "name": name}}}


def _overlay(items: dict[int, dict], speech: date):
    speech_dt = datetime(speech.year, speech.month, speech.day, tzinfo=timezone.utc)
    return plan_overlay([EdgeTimeInput("e", "fact", speech_dt, None)], items, speech)[0]


SUMMER_2024 = (date(2024, 6, 1), date(2024, 8, 31))


@pytest.mark.parametrize(("speech", "kind"), [
    (date(2024, 4, 15), "point_event"),  # R2-2: 47 days before summer, outside the old 31-day check
    (date(2024, 1, 10), "point_event"),
    (date(2024, 9, 15), "plan"),  # R2-2: next year's summer was chosen and shown as the plan
    (date(2024, 12, 20), "plan"),
])
def test_this_summer_against_the_kind_is_year_ambiguous(speech: date, kind: str) -> None:
    decision = _overlay(_season("summer", kind), speech)

    assert decision.new_valid_at is None
    # A plan is never written (reason "plan"); the resolver still marks the year ambiguous.
    assert _place(speech, _season("summer", kind), 0, 1)[1] == "year_ambiguous"
    if kind != "plan":
        assert decision.reason == "year_ambiguous"


@pytest.mark.parametrize(("speech", "kind", "reason"), [
    (date(2024, 4, 15), "plan", "plan"),
    (date(2024, 1, 10), "plan", "plan"),
    (date(2024, 9, 15), "point_event", "written"),
    (date(2024, 12, 20), "point_event", "written"),
    (date(2024, 7, 10), "point_event", "graphiti_inside_window"),
    (date(2024, 7, 10), "plan", "plan"),
])
def test_this_summer_in_the_speech_year_is_dated(speech: date, kind: str, reason: str) -> None:
    decision = _overlay(_season("summer", kind), speech)

    assert (decision.window_start, decision.window_end) == SUMMER_2024
    assert decision.reason == reason
    assert _place(speech, _season("summer", kind), 0, 1)[1] is None


@pytest.mark.parametrize("kind", ["point_event", "plan"])
def test_this_winter_between_two_winters_of_the_year_is_ambiguous(kind: str) -> None:
    # Said 2024-03-15: Dec 2023 - Feb 2024 and Dec 2024 - Feb 2025 both touch 2024.
    assert _place(date(2024, 3, 15), _season("winter", kind), 0, 1)[1] == "year_ambiguous"


def test_this_winter_inside_winter_is_the_current_one() -> None:
    decision = _overlay(_season("winter", "point_event"), date(2024, 1, 20))

    assert (decision.window_start, decision.window_end) == (date(2023, 12, 1), date(2024, 2, 29))


def _leap(kind: str) -> dict[int, dict]:
    return {0: {"basis": "explicit_date", "kind": kind, "expression": "February 29",
                "date": "--02-29"}}


@pytest.mark.parametrize(("speech", "kind", "expected"), [
    (date(2026, 3, 1), "point_event", date(2024, 2, 29)),  # R2-3
    (date(2027, 12, 1), "point_event", date(2024, 2, 29)),
    (date(2025, 3, 1), "plan", date(2028, 2, 29)),  # R2-3
    (date(2024, 3, 1), "plan", date(2028, 2, 29)),
    (date(1903, 6, 1), "point_event", date(1896, 2, 29)),  # 1900 is not a leap year
    (date(1897, 1, 1), "plan", date(1904, 2, 29)),
])
def test_feb_29_reaches_the_nearest_leap_day_in_the_kind_direction(speech, kind, expected) -> None:
    assert compute(speech, _leap(kind), 0) == (expected, expected)


def test_feb_29_at_the_end_of_the_date_range_is_unplaceable_not_an_error() -> None:
    assert compute(date(9999, 3, 1), _leap("plan"), 0) is None
