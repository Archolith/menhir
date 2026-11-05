"""#155 grounded event dates — offline unit tests, no live model/network/db.

Event valid time must be grounded in SOURCE evidence inside the sentence containing the claim span,
never in an ungrounded model `when`. Proves: the repro (model 2099 date ignored -> episode reference),
explicit full source dates (ISO and 'Month D, YYYY'), sentence-locality, future source dates ignored,
month+day-without-year resolution to the most recent year <= the reference, supported relative phrases,
malformed `when` falling back to the reference, and unchanged abstention without a usable reference.
These tests exercise the resolver in ``services.grounded_event_time`` (shared with read-time
occurrence dating).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from menhir.services.grounded_event_time import (
    resolve_event_valid_time,
    sentence_context,
)

REF = "2026-09-23T12:00:00+00:00"

_SPAN = "bought a notebook"


@dataclass(frozen=True)
class _Res:
    valid_at: str | None
    time_basis: str | None
    note: str | None

    @property
    def built(self) -> bool:
        return self.valid_at is not None

    @property
    def assertion(self) -> "_Res":
        return self

    @property
    def reason(self) -> str | None:
        return None if self.built else "no_valid_time"


def _build(content: str, *, when: str = "", ref: str | None = REF) -> _Res:
    start = content.index(_SPAN)
    context = sentence_context(content, start, start + len(_SPAN))
    valid_at, time_basis, note = resolve_event_valid_time(when, context, ref)
    return _Res(valid_at, time_basis, note)


# --------------------------------------------------------------------------- issue repro


@pytest.mark.unit
def test_ungrounded_model_when_is_ignored_falls_to_episode_reference():
    res = _build("I bought a notebook.", when="2099-01-01")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note == "ungrounded_when_ignored"


@pytest.mark.unit
def test_blank_when_falls_to_episode_reference_with_no_note():
    res = _build("I bought a notebook.")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note is None


# --------------------------------------------------------------------------- explicit source dates


@pytest.mark.unit
def test_iso_source_date_wins_over_matching_model_when():
    res = _build("On 2026-07-18 I bought a notebook.", when="2026-07-18")
    assert res.built
    assert res.assertion.valid_at == "2026-07-18T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note is None


@pytest.mark.unit
def test_month_day_year_source_date_wins():
    res = _build("I bought a notebook on July 18, 2026.")
    assert res.built
    assert res.assertion.valid_at == "2026-07-18T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note is None


@pytest.mark.unit
def test_conflicting_model_when_yields_source_date_with_conflict_note():
    res = _build("On 2026-07-18 I bought a notebook.", when="2026-08-01")
    assert res.built
    assert res.assertion.valid_at == "2026-07-18T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note == "when_conflict_used_source"


# --------------------------------------------------------------------------- sentence locality


@pytest.mark.unit
def test_date_in_a_different_sentence_is_not_used():
    res = _build("On 2026-07-18 I walked far. I bought a notebook.", when="2026-07-18")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note == "ungrounded_when_ignored"


# --------------------------------------------------------------------------- future source dates


@pytest.mark.unit
def test_future_source_date_is_ignored():
    res = _build("On 2099-01-01 I bought a notebook.")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note == "future_source_date_ignored"


# --------------------------------------------------------------------------- month+day without year


@pytest.mark.unit
def test_month_day_no_year_matching_model_when_resolves_most_recent_year():
    res = _build("I bought a notebook on July 18.", when="2020-07-18")
    assert res.built
    assert res.assertion.valid_at == "2026-07-18T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"


@pytest.mark.unit
def test_month_day_no_year_rolls_back_a_year_when_needed():
    res = _build("I bought a notebook on December 2.", when="2021-12-02")
    assert res.built
    assert res.assertion.valid_at == "2025-12-02T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"


@pytest.mark.unit
def test_month_day_no_year_mismatched_model_when_falls_back():
    res = _build("I bought a notebook on July 18.", when="2026-08-05")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note == "undated_month_day_unmatched"


@pytest.mark.unit
def test_month_day_no_year_without_model_when_resolves_stated_date():
    # #155 K1: the stated month/day grounds the date even when the model gave no when
    res = _build("I bought a notebook on July 18.", when="")
    assert res.built
    assert res.assertion.valid_at == "2026-07-18T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"


# --------------------------------------------------------------------------- future tolerance


@pytest.mark.unit
def test_source_date_one_day_after_reference_is_accepted():
    # #155 K2: exactly 1 day after the reference is timezone tolerance, not future
    res = _build("On 2026-09-24 I bought a notebook.")
    assert res.built
    assert res.assertion.valid_at == "2026-09-24T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note is None


@pytest.mark.unit
def test_source_date_two_days_after_reference_is_ignored():
    # #155 K2: more than 1 day after the reference is future -> ignored
    res = _build("On 2026-09-25 I bought a notebook.")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"
    assert res.note == "future_source_date_ignored"


# --------------------------------------------------------------------------- relative phrases


@pytest.mark.unit
def test_relative_yesterday_resolves_from_reference():
    res = _build("I bought a notebook yesterday.")
    assert res.built
    assert res.assertion.valid_at == "2026-09-22T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note is None


@pytest.mark.unit
def test_relative_days_ago_resolves_from_reference():
    res = _build("I bought a notebook 3 days ago.")
    assert res.built
    assert res.assertion.valid_at == "2026-09-20T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"


@pytest.mark.unit
def test_relative_word_weeks_ago_resolves_from_reference():
    res = _build("I bought a notebook two weeks ago.")
    assert res.built
    assert res.assertion.valid_at == "2026-09-09T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"


@pytest.mark.unit
def test_relative_conflicting_model_when_still_uses_computed_date():
    res = _build("I bought a notebook yesterday.", when="2026-09-01")
    assert res.built
    assert res.assertion.valid_at == "2026-09-22T00:00:00+00:00"
    assert res.assertion.time_basis == "explicit"
    assert res.note == "when_conflict_used_relative"


@pytest.mark.unit
@pytest.mark.parametrize("phrase", ["last week", "recently"])
def test_unsupported_relative_phrases_fall_back(phrase):
    res = _build(f"I bought a notebook {phrase}.")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"


# --------------------------------------------------------------------------- malformed when


@pytest.mark.unit
def test_malformed_when_falls_back_to_episode_reference():
    res = _build("I bought a notebook.", when="notadate")
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"


# --------------------------------------------------------------------------- abstention unchanged


@pytest.mark.unit
def test_no_usable_reference_and_no_source_date_abstains():
    res = _build("I bought a notebook.", ref=None)
    assert not res.built
    assert res.reason == "no_valid_time"
