"""valid_at overlay rules (P1 plan sections 6 and 10) as a table over the pure ``plan_overlay``."""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

import pytest

from menhir.infrastructure.anchored_time import EdgeTimeInput, plan_overlay

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
        # Graphiti outside (e.g. the speech-date default): write the midpoint
        (LAST_MONTH, utc("2024-02-14T10:00:00"), None, "written", MID),
        (LAST_MONTH, utc("2023-12-31T23:00:00"), None, "written", MID),
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
        (LAST_MONTH, utc("2024-03-01T00:00:00"), utc("2024-03-05T00:00:00"), "written", MID),
    ],
)
def test_overlay_table(item, valid_at, invalid_at, reason, new_valid_at) -> None:
    items = {0: dict(item)} if item is not None else {}
    (result,) = plan_overlay([edge(valid_at, invalid_at)], items, SPEECH)
    assert result.reason == reason
    assert result.new_valid_at == new_valid_at
    assert result.written is (new_valid_at is not None)
    assert result.graphiti_valid_at == valid_at


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
