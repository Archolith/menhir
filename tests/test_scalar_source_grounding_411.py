"""#411: a model value in the five model-read kinds must be supported by its source.

Each row is scripted, so these pin the check itself rather than any model's behaviour.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.services.typed_scalar_rules import parse_scalar_row

UNITS = {"duration": "minutes", "frequency": "per_week"}


def _parse(kind, source, value, *, attribute="probe", span=None, unit=None, operation="absolute",
           subject="user"):
    episode = SimpleNamespace(uuid="ep", content=source, reference_time="2026-07-01T10:00:00+00:00")
    row = {
        "subject": subject, "attribute": attribute, "scope": "", "value_kind": kind,
        "unit": UNITS.get(kind, "") if unit is None else unit, "display": "", "when": "",
        "operation": operation, "value": value, "stated_span": span or source, "episode": 0,
    }
    drops: list[str] = []
    return parse_scalar_row(row, [episode], drops.append), drops


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "source", "value", "attribute", "unit"),
    [
        # The 13 unsafe admissions the #169 matrix recorded at 44933985.
        ("duration", "My commute takes 45 minutes.", 30, "commute_duration", None),
        ("duration", "My commute is pretty long.", 60, "commute_duration", None),
        ("duration", "I sleep 7 hours a night.", 7, "sleep_duration", "minutes"),
        ("frequency", "I go to the gym 3 times a week.", 5, "gym_frequency", None),
        ("frequency", "I go to the gym when I can.", 3, "gym_frequency", None),
        ("boolean", "I'm not a vegetarian.", True, "is_vegetarian", None),
        ("boolean", "I might be allergic to cats.", True, "cat_allergy", None),
        ("boolean", "I visited my aunt who has cats.", True, "cat_allergy", None),
        ("status", "I'm divorced.", "married", "marital_status", None),
        ("status", "My partner and I went to Rome.", "married", "marital_status", None),
        ("status", "I'm not employed right now.", "employed", "employment_status", None),
        ("weekday", "I do laundry on Sundays.", "saturday", "laundry_day", None),
        ("weekday", "I do laundry every week.", "sunday", "laundry_day", None),
        # Counterexamples beyond the matrix.
        ("boolean", "I visited my aunt who has cats.", True, "owns_cats", None),
        ("boolean", "I'm a vegetarian.", False, "is_vegetarian", None),
        ("boolean", "Alice is a vegetarian.", True, "is_vegetarian", None),
        ("status", "I'm unemployed.", "employed", "employment_status", None),
        ("status", "I visited my aunt who is married.", "married", "marital_status", None),
        ("status", "Am I married?", "married", "marital_status", None),
        ("status", "If I were married I'd move.", "married", "marital_status", None),
        ("weekday", "I work every day except Sundays.", "sunday", "work_day", None),
        ("weekday", "I'm not free on Sundays.", "sunday", "free_day", None),
        ("duration", "Is 7 hours enough sleep?", 7, "sleep_duration", "hours"),
        ("duration", "I'm hoping to sleep 7 hours a night.", 7, "sleep_duration", "hours"),
        ("duration", "My commute doesn't take 45 minutes.", 45, "commute_duration", None),
        ("duration", "I sleep 7 hours, not 8 hours.", 8, "sleep_duration", "hours"),
        ("frequency", "I go 3 times a week for 5 minutes.", 5, "gym_frequency", None),
        ("frequency", "I go to the gym 3 times a month.", 3, "gym_frequency", None),
        ("frequency", "I don't go to the gym every week.", 1, "gym_frequency", "week"),
    ],
)
def test_unsupported_values_are_dropped(kind, source, value, attribute, unit):
    proposal, drops = _parse(kind, source, value, attribute=attribute, unit=unit)
    assert proposal is None
    assert drops


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "source", "span", "value", "attribute"),
    [
        ("boolean", "I'm not a vegetarian.", "a vegetarian", True, "is_vegetarian"),
        ("status", "I'm not employed right now.", "employed", "employed", "employment_status"),
        ("weekday", "I'm not free on Sundays.", "free on Sundays", "sunday", "free_day"),
        ("duration", "My commute doesn't take 45 minutes.", "45 minutes", 45, "commute_duration"),
    ],
)
def test_a_short_quote_does_not_hide_the_negation_before_it(kind, source, span, value, attribute):
    proposal, drops = _parse(kind, source, value, attribute=attribute, span=span)
    assert proposal is None
    assert drops == ["negated_value"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "source", "value", "attribute", "unit", "expected"),
    [
        ("duration", "My commute takes 45 minutes.", 45, "commute_duration", None, 2700),
        ("duration", "My commute takes forty-five minutes.", 45, "commute_duration", None, 2700),
        ("duration", "My commute takes half an hour.", 30, "commute_duration", None, 1800),
        ("duration", "My commute takes an hour.", 1, "commute_duration", "hours", 3600),
        ("duration", "My commute takes an hour and a half.", 90, "commute_duration", None, 5400),
        ("duration", "It takes 1 hour and 30 minutes.", 90, "commute_duration", None, 5400),
        ("duration", "It's a 45-minute commute.", 45, "commute_duration", None, 2700),
        ("duration", "I sleep 7 hours, not 8 hours.", 7, "sleep_duration", "hours", 25200),
        ("duration", "My best time of 25:50 still stands.", "25:50", "best_time", None, 1550),
        ("frequency", "I go to the gym 3 times a week.", 3, "gym_frequency", None, 3),
        ("frequency", "I call my mom twice a week.", 2, "mom_call_frequency", None, 2),
        ("frequency", "I go to the gym 3 days per week.", 3, "gym_frequency", None, 3),
        ("frequency", "I call home weekly.", 1, "call_frequency", "per_week", 1),
        ("frequency", "I go every other week.", 1, "gym_frequency", "week", 0.5),
        ("weekday", "I do laundry on Sundays.", "sunday", "laundry_day", None, "sunday"),
        ("status", "I'm married.", "married", "marital_status", None, "married"),
        ("status", "I'm single, not married.", "single", "marital_status", None, "single"),
        ("status", "I work full-time.", "full_time", "work_status", None, "full_time"),
        ("boolean", "I'm a vegetarian.", True, "is_vegetarian", None, True),
        ("boolean", "I'm not a vegetarian.", False, "is_vegetarian", None, False),
        ("boolean", "I don't smoke.", False, "smokes", None, False),
        ("boolean", "I have cats.", True, "has_cats", None, True),
        ("boolean", "I quit smoking but started again in May.", True, "smokes", None, True),
    ],
)
def test_stated_values_are_admitted(kind, source, value, attribute, unit, expected):
    proposal, drops = _parse(kind, source, value, attribute=attribute, unit=unit)
    assert drops == []
    assert proposal is not None
    assert proposal.value == expected


@pytest.mark.unit
def test_an_expire_row_need_not_restate_the_value_it_ends():
    # Dropping this would leave `employed` current, the same wrong answer from the other side.
    proposal, drops = _parse(
        "status", "I quit my job.", "employed", attribute="employment_status", operation="expire")
    assert drops == []
    assert proposal is not None and proposal.operation == "expire"


@pytest.mark.unit
def test_a_third_party_subject_keeps_its_own_fact():
    proposal, drops = _parse("boolean", "Alice is a vegetarian.", True, attribute="is_vegetarian", subject="Alice")
    assert drops == []
    assert proposal is not None and proposal.value is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "source", "value", "attribute"),
    [
        ("status", "I was married for ten years.", "married", "marital_status"),
        ("status", "I was a teacher.", "teacher", "occupation_status"),
        ("boolean", "I was a vegetarian.", True, "is_vegetarian"),
    ],
)
def test_a_past_state_is_not_a_current_value(kind, source, value, attribute):
    proposal, drops = _parse(kind, source, value, attribute=attribute)
    assert proposal is None
    assert drops


@pytest.mark.unit
def test_a_stated_record_is_kept_even_in_the_past_tense():
    proposal, drops = _parse("duration", "My best time was 25:50.", "25:50", attribute="best_time")
    assert drops == []
    assert proposal is not None and proposal.value == 1550
