"""Event anchors that name a US holiday resolve to that holiday, year taken from the speech date."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, compute, plan_overlay

pytestmark = pytest.mark.unit


def D(text: str) -> date:
    return date.fromisoformat(text)


def item(event, offset=None, calendar=None, kind="point_event", fact=None):
    return {"expression": "x", "basis": "event_anchored", "kind": kind, "offset": offset,
            "calendar": calendar, "date": None, "anchor": {"fact": fact, "event": event}}


WEEK_BEFORE = {"amount": 1, "unit": "week", "direction": "before", "approx": False}
SINCE = {"amount": 0, "unit": "day", "direction": "after", "approx": False}


@pytest.mark.parametrize(
    ("ref", "it", "expected"),
    [
        # L3 L04: "a week before Black Friday" said after it; Black Friday 2023 = 11-24
        ("2023-12-10", item("Black Friday", WEEK_BEFORE), (D("2023-11-14"), D("2023-11-20"))),
        # the derived date, not the holiday, decides the year: Black Friday 2024 (11-29) is still ahead
        ("2024-11-25", item("Black Friday", WEEK_BEFORE), (D("2024-11-19"), D("2024-11-25"))),
        # year boundary: a past Christmas said in January is last year's
        ("2023-01-13", item("Christmas", SINCE, kind="state"), (D("2022-12-25"), D("2022-12-25"))),
        # a plan takes the next occurrence whose window is still ahead
        ("2023-12-30", item("Christmas", {"amount": 2, "unit": "day", "direction": "after"}, kind="plan"),
         (D("2024-12-27"), D("2024-12-27"))),
        ("2024-02-14", item("Easter", WEEK_BEFORE, kind="plan"), (D("2024-03-21"), D("2024-03-27"))),
        # the actual date, not the observed weekday off (2021-07-04 was a Sunday)
        ("2021-08-01", item("the 4th of July", SINCE), (D("2021-07-04"), D("2021-07-04"))),
        ("2023-12-01", item("Cyber Monday", SINCE), (D("2023-11-27"), D("2023-11-27"))),
        # calendar unit relative to the holiday: Thanksgiving 2023 = Thu 11-23
        ("2023-12-10", item("Thanksgiving", calendar={"which": "this", "unit": "weekend"}),
         (D("2023-11-25"), D("2023-11-26"))),
        # aliases are matched after case, apostrophe and period folding
        ("2023-06-01", item("Mother’s Day", SINCE), (D("2023-05-14"), D("2023-05-14"))),
        ("2023-06-01", item("ST. PATRICK'S DAY", SINCE), (D("2023-03-17"), D("2023-03-17"))),
        ("2023-06-01", item("XMAS", SINCE), (D("2022-12-25"), D("2022-12-25"))),
        # no amount: open interval, as for fact anchors
        ("2023-06-01", item("Christmas", {"amount": None, "direction": "before"}), (None, D("2022-12-25"))),
    ],
)
def test_holiday_anchor_window(ref, it, expected) -> None:
    assert compute(D(ref), {0: it}, 0) == expected


@pytest.mark.parametrize(
    "event",
    [
        "Father John's sermon on forgiveness",  # exact match only, never a substring
        "Thanksgiving weekend",  # an interval with no agreed convention
        "the trip",
        None,
        "",
    ],
)
def test_non_holiday_event_stays_unplaceable(event) -> None:
    assert compute(D("2023-12-10"), {0: item(event, WEEK_BEFORE)}, 0) is None


def test_fact_anchor_still_wins_and_bad_anchors_stay_unplaceable() -> None:
    items = {0: {"basis": "explicit_date", "date": "2023-05-06", "kind": "point_event"},
             1: item("Christmas", SINCE, fact=0),
             2: item("Christmas", SINCE, fact=2),
             3: item("Christmas", SINCE, fact="0")}
    assert compute(D("2023-12-10"), items, 1) == (D("2023-05-06"), D("2023-05-06"))
    assert compute(D("2023-12-10"), items, 2) is None
    assert compute(D("2023-12-10"), items, 3) is None


def test_overlay_writes_a_holiday_anchored_fact() -> None:
    edge = EdgeTimeInput(uuid="e0", fact="The user bought a TV.", valid_at=None, invalid_at=None)
    (result,) = plan_overlay([edge], {0: item("Black Friday", WEEK_BEFORE)}, D("2023-12-10"))
    assert result.reason == "written"
    assert result.new_valid_at == datetime(2023, 11, 17, tzinfo=timezone.utc)
    assert result.anchor_ref == "Black Friday"
