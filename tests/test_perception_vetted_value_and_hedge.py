"""Counter perception stores the value its gate vetted, and abstains on hedged numbers.

Found by E2E-16 (#170 item 5): "I have 15 mugs" then "I bought a new mug" was gated as the Law-3
value 15 + 1 = 16, but the sink re-reduced [anchor, acquire] under count and stored 2. And "I own
about 5 or 6 bikes", read unanimously as 5, committed an exact counter of 5.
"""

from __future__ import annotations

import json

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.event_fold import fold_events_to_counter
from menhir.services.perception import VETO_HEDGED_VALUE, Episode, perceive_and_fold


class FakeLLM:
    def __init__(self, events: list[dict]):
        self._reply = json.dumps(events)

    def __call__(self, system: str, user: str) -> str:
        return self._reply


class FakeAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    def record_counter(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "u", "view_key": "k", "created": True}


def _episodes(*texts: str) -> list[Episode]:
    return [Episode(uuid=f"e{i}", content=text) for i, text in enumerate(texts)]


def _event(ep: int, measure: str, kind: str, when: str, **extra) -> dict:
    return {"episode": ep, "subject": "user", "measure": measure, "kind": kind, "when": when,
            "what": "quote", **extra}


def _fold(episodes: list[Episode], events: list[dict]):
    adapter = FakeAdapter()
    result = perceive_and_fold(episodes=episodes, llm_complete=FakeLLM(events), graph_adapter=adapter,
                               k=3, enable_stated_span_guard=True)
    return result, {c["counter"]: c["value"] for c in adapter.calls}


@pytest.mark.unit
def test_stated_count_plus_later_acquisition_stores_anchor_plus_delta():
    episodes = _episodes("[2026-01-01] I have 15 mugs in the kitchen.",
                         "[2026-02-01] I bought a new mug at the market.")
    _result, stored = _fold(episodes, [
        _event(0, "mugs", "assertion", "2026-01-01", value=15),
        _event(1, "mugs", "acquire", "2026-02-01", identity="blue mug", category="kitchen"),
    ])
    assert stored == {"mugs": 16.0}


@pytest.mark.unit
def test_stated_count_plus_later_possessions_stores_anchor_plus_distinct_items():
    episodes = _episodes("[2026-01-01] I have 3 bikes in the garage.",
                         "[2026-02-01] I now also own a gravel bike.",
                         "[2026-02-03] And a tandem bike too.")
    _result, stored = _fold(episodes, [
        _event(0, "bikes", "assertion", "2026-01-01", value=3),
        _event(1, "bikes", "item", "2026-02-01", identity="gravel bike"),
        _event(2, "bikes", "item", "2026-02-03", identity="tandem bike"),
    ])
    assert stored == {"bikes": 5.0}


@pytest.mark.unit
def test_stated_spend_plus_later_purchase_is_unchanged():
    episodes = _episodes("[2026-01-01] I have spent $60 on groceries.",
                         "[2026-02-01] I spent $15 at the farmers market.")
    _result, stored = _fold(episodes, [
        _event(0, "grocery_spend", "assertion", "2026-01-01", value=60),
        _event(1, "grocery_spend", "purchase", "2026-02-01", value=15),
    ])
    assert stored == {"grocery_spend": 75.0}


@pytest.mark.unit
def test_sink_stores_a_given_value_and_reduces_without_one():
    events = [Event(when="2026-01-01", kind="assertion", value=15, episode_uuid="e0"),
              Event(when="2026-02-01", kind="acquire", identity="blue mug", episode_uuid="e1")]
    given, reduced = FakeAdapter(), FakeAdapter()
    fold_events_to_counter(graph_adapter=given, subject="user", measure="mugs", events=events,
                           reducer="count", value=16.0)
    fold_events_to_counter(graph_adapter=reduced, subject="user", measure="mugs", events=events,
                           reducer="count")
    assert given.calls[0]["value"] == 16.0 and given.calls[0]["valid_at"] == "2026-02-01"
    assert given.calls[0]["episode_uuids"] == ["e0", "e1"]
    assert reduced.calls[0]["value"] == 2.0


@pytest.mark.unit
@pytest.mark.parametrize("text", [
    "I own about 5 or 6 bikes.",
    "I own maybe 5 bikes.",
    "I own between 5 and 6 bikes.",
    "I own 5-ish bikes.",
])
def test_hedged_stated_count_abstains(text):
    result, stored = _fold(_episodes(f"[2026-01-01] {text}"),
                           [_event(0, "bikes", "assertion", "2026-01-01", value=5)])
    assert stored == {}
    assert [d.veto for d in result.abstained] == [VETO_HEDGED_VALUE]


@pytest.mark.unit
def test_hedged_anchor_abstains_the_law3_total():
    episodes = _episodes("[2026-01-01] I have around 15 mugs.",
                         "[2026-02-01] I bought a new mug at the market.")
    result, stored = _fold(episodes, [
        _event(0, "mugs", "assertion", "2026-01-01", value=15),
        _event(1, "mugs", "acquire", "2026-02-01", identity="blue mug"),
    ])
    assert stored == {} and [d.veto for d in result.abstained] == [VETO_HEDGED_VALUE]


@pytest.mark.unit
def test_hedged_purchase_amount_abstains_the_sum():
    episodes = _episodes("[2026-01-01] I spent about $20 on seeds.",
                         "[2026-01-05] I spent $15 on soil.")
    result, stored = _fold(episodes, [
        _event(0, "garden_spend", "purchase", "2026-01-01", value=20),
        _event(1, "garden_spend", "purchase", "2026-01-05", value=15),
    ])
    assert stored == {} and [d.veto for d in result.abstained] == [VETO_HEDGED_VALUE]


@pytest.mark.unit
def test_a_hedge_in_another_clause_does_not_fire():
    episodes = _episodes("[2026-01-01] I have 15 mugs, and maybe 2 or 3 cups somewhere.")
    _result, stored = _fold(episodes, [_event(0, "mugs", "assertion", "2026-01-01", value=15)])
    assert stored == {"mugs": 15.0}
