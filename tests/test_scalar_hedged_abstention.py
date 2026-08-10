"""Hedged-value abstention (perceiver admission validation) -- offline.

The unanimous k-sample gate is necessary but not sufficient: the 3 samples can agree on the SAME
over-confident exact value for an underdetermined statement ("about 30 or 40 coins"). `is_ambiguous_exact`
is a deterministic, span-local check that drops an exact value-bearing proposal whose grounded span states
an underdetermined value, so the claim fails closed to abstention. A genuine [lo, hi] range is exempt.
"""

from __future__ import annotations

import pytest

from menhir.services.typed_scalar_perception import (
    extract_typed_scalars_once,
    is_ambiguous_exact,
)


# --------------------------------------------------------------------- validator: contrast pairs

@pytest.mark.unit
@pytest.mark.parametrize("span,value,expect_ambiguous", [
    # DISCRETE alternatives -> abstain (even if a range value were offered)
    ("I own 30 or 40 coins.", 30, True),
    ("My rent is 1500 or maybe 1600 dollars.", 1500, True),
    ("between 30 and 40 or so", [30, 40], True),        # 'or' governs -> still discrete/ambiguous
    # APPROXIMATION -> abstain for an exact value
    ("I pay about $1,500 in rent.", 1500, True),
    ("I wake up around 7:30.", "07:30", True),
    ("The delivery took around forty minutes.", 40, True),
    ("The maintenance window starts around noon.", "12:00", True),
    ("The replacement should take around a year.", 1, True),
    ("The package weighs around a kilogram.", 1, True),
    ("roughly 45 minutes", 45, True),
    ("a few times a week", 3, True),
    ("several coins", 5, True),
    ("usually 7, sometimes 7:30", "07:00", True),
    # EXPLICIT CLOSED INTERVAL with a RANGE value -> admit (range captures the uncertainty)
    ("I own between 30 and 40 coins.", [30, 40], False),
    ("from 30 to 40 minutes", [30, 40], False),
    # DEFINITE exact values -> admit
    ("My rent is exactly $1,500.", 1500, False),
    ("I have 37 coins.", 37, False),
    ("I wake up at 7:30 every morning.", "07:30", False),
    ("The tank holds exactly 37 liters this time around.", 37, False),
    ("This time around, my balance is exactly $1,500.", 1500, False),
    ("My fastest lap is 1:42 on this course this time around.", 102, False),
    ("The next time around, the tank will still hold exactly 37 liters.", 37, False),
    ("My car is red.", "red", False),
    ("My day off is Wednesday.", "Wednesday", False),
])
def test_is_ambiguous_exact(span, value, expect_ambiguous):
    assert is_ambiguous_exact(span, value, "absolute") is expect_ambiguous


@pytest.mark.unit
def test_interval_with_exact_value_is_ambiguous_range_value_is_not():
    # "between 30 and 40" with a mistakenly-EXACT value must abstain (an exact 30 is over-confident);
    # the SAME span with a proper [30,40] range is admitted.
    assert is_ambiguous_exact("I own between 30 and 40 coins.", 30, "absolute") is True
    assert is_ambiguous_exact("I own between 30 and 40 coins.", [30, 40], "absolute") is False


@pytest.mark.unit
def test_span_local_or_elsewhere_does_not_govern():
    # a definite value whose span is just the value clause is admitted; the ambiguity must be IN the span.
    assert is_ambiguous_exact("I have 37 coins", 37, "absolute") is False
    # but if the extracted span itself carries the 'or', it governs
    assert is_ambiguous_exact("37 or 38 coins", 37, "absolute") is True


@pytest.mark.unit
def test_expire_is_never_hedge_checked():
    # an expire carries the OLD value as provenance only -> not a current value, never dropped as hedged.
    assert is_ambiguous_exact("I used to own about 30 coins", 30, "expire") is False


# --------------------------------------------------------------------- end-to-end extraction drop

class _Ep:
    def __init__(self, uuid, content):
        self.uuid = uuid
        self.content = content


def _llm(rows):
    import json

    def complete(system, user):
        return json.dumps(rows)
    return complete


def _row(**over):
    base = dict(episode=0, subject="user", attribute="owned", scope="", value_kind="count",
                unit="", operation="absolute", value=37, when="", stated_span="I own 37 coins")
    base.update(over)
    return base


@pytest.mark.unit
def test_extraction_drops_a_hedged_exact_proposal():
    ep = _Ep("ep-1", "user: I own 30 or 40 coins.")
    rows = [_row(value=30, stated_span="I own 30 or 40 coins")]
    out = extract_typed_scalars_once([ep], _llm(rows))
    assert out == []   # dropped -> the gate sees no vote -> the claim abstains


@pytest.mark.unit
def test_extraction_keeps_a_definite_exact_proposal():
    ep = _Ep("ep-1", "user: I have 37 coins.")
    rows = [_row(value=37, stated_span="I have 37 coins")]
    out = extract_typed_scalars_once([ep], _llm(rows))
    assert len(out) == 1 and out[0].value == 37


@pytest.mark.unit
def test_extraction_keeps_definite_value_when_around_is_an_unrelated_idiom():
    ep = _Ep("ep-1", "user: The tank holds 37 liters this time around.")
    rows = [_row(
        attribute="tank_capacity",
        value=37,
        unit="liters",
        stated_span="The tank holds 37 liters this time around",
    )]
    out = extract_typed_scalars_once([ep], _llm(rows))
    assert len(out) == 1 and out[0].value == 37


@pytest.mark.unit
def test_extraction_keeps_existing_record_inside_future_goal_with_around_idiom():
    source = "I'm hoping to beat my fastest cycling time of 41:08 this time around."
    ep = _Ep("ep-1", f"user: {source}")
    rows = [_row(
        attribute="fastest_cycling_time",
        value_kind="duration",
        value="41:08",
        unit="seconds",
        stated_span=source.rstrip("."),
    )]
    out = extract_typed_scalars_once([ep], _llm(rows))
    assert len(out) == 1 and out[0].value == 2468


@pytest.mark.unit
def test_extraction_keeps_a_valid_range_proposal():
    ep = _Ep("ep-1", "user: I own between 30 and 40 coins.")
    rows = [_row(value=[30, 40], stated_span="between 30 and 40 coins")]
    out = extract_typed_scalars_once([ep], _llm(rows))
    assert len(out) == 1 and out[0].value == [30, 40]
