"""#155 grounded event dates — offline unit tests, no live model/network/db.

Event `valid_at` must be grounded in SOURCE evidence inside the sentence containing the claim span,
never in an ungrounded model `when`. Proves: the repro (model 2099 date ignored -> episode reference),
explicit full source dates (ISO and 'Month D, YYYY'), sentence-locality, future source dates ignored,
month+day-without-year resolution to the most recent year <= the reference, supported relative phrases,
malformed `when` no longer dropping the event, unchanged abstention without a usable reference,
persistence of the resolved valid_at/time_basis into repository write params, and the v2 version bump.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.typed_assertion import perceiver_rank
from menhir.infrastructure.typed_event_repository import TypedEventAssertionRepository
from menhir.services.event_history_perception import (
    build_event_assertion,
    extract_events_once,
)

REF = "2026-09-23T12:00:00+00:00"


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _llm(rows) -> "object":
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)

    return complete


def _event(**over):
    base = dict(
        episode=0, subject="user", predicate="bought", object="notebook",
        object_display="", domain="", when="", stated_span="bought a notebook",
    )
    base.update(over)
    return base


def _extract(content: str, *, when: str = "", on_note=None, on_drop=None):
    episodes = [_Ep(uuid="ep-0", content=content)]
    rows = [{"episode": 0, "events": [_event(when=when)]}]
    out = extract_events_once(
        episodes, _llm(rows), on_drop=on_drop, on_note=on_note)
    assert len(out) == 1
    return out[0]


def _build(content: str, *, when: str = "", ref: str | None = REF):
    p = _extract(content, when=when)
    return build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-09-30T00:00:00+00:00",
        episode_reference_time=ref, turn_evidence_uuid="te-1", perceiver_version="v2",
    )


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
def test_malformed_when_no_longer_drops_the_event():
    drops: list[str] = []
    notes: list[str] = []
    p = _extract("I bought a notebook.", when="notadate", on_drop=drops.append,
                 on_note=notes.append)
    assert drops == []
    assert notes == ["malformed_when_ignored"]
    res = build_event_assertion(
        p, subject_uuid="u-1", namespace="n", learned_at="2026-09-30T00:00:00+00:00",
        episode_reference_time=REF, turn_evidence_uuid="te-1", perceiver_version="v2",
    )
    assert res.built
    assert res.assertion.valid_at == REF
    assert res.assertion.time_basis == "episode_reference"


# --------------------------------------------------------------------------- abstention unchanged


@pytest.mark.unit
def test_no_usable_reference_and_no_source_date_abstains():
    res = _build("I bought a notebook.", ref=None)
    assert not res.built
    assert res.reason == "no_valid_time"


# --------------------------------------------------------------------------- persistence


@pytest.mark.unit
def test_resolved_valid_at_and_time_basis_reach_repository_write_params():
    class FakeNeo4j:
        def execute(self, query, params=None):
            return []

    res = _build("On 2026-07-18 I bought a notebook.", when="2026-08-01")
    assert res.built and res.assertion is not None
    params = TypedEventAssertionRepository(FakeNeo4j())._to_params(res.assertion)
    assert params["valid_at_raw"] == "2026-07-18T00:00:00+00:00"
    assert params["time_basis"] == "explicit"


# --------------------------------------------------------------------------- version bump


@pytest.mark.unit
def test_event_perceiver_default_is_v2_and_outranks_v1():
    assert MemorySettings.personal_memory_event_history_perceiver_version == "v2"
    assert perceiver_rank("v2") > perceiver_rank("v1")
