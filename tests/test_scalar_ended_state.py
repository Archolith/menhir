"""Ended-state quotes never yield a CURRENT scalar value (#168 step-5 offline check, false-current).

The extraction contract represents an ended value as operation=expire (or boolean false). Before this
fix, "I quit smoking" with a model `smokes=true` and "I no longer have 3 cars" with a model `cars=3`
(absolute) were admitted as current values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.services.typed_scalar_rules import parse_scalar_row

pytestmark = [pytest.mark.unit]

REF = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _parse(sentence: str, span: str, attribute: str, kind: str, value, *, operation="absolute", unit=""):
    ep = SimpleNamespace(uuid="ep-1", content=sentence, reference_time=REF)
    row = {"subject": "I", "attribute": attribute, "operation": operation, "stated_span": span,
           "value_kind": kind, "value": value, "unit": unit, "scope": "", "display": "", "episode": 0}
    drops: list[str] = []
    return parse_scalar_row(row, [ep], drops.append), drops


@pytest.mark.parametrize("span", [
    "I quit smoking",
    "I stopped smoking",
    "I gave up smoking",
    "I no longer smoke",
    "I don't smoke anymore",
    "I used to smoke",
])
def test_ended_boolean_is_false_never_true(span: str) -> None:
    proposal, drops = _parse(span + ".", span, "smokes", "boolean", True)
    assert proposal is None and drops == ["boolean_source_mismatch"]
    proposal, _drops = _parse(span + ".", span, "smokes", "boolean", False)
    assert proposal is not None and proposal.value is False


def test_restored_state_is_not_forced_false() -> None:
    span = "I quit smoking but started again"
    proposal, _drops = _parse(span + ".", span, "smokes", "boolean", True)
    assert proposal is not None and proposal.value is True


def test_habit_idiom_used_to_is_a_current_state() -> None:
    span = "I'm used to waking up at 6:00"
    proposal, drops = _parse(span + ".", span, "wake_time", "clock_time", "06:00")
    assert proposal is not None, drops
    assert proposal.value == "06:00"


@pytest.mark.parametrize("sentence,span,attribute,kind,value", [
    ("I no longer have 3 cars.", "I no longer have 3 cars", "cars", "count", 3),
    ("I used to weigh 90 kg.", "I used to weigh 90 kg", "body_weight", "measurement", 90),
    ("I don't pay $1,200 in rent anymore.", "I don't pay $1,200 in rent anymore", "rent", "money", 1200),
])
def test_ended_absolute_value_is_dropped(sentence, span, attribute, kind, value) -> None:
    proposal, drops = _parse(sentence, span, attribute, kind, value)
    assert proposal is None and drops == ["ended_state"]


def test_ended_state_with_current_cue_keeps_the_current_value() -> None:
    span = "I used to weigh 90 kg but now I weigh 82 kg"
    proposal, drops = _parse(span + ".", span, "body_weight", "measurement", 82)
    assert proposal is not None, drops
    assert proposal.value == 82 and proposal.unit == "kg"


def test_expire_for_an_ended_state_is_still_admitted() -> None:
    proposal, drops = _parse("I no longer have 3 cars.", "I no longer have 3 cars", "cars", "count", 3,
                             operation="expire")
    assert proposal is not None, drops
    assert proposal.operation == "expire"


def test_pounds_resolve_to_lb() -> None:
    proposal, drops = _parse("I weigh 180 pounds.", "I weigh 180 pounds", "body_weight", "measurement", 180)
    assert proposal is not None, drops
    assert proposal.unit == "lb" and proposal.value == 180
