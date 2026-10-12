"""Blind attach check (MENHIR_ANCHORED_TIME_ATTACH): prompt blindness, answer parsing, the V1-V3
veto, the plan_overlay ``attach`` argument and the cached client call. Offline, invented turns only.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import inspect
import json
import re
from types import SimpleNamespace

import pytest

from menhir.infrastructure import anchored_time_attach as attach_mod
from menhir.infrastructure.anchored_time import (
    ATTACH_AGREE,
    ATTACH_REJECTED,
    ATTACH_UNVERIFIED,
    EdgeTimeInput,
    plan_overlay,
)
from menhir.infrastructure.anchored_time_attach import (
    ATTACH_PROMPT_VERSION,
    agrees,
    build_messages,
    decide,
    decide_all,
    parse_answers,
)

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 2, 14, 18, 30, tzinfo=timezone.utc)  # a Wednesday
SPEECH_DATE = date(2024, 2, 14)


# ---------------------------------------------------------------- A. blindness


def test_build_messages_signature_has_no_resolver_parameter() -> None:
    assert list(inspect.signature(build_messages).parameters) == ["turn", "speech_date", "facts"]


def test_build_messages_carries_turn_facts_date_and_weekday_deterministically() -> None:
    facts = ["The user adopted a cat.", "The user walks daily."]
    msgs = build_messages("I adopted a cat in March.", "2024-02-14", facts)
    assert [m["role"] for m in msgs] == ["system", "user"]
    user = msgs[1]["content"]
    assert "TURN: I adopted a cat in March." in user
    assert "0. The user adopted a cat.\n1. The user walks daily." in user
    assert "SPEECH_TIME: 2024-02-14 (Wednesday)" in user
    assert build_messages("I adopted a cat in March.", "2024-02-14", facts) == msgs


def _client_module():
    pytest.importorskip("graphiti_core")
    import menhir.infrastructure.anchored_time_attach_client as mod
    return mod


def test_build_request_signature_and_no_resolver_vocabulary() -> None:
    mod = _client_module()
    assert list(inspect.signature(mod.build_request).parameters) == ["model", "turn", "speech_date", "facts"]
    request = mod.build_request("m", "I sold my bike last week.", SPEECH_DATE, ["The user sold a bike."])
    blob = json.dumps(request["messages"])
    for word in ("basis", "explicit_date", "speech_relative", "event_anchored"):
        assert word not in blob


# ---------------------------------------------------------------- B. parse_answers


def _facts_json(*rows) -> str:
    return json.dumps({"facts": list(rows)})


def test_parse_answers_valid_object_normalizes_relation() -> None:
    out = parse_answers(_facts_json(
        {"i": 0, "relation": " Occurrence ", "expression": "last week"},
        {"i": 1, "relation": "NONE", "expression": None},
    ))
    assert out == {0: {"relation": "occurrence", "expression": "last week"},
                   1: {"relation": "none", "expression": None}}


def test_parse_answers_unknown_relation_drops_that_fact_only() -> None:
    out = parse_answers(_facts_json(
        {"i": 0, "relation": "sometime", "expression": "last week"},
        {"i": 1, "relation": "start", "expression": "since May"},
    ))
    assert list(out) == [1]


def test_parse_answers_fact_listed_twice_is_dropped_even_when_entries_agree() -> None:
    out = parse_answers(_facts_json(
        {"i": 0, "relation": "start", "expression": "since May"},
        {"i": 0, "relation": "start", "expression": "since May"},
        {"i": 1, "relation": "none", "expression": None},
    ))
    assert list(out) == [1]


def test_parse_answers_bad_indexes_dropped_digit_string_accepted() -> None:
    out = parse_answers(_facts_json(
        {"i": "x", "relation": "none", "expression": None},
        {"i": -1, "relation": "none", "expression": None},
        {"i": True, "relation": "none", "expression": None},
        {"i": None, "relation": "none", "expression": None},
        {"i": "2", "relation": "start", "expression": "since May"},
    ))
    assert out == {2: {"relation": "start", "expression": "since May"}}


@pytest.mark.parametrize("expression", ["", "   ", None, 5])
def test_parse_answers_blank_expression_is_none(expression) -> None:
    out = parse_answers(_facts_json({"i": 0, "relation": "occurrence", "expression": expression}))
    assert out[0]["expression"] is None


def test_parse_answers_rejects_non_object_and_invalid_json() -> None:
    with pytest.raises(ValueError):
        parse_answers("[1, 2]")
    with pytest.raises(ValueError):
        parse_answers("{not json")


def test_parse_answers_accepts_a_fenced_answer() -> None:
    body = _facts_json({"i": 0, "relation": "occurrence", "expression": "last week"})
    assert parse_answers(f"```json\n{body}\n```") == {0: {"relation": "occurrence", "expression": "last week"}}


# ---------------------------------------------------------------- C. agrees (V1)

TURN_MAYJUNE = "In May I bought a bike and in June I sold it."


@pytest.mark.parametrize(("turn", "resolver", "check", "expected"), [
    ("I sold it two weeks ago.", "two weeks ago", "two weeks ago", True),
    ("I sold it about two weeks ago.", "about two weeks ago", "two weeks ago", True),
    ("I sold it about two weeks ago.", "two weeks ago", "about two weeks ago", True),
    (TURN_MAYJUNE, "In May", "in June", False),
    (TURN_MAYJUNE, "in June", "in May", False),
    ("I sold it last week.", "last week", "in July", False),
    ("I sold it last week.", "in July", "last week", False),
    ("I sold it last week.", None, "last week", False),
    ("I sold it last week.", "last week", None, False),
    ("I sold it last week.", "", "last week", False),
    ("I sold it last week.", "last week", "  ", False),
    ("I sold it Last Week.", "Last Week", "last week", True),
    ("We met at Mom’s Last Week.", "Mom’s Last Week", "mom's last week", True),
    ("I sold it last week and Monday was calm.", "last week and", "week and Monday", False),
])
def test_agrees(turn, resolver, check, expected) -> None:
    assert agrees(turn, resolver, check) is expected


def test_agrees_requires_the_same_position_not_just_the_same_words() -> None:
    # "in May" occurs twice; the contained phrase must overlap the resolver's own occurrence.
    turn = "In May I sold a bike. I will sell a car in May"
    assert agrees(turn, "In May I sold", "in may") is True
    assert agrees(turn, "sell a car in May", "In May I sold") is False


# ---------------------------------------------------------------- D. decide


TURN_D = "I sold the bike two weeks ago."


@pytest.mark.parametrize("answer", [None, {}])
def test_decide_missing_answer_is_unverified(answer) -> None:
    assert decide(TURN_D, "point_event", "two weeks ago", answer) == ATTACH_UNVERIFIED


@pytest.mark.parametrize(("kind", "relation"), [
    ("state", "occurrence"), ("point_event", "start"),
    ("state", "plan"), ("state", "none"), ("point_event", "plan"), ("point_event", "none"),
])
def test_decide_wrong_relation_is_rejected(kind, relation) -> None:
    answer = {"relation": relation, "expression": "two weeks ago"}
    assert decide(TURN_D, kind, "two weeks ago", answer) == ATTACH_REJECTED


def test_decide_right_relation_wrong_phrase_is_rejected() -> None:
    turn = "In May I bought a bike and in June I sold it."
    assert decide(turn, "point_event", "In May", {"relation": "occurrence", "expression": "in June"}) \
        == ATTACH_REJECTED


def test_decide_agreeing_answers() -> None:
    turn = "I have lived here for two years and I sold the bike two weeks ago."
    assert decide(turn, "state", "for two years", {"relation": "start", "expression": "for two years"}) \
        == ATTACH_AGREE
    assert decide(turn, "point_event", "two weeks ago",
                  {"relation": "occurrence", "expression": "two weeks ago"}) == ATTACH_AGREE


def test_decide_unknown_kind_is_rejected() -> None:
    assert decide(TURN_D, "plan", "two weeks ago", {"relation": "start", "expression": "two weeks ago"}) \
        == ATTACH_REJECTED


# ---------------------------------------------------------------- E. decide_all


def _cand(i, kind, expression):
    return SimpleNamespace(fact_index=i, kind=kind, expression=expression)


def test_decide_all_without_answers_marks_every_candidate_unverified() -> None:
    cands = [_cand(0, "point_event", "two weeks ago"), _cand(3, "state", "for two years")]
    assert decide_all(TURN_D, cands, None) == {0: ATTACH_UNVERIFIED, 3: ATTACH_UNVERIFIED}


def test_decide_all_keys_by_fact_index_and_uses_kind_and_expression() -> None:
    cands = [_cand(0, "point_event", "two weeks ago"), _cand(2, "state", "two weeks ago"),
             _cand(5, "point_event", "two weeks ago")]
    answers = {0: {"relation": "occurrence", "expression": "two weeks ago"},
               2: {"relation": "occurrence", "expression": "two weeks ago"}}  # wrong for a state
    assert decide_all(TURN_D, cands, answers) == {0: ATTACH_AGREE, 2: ATTACH_REJECTED, 5: ATTACH_UNVERIFIED}


def test_decide_all_accepts_plan_overlay_results() -> None:
    results = plan_overlay([_edge("e0")], {0: dict(LAST_MONTH)}, SPEECH_DATE)
    answers = {0: {"relation": "occurrence", "expression": "last month"}}
    assert decide_all("I moved last month.", results, answers) == {0: ATTACH_AGREE}


# ---------------------------------------------------------------- F. plan_overlay attach


def _edge(uuid="e0", valid_at=SPEECH, invalid_at=None):
    return EdgeTimeInput(uuid=uuid, fact="A fact.", valid_at=valid_at, invalid_at=invalid_at)


LAST_MONTH = {"expression": "last month", "basis": "speech_relative", "kind": "point_event",
              "calendar": {"which": "last", "unit": "month"}}
THIS_WEEK_STATE = {"expression": "this week", "basis": "speech_relative", "kind": "state",
                   "calendar": {"which": "this", "unit": "week"}}  # 02-12..02-18, past the speech date
PLAN = {**LAST_MONTH, "kind": "plan"}


def _all_cases():
    edges = [_edge("e0"), _edge("e1"), _edge("e2"), _edge("e3"),
             _edge("e4", valid_at=datetime(2024, 1, 5, tzinfo=timezone.utc)),
             _edge("e5", valid_at=datetime(2023, 12, 1, tzinfo=timezone.utc))]
    items = {0: dict(LAST_MONTH), 1: dict(THIS_WEEK_STATE), 2: {**LAST_MONTH, "guard": "state_start"},
             3: dict(PLAN), 4: dict(LAST_MONTH), 5: dict(LAST_MONTH)}
    return edges, items


def test_attach_none_is_identical_to_omitting_it() -> None:
    edges, items = _all_cases()
    base = plan_overlay(edges, items, SPEECH_DATE)
    assert plan_overlay(edges, items, SPEECH_DATE, None, None) == base
    assert plan_overlay(edges, items, SPEECH_DATE, attach=None) == base
    assert [r.reason for r in base] == ["written", "state_start", "state_start", "plan",
                                        "graphiti_inside_window", "graphiti_resolved"]


def test_attach_empty_makes_a_would_be_write_unverified() -> None:
    edges, items = _all_cases()
    base = plan_overlay(edges, items, SPEECH_DATE)[0]
    assert base.written
    got = plan_overlay(edges, items, SPEECH_DATE, attach={})[0]
    assert (got.reason, got.written, got.new_valid_at) == (ATTACH_UNVERIFIED, False, None)
    assert replace(got, reason=base.reason, written=base.written, new_valid_at=base.new_valid_at) == base


def test_attach_agree_writes_exactly_as_without() -> None:
    edges, items = _all_cases()
    base = plan_overlay(edges, items, SPEECH_DATE)
    assert plan_overlay(edges, items, SPEECH_DATE, attach={0: ATTACH_AGREE})[0] == base[0]


def test_attach_rejected_and_unknown_decision() -> None:
    edges, items = _all_cases()
    rejected = plan_overlay(edges, items, SPEECH_DATE, attach={0: ATTACH_REJECTED})[0]
    assert (rejected.reason, rejected.written, rejected.new_valid_at) == (ATTACH_REJECTED, False, None)
    odd = plan_overlay(edges, items, SPEECH_DATE, attach={0: "maybe"})[0]
    assert (odd.reason, odd.written) == (ATTACH_UNVERIFIED, False)


def test_attach_given_splits_state_future_from_the_guard() -> None:
    edges, items = _all_cases()
    assert plan_overlay(edges, items, SPEECH_DATE)[1].reason == "state_start"
    assert plan_overlay(edges, items, SPEECH_DATE)[2].reason == "state_start"
    with_attach = plan_overlay(edges, items, SPEECH_DATE, attach={})
    assert with_attach[1].reason == "state_future" and not with_attach[1].written
    assert with_attach[2].reason == "state_start" and not with_attach[2].written


def test_attach_does_not_touch_non_candidates() -> None:
    edges, items = _all_cases()
    base = plan_overlay(edges, items, SPEECH_DATE)
    got = plan_overlay(edges, items, SPEECH_DATE, attach={})
    for i in (3, 4, 5):
        assert got[i] == base[i]


# ---------------------------------------------------------------- G. AnchoredTimeAttacher


GOOD = _facts_json({"i": 0, "relation": "occurrence", "expression": "last week"})
ATTACH_TURN = "I sold the bike last week."
FACTS = ["The user sold a bike."]


class _Completions:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])


async def _sleep_forever():
    await asyncio.sleep(30)


def _attacher(*outcomes, timeout_s: float = 5.0):
    mod = _client_module()
    completions = _Completions(outcomes or [GOOD])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return mod.AnchoredTimeAttacher(client, base_url="https://api.openai.com/v1", model="gpt-6-luna",
                                    timeout_s=timeout_s), completions


@pytest.mark.asyncio
async def test_attacher_ok_answers_and_public_attributes() -> None:
    attacher, completions = _attacher()
    out = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    assert out.status == "ok" and out.cached is False
    assert out.answers == {0: {"relation": "occurrence", "expression": "last week"}}
    assert len(completions.calls) == 1
    assert attacher.model == "gpt-6-luna" and attacher.prompt_version == ATTACH_PROMPT_VERSION


@pytest.mark.asyncio
async def test_attacher_labels_the_call(monkeypatch) -> None:
    mod = _client_module()
    seen: list[str] = []
    real = mod.acreate_chat_completion

    def spy(*args, **kwargs):
        seen.append(kwargs.get("label"))
        return real(*args, **kwargs)

    monkeypatch.setattr(mod, "acreate_chat_completion", spy)
    attacher, _ = _attacher()
    assert (await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)).status == "ok"
    assert seen == ["anchored_time_attach"]


@pytest.mark.asyncio
async def test_attacher_cache_hit_skips_the_client_and_is_isolated() -> None:
    attacher, completions = _attacher()
    first = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    first.answers[0]["relation"] = "mutated"  # must not leak into the cache
    second = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    assert second.cached is True and len(completions.calls) == 1
    assert second.answers == {0: {"relation": "occurrence", "expression": "last week"}}
    second.answers[0]["relation"] = "mutated"
    third = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    assert third.answers[0]["relation"] == "occurrence"
    assert len(completions.calls) == 1


@pytest.mark.asyncio
async def test_attacher_cache_key_covers_facts_turn_and_date() -> None:
    attacher, completions = _attacher()
    await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    assert (await attacher.attach(ATTACH_TURN, SPEECH_DATE, ["The user sold a car."])).cached is False
    assert (await attacher.attach(ATTACH_TURN, date(2024, 2, 15), FACTS)).cached is False
    assert (await attacher.attach("I sold the bike last month.", SPEECH_DATE, FACTS)).cached is False
    assert len(completions.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status", "error_class"),
    [
        ("", "empty", ""),
        ("   ", "empty", ""),
        ("{not json", "parse_error", ""),
        ("[1, 2, 3]", "parse_error", ""),
        (RuntimeError("connection reset"), "error", "RuntimeError"),
        (None, "budget", "LlmUsageControlSignal"),  # replaced by the signal inside the test
        (_sleep_forever, "timeout", ""),
    ],
)
async def test_attacher_failures_have_no_answers_and_are_not_cached(outcome, status, error_class) -> None:
    if status == "budget":
        from menhir.infrastructure.observability import LlmUsageControlSignal
        outcome = LlmUsageControlSignal("budget refused")
    attacher, completions = _attacher(outcome, GOOD, timeout_s=0.05)
    out = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)
    assert (out.status, out.answers, out.cached, out.error_class) == (status, None, False, error_class)
    assert len(completions.calls) == 1
    retry = await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)  # the failure was not cached
    assert len(completions.calls) == 2
    assert retry.status == "ok" and retry.cached is False


@pytest.mark.asyncio
async def test_attacher_cancellation_propagates() -> None:
    attacher, _ = _attacher(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await attacher.attach(ATTACH_TURN, SPEECH_DATE, FACTS)


# ---------------------------------------------------------------- H. prompt version


def test_prompt_version_is_the_hash_of_the_prompt_text() -> None:
    assert re.fullmatch(r"[0-9a-f]{8}", ATTACH_PROMPT_VERSION)
    expected = hashlib.sha256(f"{attach_mod.SYSTEM}\n{attach_mod.INSTRUCTIONS}".encode("utf-8")).hexdigest()[:8]
    assert ATTACH_PROMPT_VERSION == expected
