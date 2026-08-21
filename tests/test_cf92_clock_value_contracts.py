"""CF-92: `_clock_value` existed twice with different failure contracts. They are now visibly
distinct (`_clock_value_or_raise` in the extractor vs `_clock_value_or_none` in the composer),
with no behavior change: the extractor still raises ValueError and the composer still returns None
on a mixed 24h+meridiem form, and both still produce identical zero-padded output on valid input.
"""

from __future__ import annotations

import re

import pytest

from menhir.services.deterministic_scalar_extractor import _clock_value_or_raise
from menhir.services.structural_scalar_composer import _clock_value_or_none

_CLOCK_RE = re.compile(
    r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)?", re.IGNORECASE)

_MIXED_FORMS = ["22:15 PM", "00:15 am"]
_VALID_FORMS = ["9:05 am", "9:05 pm", "12:00 am", "12:00 pm", "22:15"]


def _extract_match(text: str) -> re.Match[str]:
    match = _CLOCK_RE.fullmatch(text)
    assert match is not None
    return match


@pytest.mark.parametrize("text", _MIXED_FORMS)
def test_extractor_raises_on_mixed_24h_plus_meridiem(text: str) -> None:
    with pytest.raises(ValueError):
        _clock_value_or_raise(_extract_match(text))


@pytest.mark.parametrize("text", _MIXED_FORMS)
def test_composer_returns_none_on_mixed_24h_plus_meridiem(text: str) -> None:
    assert _clock_value_or_none(text) is None


@pytest.mark.parametrize("text", _VALID_FORMS)
def test_extractor_and_composer_agree_on_valid_input(text: str) -> None:
    expected = _clock_value_or_none(text)
    assert expected is not None
    assert _clock_value_or_raise(_extract_match(text)) == expected


def test_old_clock_value_name_is_absent_from_both_modules() -> None:
    import menhir.services.deterministic_scalar_extractor as extractor
    import menhir.services.structural_scalar_composer as composer
    assert not hasattr(extractor, "_clock_value")
    assert not hasattr(composer, "_clock_value")
    assert hasattr(extractor, "_clock_value_or_raise")
    assert hasattr(composer, "_clock_value_or_none")


@pytest.mark.unit
@pytest.mark.parametrize("text", ["not a time", "", "25:00", "9:60 am", "9:05 xm"])
def test_composer_returns_none_on_unparseable_text_rather_than_raising(text: str) -> None:
    """The other None-returning branch. `_clock_validator` compares the result against
    `proposal.normalized_value` and reports a reason code; a raise here would escape as an
    exception instead of a validation reason, which is the failure-mode confusion CF-92 is about.
    """
    assert _clock_value_or_none(text) is None
