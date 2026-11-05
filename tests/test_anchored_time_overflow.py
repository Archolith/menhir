"""CF-263: an absurd LLM offset/count makes that item unusable, never the whole episode."""
from __future__ import annotations

import logging
from datetime import date

import pytest

from menhir.infrastructure import anchored_time
from menhir.infrastructure.anchored_time import (
    EdgeTimeInput,
    _amount,
    _calendar,
    _place,
    plan_overlay,
)

REF = date(2024, 6, 15)


def _edge(uuid: str) -> EdgeTimeInput:
    return EdgeTimeInput(uuid=uuid, fact="f", valid_at=None, invalid_at=None)


def _off(amount, unit, direction="before") -> dict:
    return {"basis": "speech_relative", "kind": "point_event", "offset": {"amount": amount, "unit": unit, "direction": direction}}


GOOD = _off(2, "day")


@pytest.mark.parametrize("amount,unit", [(1e9, "day"), (1e9, "week"), (1e5, "year"), (1e7, "month"), (1e300, "day")])
def test_amount_rejects_uncalendarable(amount, unit):
    assert _amount({"amount": amount, "unit": unit}) is None


@pytest.mark.parametrize("amount,unit", [(1e9, "day"), (1e5, "year"), (1e7, "month"), (9000, "year")])
def test_speech_relative_offset_unresolved(amount, unit):
    assert _place(REF, {0: _off(amount, unit)}, 0, 1) == (None, None)
    assert _place(REF, {0: _off(amount, unit, "after")}, 0, 1) == (None, None)


@pytest.mark.parametrize("unit,count", [("quarter", 10_000_000), ("year", 100_000), ("month", 10**9), ("day", 10**9),
                                         ("week", 10**9), ("year", 9000)])
def test_calendar_count_unusable(unit, count):
    for which in ("last", "next"):
        assert _calendar(REF, {"which": which, "unit": unit, "count": count}) is None


def test_anchored_offset_overflow_unresolved():
    items = {0: {"basis": "explicit_date", "kind": "point_event", "date": "9999-12-31"},
             1: {"basis": "event_anchored", "kind": "point_event", "anchor": {"fact": 0},
                 "offset": {"amount": 1, "unit": "day", "direction": "after"}}}
    assert _place(REF, items, 1, 2)[0] is None


def test_one_bad_item_does_not_poison_batch():
    edges = [_edge("a"), _edge("b"), _edge("c"), _edge("d")]
    items = {0: _off(1e9, "day"), 1: GOOD, 2: _off(1e5, "year"),
             3: {"basis": "speech_relative", "kind": "point_event", "calendar": {"which": "last", "unit": "quarter", "count": 10_000_000}}}
    res = plan_overlay(edges, items, REF)
    assert [r.reason for r in res] == ["open_or_unplaceable", "written", "open_or_unplaceable", "open_or_unplaceable"]
    assert res[1].new_valid_at is not None


def test_unexpected_item_exception_is_isolated_and_logged(monkeypatch, caplog):
    real = anchored_time._place

    def boom(ref, items, i, n, depth=0):
        if i == 0:
            raise RuntimeError("secret fact text")
        return real(ref, items, i, n, depth)

    monkeypatch.setattr(anchored_time, "_place", boom)
    with caplog.at_level(logging.WARNING):
        res = plan_overlay([_edge("a"), _edge("b")], {0: GOOD, 1: GOOD}, REF)
    assert res[0].reason == "planning_error" and not res[0].written
    assert res[1].written
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1 and "0" in msgs[0] and "RuntimeError" in msgs[0] and "secret" not in msgs[0]
