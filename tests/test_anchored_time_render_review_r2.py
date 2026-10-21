"""Round-2 render counterexamples: R2-4 derived candidates, R2-2 ambiguous plan years."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from menhir.domain.event_time import EVENT_TIME_PROPERTIES, rendered_event_time
from menhir.infrastructure.anchored_time import AnchoredTimeReport, EdgeTimeInput, plan_overlay
from menhir.infrastructure.anchored_time_persist import contract_rows

pytestmark = pytest.mark.unit

SPEECH = date(2024, 2, 14)  # a Wednesday
LAST_TUESDAY = {"expression": "last Tuesday", "basis": "speech_relative", "kind": "point_event",
                "calendar": {"which": "last", "unit": "weekday", "name": "tuesday"}}


def _utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _rendered(items: dict[int, dict], graphiti: list[date | None], speech: date = SPEECH):
    """plan_overlay -> contract_rows -> stored rows -> rendered_event_time, as in production."""
    edges = [EdgeTimeInput(uuid=f"e{i}", fact="f", valid_at=_utc(g) if g else None,
                           invalid_at=None) for i, g in enumerate(graphiti)]
    results = plan_overlay(edges, items, speech)
    report = AnchoredTimeReport(status="ok", model="m", results=tuple(results))
    out = []
    for result, row in zip(results, contract_rows(report, [e.uuid for e in edges], speech)):
        stored = {k: v for k, v in row.items() if k in EVENT_TIME_PROPERTIES}
        valid_at = result.new_valid_at or result.graphiti_valid_at
        stored["valid_at"] = valid_at.isoformat() if valid_at else None
        out.append((result, rendered_event_time(stored)))
    return out


def _after(offset: dict) -> dict:
    return {"expression": "after the move", "basis": "event_anchored", "kind": "point_event",
            "anchor": {"fact": 0}, "offset": offset}


ONE_DAY = {"amount": 1, "unit": "day", "direction": "after"}


def test_exact_day_after_two_candidate_days_is_two_candidate_days() -> None:
    # R2-4: Graphiti's Feb 14 is the speech-date default, not an independent pick.
    (_, _), (derived, text) = _rendered({0: LAST_TUESDAY, 1: _after(ONE_DAY)},
                                        [date(2024, 2, 13), SPEECH])

    assert derived.ambiguity == "two_options"
    assert derived.new_valid_at is None
    assert text == "2024-02-07 or 2024-02-14 (from 'after the move')"


def test_derived_candidate_graphiti_picked_itself_is_shown() -> None:
    (_, _), (_, text) = _rendered({0: LAST_TUESDAY, 1: _after(ONE_DAY)},
                                  [date(2024, 2, 13), date(2024, 2, 7)])

    assert text == "2024-02-07 (from 'after the move')"


@pytest.mark.parametrize("offset", [
    {"amount": 1, "unit": "day", "direction": "after", "approx": True},
    {"amount": 1, "unit": "week", "direction": "after"},
])
def test_tolerance_from_two_candidate_days_is_not_rendered_as_a_span(offset) -> None:
    (_, _), (derived, text) = _rendered({0: LAST_TUESDAY, 1: _after(offset)},
                                        [date(2024, 2, 13), SPEECH])

    assert derived.ambiguity == "ambiguous_anchor"
    assert ".." not in text and text.startswith("event time unknown")


def test_anchor_on_a_year_ambiguous_date_is_not_rendered() -> None:
    feb5 = {"expression": "on February 5th", "basis": "explicit_date", "kind": "point_event",
            "date": "--02-05"}
    speech = date(2024, 2, 3)  # the past Feb 5 is 2023, but 2024-02-05 is two days away
    (anchor, anchor_text), (derived, text) = _rendered({0: feb5, 1: _after(ONE_DAY)},
                                                       [speech, speech], speech)

    assert anchor.ambiguity == "year_ambiguous" and anchor_text.startswith("event time unknown")
    assert derived.ambiguity == "ambiguous_anchor" and text.startswith("event time unknown")


@pytest.mark.parametrize("speech", [date(2024, 9, 15), date(2024, 12, 20)])
def test_year_ambiguous_plan_is_not_shown_as_next_years_season(speech: date) -> None:
    # R2-2: "this summer" said in September as a plan.
    plan = {"expression": "this summer", "basis": "speech_relative", "kind": "plan",
            "calendar": {"which": "this", "unit": "season", "name": "summer"}}
    ((result, text),) = _rendered({0: plan}, [speech], speech)

    assert result.ambiguity == "year_ambiguous"
    assert text == f"planned, time unknown (said {speech.isoformat()}; 'this summer')"


def test_unambiguous_plan_still_shows_its_window() -> None:
    plan = {"expression": "this summer", "basis": "speech_relative", "kind": "plan",
            "calendar": {"which": "this", "unit": "season", "name": "summer"}}
    ((_, text),) = _rendered({0: plan}, [date(2024, 4, 15)], date(2024, 4, 15))

    assert text == "planned ~2024-06-01..2024-08-31 (from 'this summer')"
