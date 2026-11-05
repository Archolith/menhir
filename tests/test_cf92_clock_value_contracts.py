"""CF-92: `_clock_value` existed twice with different failure contracts. The composer's is now
named `_clock_value_or_none`: it returns None on a mixed 24h+meridiem form, with no behavior change.
"""

from __future__ import annotations

import pytest

from menhir.services.structural_scalar_composer import _clock_value_or_none

_MIXED_FORMS = ["22:15 PM", "00:15 am"]


@pytest.mark.parametrize("text", _MIXED_FORMS)
def test_composer_returns_none_on_mixed_24h_plus_meridiem(text: str) -> None:
    assert _clock_value_or_none(text) is None


def test_old_clock_value_name_is_absent_from_composer() -> None:
    import menhir.services.structural_scalar_composer as composer
    assert not hasattr(composer, "_clock_value")
    assert hasattr(composer, "_clock_value_or_none")


@pytest.mark.unit
@pytest.mark.parametrize("text", ["not a time", "", "25:00", "9:60 am", "9:05 xm"])
def test_composer_returns_none_on_unparseable_text_rather_than_raising(text: str) -> None:
    """The other None-returning branch. `_clock_validator` compares the result against
    `proposal.normalized_value` and reports a reason code; a raise here would escape as an
    exception instead of a validation reason, which is the failure-mode confusion CF-92 is about.
    """
    assert _clock_value_or_none(text) is None
