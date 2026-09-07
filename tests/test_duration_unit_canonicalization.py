"""Regression coverage for canonical duration identity.

Duration unit is part of the durable scalar slot, so equivalent elapsed amounts must be
canonicalized before voting and persistence. Otherwise 60 minutes, 1 hour, and 1:00:00 can occupy
different current ScalarStateView slots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import extract_typed_scalars_once, gate_typed_scalars


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, value, unit, stated_span, operation="absolute"):
    return {
        "episode": 0,
        "subject": "user",
        "attribute": "workout_duration",
        "scope": "",
        "value_kind": "duration",
        "unit": unit,
        "operation": operation,
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "content,value,unit,span,expected",
    [
        ("My workout lasts 60 seconds.", 60, "seconds", "workout lasts 60 seconds", 60),
        ("My workout lasts 1 minute.", 1, "minute", "workout lasts 1 minute", 60),
        ("My workout lasts 1.5 minutes.", 1.5, "minutes", "workout lasts 1.5 minutes", 90),
        ("My workout lasts 1 hour.", 1, "hour", "workout lasts 1 hour", 3600),
        ("My workout lasts 0.5 hours.", 0.5, "hours", "workout lasts 0.5 hours", 1800),
        ("My workout time is 1:30.", "1:30", "", "workout time is 1:30", 90),
        ("My workout time is 1:00:00.", "1:00:00", "", "workout time is 1:00:00", 3600),
    ],
)
def test_duration_forms_canonicalize_to_seconds(content, value, unit, span, expected):
    out = extract_typed_scalars_once(
        [_Ep(uuid="ep", content=content)],
        _llm([_row(value=value, unit=unit, stated_span=span)]),
    )
    assert len(out) == 1
    assert out[0].value == expected
    assert out[0].unit == "seconds"
    assert out[0].slot_key == ("workout_duration", "", "duration", "seconds")


@pytest.mark.unit
def test_equivalent_numeric_duration_units_vote_as_one_interpretation():
    ep = [_Ep(uuid="ep", content="My workout lasts 1 hour.")]
    samples = []
    for value, unit in [(1, "hour"), (60, "minutes"), (3600, "seconds")]:
        samples.append(
            extract_typed_scalars_once(
                ep,
                _llm([_row(value=value, unit=unit, stated_span="workout lasts 1 hour")]),
            )
        )

    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.committed is True
    assert decision.agreement == 1.0
    assert decision.proposal is not None
    assert decision.proposal.value == 3600
    assert decision.proposal.unit == "seconds"


@pytest.mark.unit
def test_duration_delta_canonicalizes_to_seconds():
    ep = [_Ep(uuid="ep", content="My workout increased by 1.5 minutes.")]
    out = extract_typed_scalars_once(
        ep,
        _llm([
            _row(
                value=1.5,
                unit="minutes",
                operation="delta",
                stated_span="workout increased by 1.5 minutes",
            )
        ]),
    )
    assert len(out) == 1
    assert out[0].value == 90
    assert out[0].unit == "seconds"
    assert out[0].operation == "delta"


@pytest.mark.unit
def test_duration_range_canonicalizes_both_endpoints():
    ep = [_Ep(uuid="ep", content="My workout lasts between 1 and 1.5 hours.")]
    out = extract_typed_scalars_once(
        ep,
        _llm([
            _row(
                value=[1, 1.5],
                unit="hours",
                stated_span="workout lasts between 1 and 1.5 hours",
            )
        ]),
    )
    assert len(out) == 1
    assert out[0].value == [3600, 5400]
    assert out[0].unit == "seconds"


@pytest.mark.unit
def test_unknown_duration_unit_fails_closed():
    ep = [_Ep(uuid="ep", content="My workout lasts 3 fortnights.")]
    out = extract_typed_scalars_once(
        ep,
        _llm([_row(value=3, unit="fortnights", stated_span="workout lasts 3 fortnights")]),
    )
    assert out == []
