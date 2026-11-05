"""CF-263 second pass: no single resolver item or edge may turn the episode into internal_error.

Runs the real resolver parse and the real ``_apply_anchored_time`` (offline, fake LLM transport).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

import menhir.infrastructure.anchored_time as at  # noqa: E402
import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time import (  # noqa: E402
    EdgeTimeInput,
    _amount,
    _calendar,
    _shift,
    parse_items,
    plan_overlay,
)
from menhir.infrastructure.anchored_time_attach import parse_answers  # noqa: E402
from menhir.infrastructure.anchored_time_attach_client import AnchoredTimeAttacher  # noqa: E402
from menhir.infrastructure.anchored_time_persist import contract_rows  # noqa: E402
from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver  # noqa: E402

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 6, 15, 12, tzinfo=timezone.utc)
TURN = "I saw it yesterday and again on September 3rd."
GOOD = {"expression": "yesterday", "basis": "speech_relative", "kind": "point_event",
        "calendar": {"which": "last", "unit": "day"}}
YESTERDAY = datetime(2024, 6, 14, tzinfo=timezone.utc)


class _Completions:
    def __init__(self, content: str) -> None:
        self.content = content

    async def create(self, **kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def _resolver(content: str) -> AnchoredTimeResolver:
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(content)))
    return AnchoredTimeResolver(client, base_url="https://api.openai.com/v1", model="m", timeout_s=5.0)


def _content(item0: dict) -> str:
    return json.dumps({"facts": [{"i": 0, **item0}, {"i": 1, **GOOD}], "missing_events": []})


def _edges(valid_at0=None):
    return [SimpleNamespace(uuid="a", fact="The user saw it.", valid_at=valid_at0, invalid_at=None),
            SimpleNamespace(uuid="b", fact="The user saw it again.", valid_at=None, invalid_at=None)]


def _apply(content: str, edges=None, tz=timezone.utc):
    edges = edges if edges is not None else _edges()
    receipt = policy.begin_extraction_receipt("ep-1", TURN, turn_role="user")
    try:
        asyncio.run(policy._apply_anchored_time(
            _resolver(content), receipt, SimpleNamespace(uuid="g-1", valid_at=SPEECH.astimezone(tz)),
            edges, expiry=True))
        return receipt.anchored_time, edges
    finally:
        policy.clear_extraction_receipt()


def _assert_neighbour_kept(report, edges) -> None:
    assert report.status == "ok", (report.status, report.reason)
    assert [r.fact_index for r in report.results][-1] == 1
    assert report.results[-1].reason == "written"
    assert edges[1].valid_at == YESTERDAY


BAD_ITEMS = {
    "long_s_month_word": {"expression": "on ſeptember 3rd", "basis": "explicit_date", "kind": "point_event",
                          "date": "--09-03"},
    "expression_int": {**GOOD, "expression": 5},
    "expression_list": {**GOOD, "expression": ["yesterday"]},
    "basis_list": {**GOOD, "basis": ["speech_relative"]},
    "basis_dict": {**GOOD, "basis": {"x": 1}},
    "kind_list": {**GOOD, "kind": ["state"]},
    "offset_string": {"expression": "yesterday", "basis": "speech_relative", "offset": "two days"},
    "calendar_string": {"expression": "yesterday", "basis": "speech_relative", "calendar": "last week"},
    "anchor_string": {"expression": "later", "basis": "event_anchored", "anchor": "x"},
    "which_list": {"expression": "yesterday", "basis": "speech_relative",
                   "calendar": {"which": ["last"], "unit": "day"}},
    "huge_int_amount": {"expression": "yesterday", "basis": "speech_relative",
                        "offset": {"amount": 10 ** 400, "unit": "day", "direction": "before"}},
}


@pytest.mark.parametrize("item0", list(BAD_ITEMS.values()), ids=list(BAD_ITEMS))
def test_bad_item_fields_leave_the_neighbour_written(item0) -> None:
    report, edges = _apply(_content(item0))
    _assert_neighbour_kept(report, edges)
    assert report.planning_errors == 0
    assert not edges[0].valid_at or edges[0].valid_at.year != 1


@pytest.mark.parametrize("tz,valid_at", [
    (timezone(timedelta(hours=-5)), datetime(1, 1, 1, tzinfo=timezone.utc)),
    (timezone(timedelta(hours=5)), datetime(9999, 12, 31, 23, tzinfo=timezone.utc)),
])
def test_graphiti_valid_at_at_the_date_bounds(tz, valid_at) -> None:
    report, edges = _apply(_content(GOOD), _edges(valid_at), tz)
    _assert_neighbour_kept(report, edges)
    assert report.planning_errors == 0 and len(report.results) == 2


def test_lone_surrogates_never_reach_the_persist_rows() -> None:
    item0 = {"expression": "yesterday\ud83d", "basis": "explicit_date", "kind": "point_event",
             "date": "2024-01-01\ud83d", "anchor": {"fact": None, "event": "trip\ud83d"},
             "offset": {"amount": 1, "unit": "day\ud83d", "direction": "before"}}
    report, edges = _apply(_content(item0))
    _assert_neighbour_kept(report, edges)
    rows = contract_rows(report, ["a", "b"], date(2024, 6, 15))
    assert {r["uuid"] for r in rows} == {"a", "b"}
    for row in rows:
        for value in row.values():
            if isinstance(value, str):
                value.encode("utf-8")  # the Neo4j packer encodes exactly like this


def test_planning_error_is_distinct_logged_without_message_and_not_persisted(monkeypatch, caplog) -> None:
    real = at._place

    def boom(ref, items, i, n, depth=0):
        if i == 0:
            raise RuntimeError("secret fact text")
        return real(ref, items, i, n, depth)

    monkeypatch.setattr(at, "_place", boom)
    with caplog.at_level(logging.DEBUG):
        report, edges = _apply(_content(GOOD))
    assert report.status == "ok" and report.planning_errors == 1
    assert [r.fact_index for r in report.results] == [1]  # no write-once contract for the failed item
    assert {r["uuid"] for r in contract_rows(report, ["a", "b"], date(2024, 6, 15))} == {"b"}
    assert edges[0].valid_at is None and edges[1].valid_at == YESTERDAY
    text = caplog.text
    assert "secret" not in text
    planning = [r.getMessage() for r in caplog.records if "planning_error" in r.getMessage()
                and r.levelno == logging.WARNING and "item 0" in r.getMessage()]
    assert len(planning) == 1 and "RuntimeError" in planning[0] and "anchored_time.py:" in planning[0]


def test_s1_nonscalar_nested_values_stay_unplaceable() -> None:
    for item0 in (
        {"expression": "last week", "basis": "speech_relative", "kind": "point_event",
         "calendar": {"which": "last", "unit": "week", "count": [2]}},
        {"expression": "last week", "basis": "speech_relative", "kind": "point_event",
         "calendar": {"which": "last", "unit": "week", "count": {"n": 2}}},
        {"expression": "the day before christmas", "basis": "event_anchored", "kind": "point_event",
         "anchor": {"fact": [0], "event": "christmas"},
         "offset": {"amount": 1, "unit": "day", "direction": "before"}},
        {"expression": "x", "basis": "event_anchored", "kind": "point_event",
         "anchor": {"fact": 1, "event": None}, "offset": {"amount": [3], "unit": "day", "direction": "after"}},
    ):
        report, edges = _apply(_content(item0))
        assert report.status == "ok"
        assert report.results[0].reason == "open_or_unplaceable", item0
        assert edges[0].valid_at is None and edges[1].valid_at == YESTERDAY


def _ended_edges():
    return [SimpleNamespace(uuid="a", fact="The user saw it.", valid_at=None,
                            invalid_at=datetime(2024, 1, 1, tzinfo=timezone.utc)),
            SimpleNamespace(uuid="b", fact="The user saw it again.", valid_at=None, invalid_at=None)]


@pytest.mark.parametrize("item0", [
    {**GOOD, "kind": "plan", "expression": 5},
    {**GOOD, "kind": "plan", "date": [2024]},
    {**GOOD, "kind": ["plan"]},
], ids=["plan_expression_int", "plan_date_list", "kind_list"])
def test_s2_malformed_item_claims_no_world_end(item0) -> None:
    report, edges = _apply(_content(item0), _ended_edges())
    first = report.results[0]
    assert first.world_end is None and first.reason == "guard" and first.guard == "malformed"
    assert first.kind in ("plan", "none")
    rows = contract_rows(report, ["a", "b"], date(2024, 6, 15))
    assert "time_world_end" not in next(r for r in rows if r["uuid"] == "a")


def test_s4_numeric_date_still_writes() -> None:
    item0 = {"expression": "in 2019", "basis": "explicit_date", "kind": "point_event", "date": 2019}
    report, edges = _apply(_content(item0))
    assert report.results[0].reason == "written" and report.malformed_items == 0
    assert edges[0].valid_at == datetime(2019, 7, 2, tzinfo=timezone.utc)


def test_s5_malformed_items_are_counted_apart_from_no_time(caplog) -> None:
    content = json.dumps({"facts": [{"i": 0, "expression": 5, "basis": "speech_relative"},
                                    {"i": 1, **GOOD}, {"i": 2, "basis": "none"}], "missing_events": []})
    edges = _edges() + [SimpleNamespace(uuid="c", fact="x", valid_at=None, invalid_at=None)]
    with caplog.at_level(logging.WARNING):
        report, _ = _apply(content, edges)
    assert report.malformed_items == 1
    assert [r.reason for r in report.results] == ["guard", "written", "undated"]
    assert any("malformed field(s): expression" in r.getMessage() for r in caplog.records)
    assert "yesterday" not in " ".join(r.getMessage() for r in caplog.records)


def test_s3_guard_failure_is_planning_error_not_a_state_start_contract(monkeypatch) -> None:
    real = at._start_reason

    def boom(turn, low, pos, expr, it, fact):
        if "september" in expr:
            raise RuntimeError("secret fact text")
        return real(turn, low, pos, expr, it, fact)

    monkeypatch.setattr(at, "_start_reason", boom)
    item0 = {"expression": "September 3rd", "basis": "explicit_date", "kind": "point_event", "date": "--09-03"}
    report, edges = _apply(_content(item0))
    assert report.status == "ok" and report.planning_errors == 1
    assert 0 not in report.guard_drops
    assert [r.fact_index for r in report.results] == [1]
    assert {r["uuid"] for r in contract_rows(report, ["a", "b"], date(2024, 6, 15))} == {"b"}
    assert edges[0].valid_at is None and edges[1].valid_at == YESTERDAY


def test_plan_overlay_marks_planning_error_reason(monkeypatch) -> None:
    monkeypatch.setattr(at, "_place", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    edge = EdgeTimeInput(uuid="a", fact="f", valid_at=None, invalid_at=None)
    [res] = plan_overlay([edge], {0: dict(GOOD)}, date(2024, 6, 15), log_errors=False)
    assert res.reason == "planning_error" and not res.written and res.new_valid_at is None


def test_narrow_wrappers_log_at_debug(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="menhir.infrastructure.anchored_time"):
        assert _shift(date(2024, 1, 1), 1e9, "day", 1) is None
    assert any("OverflowError" in r.getMessage() for r in caplog.records)


def test_superscript_index_drops_only_that_item() -> None:
    content = json.dumps({"facts": [{"i": "²", "basis": "none"}, {"i": 1, "basis": "none"}]})
    items, _ = parse_items(content)
    assert set(items) == {1}
    answers = parse_answers(json.dumps({"facts": [
        {"i": "²", "relation": "occurrence", "expression": "x"},
        {"i": "1", "relation": "occurrence", "expression": "x"}]}))
    assert set(answers) == {1}


def test_count_cap_applies_only_when_the_count_is_used() -> None:
    ref = date(2024, 6, 15)
    assert _calendar(ref, {"which": "this", "unit": "week", "count": 3_652_426}) == (
        date(2024, 6, 10), date(2024, 6, 16))
    assert _calendar(ref, {"which": "last", "unit": "week", "count": 3_652_426}) is None


def test_amount_and_calendar_never_raise_on_odd_types() -> None:
    assert _amount({"amount": 10 ** 400, "unit": "day"}) is None
    assert _amount({"amount": 1, "unit": ["day"]}) == 1.0
    assert _calendar(date(2024, 6, 15), {"which": ["last"], "unit": "week"}) is None
    assert _calendar(date(2024, 6, 15), {"which": "last", "unit": ["week"]}) is None
    assert _calendar(date(2024, 6, 15), {"which": "last", "unit": "week", "name": 5}) is None


def test_deep_json_is_a_parse_error_for_resolver_and_attacher() -> None:
    deep = '{"facts": [{"i": 0, "offset": ' + "[" * 200_000 + "]" * 200_000 + "}]}"
    outcome = asyncio.run(_resolver(deep).resolve(TURN, date(2024, 6, 15), ["f"]))
    assert outcome.status == "parse_error"
    attacher = AnchoredTimeAttacher(
        SimpleNamespace(chat=SimpleNamespace(completions=_Completions(deep))),
        base_url="https://api.openai.com/v1", model="m", timeout_s=5.0)
    assert asyncio.run(attacher.attach(TURN, date(2024, 6, 15), ["f"])).status == "parse_error"
