"""Focused contract tests for cumulative activity scalars.

These tests deliberately use generic activities (wearing an owned item, visiting a museum, and
running) rather than a benchmark panel.  A cumulative report is a scalar observation of a running
total; it is not a bag of synthetic event occurrences.  The tests exercise the existing typed
scalar parser, k-sample gate, and scalar-state fold without a live model, Neo4j, or network calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.domain.scalar_state_fold import fold_assertions
from menhir.services.typed_scalar_perception import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    extract_typed_scalars_once,
    gate_typed_scalars,
)
from menhir.services.typed_scalar_rules import TypedScalarProposal, classify_absolute_semantics


@dataclass(frozen=True)
class _Episode:
    uuid: str
    content: str
    reference_time: str | None = "2026-08-08T00:00:00+00:00"


def _row(**over):
    row = {
        "episode": 0,
        "subject": "my black Converse",
        "attribute": "wear_count",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 6,
        "when": "",
        "stated_span": "I've worn my black Converse six times",
    }
    row.update(over)
    return row


def _llm(rows):
    def complete(_system: str, _user: str) -> str:
        return json.dumps(rows)

    return complete


def _extract(content: str, rows: list[dict], *, reference_time: str | None = None):
    episode = _Episode("ep-activity", content, reference_time)
    return extract_typed_scalars_once([episode], _llm(rows))


def _proposal(**over) -> TypedScalarProposal:
    values = {
        "subject_text": "my black Converse",
        "attribute": "wear_count",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 6,
        "stated_span": "I've worn my black Converse six times",
        "episode_uuid": "ep-activity",
        "span_start": 0,
        "span_end": len("I've worn my black Converse six times"),
        "when": "2026-08-08T00:00:00+00:00",
    }
    values.update(over)
    return TypedScalarProposal(**values)


def _assertion(**over):
    row = {
        "assertion_id": "a?",
        "subject_uuid": "entity-black-converse",
        "subject_display": "my black Converse",
        "attribute": "wear_count",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 6,
        "valid_at": "2026-08-08T00:00:00+00:00",
        "learned_at": "2026-08-08T00:00:00+00:00",
        "evidence_tier": "user",
        "episode_uuid": "ep-activity",
        "binding_pending": False,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- prompt contract


@pytest.mark.unit
def test_prompt_defines_activity_total_as_scalar_not_synthetic_events():
    prompt = TYPED_SCALAR_SYSTEM_PROMPT.lower()
    assert "activity totals vs occurrences vs frequency" in prompt
    assert "current cumulative count" in prompt
    assert "a single occurrence" in prompt
    assert "do not emit six event observations" in prompt
    assert "keep that object as subject" in prompt
    assert "distinct activity object" in prompt
    assert "subject='the museum'" in prompt


@pytest.mark.unit
def test_prompt_names_activity_total_and_delta_semantics():
    prompt = TYPED_SCALAR_SYSTEM_PROMPT.lower()
    assert "stable activity-family attribute ending in '_count'" in prompt
    assert "is operation=delta" in prompt
    assert "recurring rate" in prompt


# --------------------------------------------------------------------------- parsed/gated positives


@pytest.mark.unit
def test_cumulative_activity_total_is_one_absolute_count():
    source = "I've worn my black Converse six times."
    out = _extract(source, [_row()])

    assert len(out) == 1
    proposal = out[0]
    assert proposal.subject_text == "my black Converse"
    assert proposal.attribute == "wear_count"
    assert proposal.value_kind == "count"
    assert proposal.unit == ""
    assert proposal.operation == "absolute"
    assert proposal.value == 6
    assert proposal.stated_span == "I've worn my black Converse six times"


@pytest.mark.unit
def test_owned_object_activity_stays_on_object_subject():
    source = "I've launched my red kayak three times."
    out = _extract(
        source,
        [_row(
            subject="my red kayak",
            attribute="launch_count",
            value=3,
            stated_span="I've launched my red kayak three times",
        )],
    )

    assert len(out) == 1
    assert out[0].subject_text == "my red kayak"
    assert out[0].subject_text != "user"
    assert out[0].attribute == "launch_count"


@pytest.mark.unit
def test_three_consistent_samples_commit_one_activity_slot():
    samples = [[_proposal()] for _ in range(3)]
    decisions = gate_typed_scalars(samples, threshold=1.0)

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.committed is True
    assert decision.proposal is not None
    assert decision.proposal.slot_key == ("wear_count", "", "count", "")
    assert decision.proposal.subject_text == "my black Converse"


@pytest.mark.unit
def test_later_explicit_delta_has_delta_operation_and_same_slot():
    source = "I wore my black Converse two more times."
    out = _extract(
        source,
        [_row(
            operation="delta",
            value=2,
            stated_span="I wore my black Converse two more times",
        )],
    )

    assert len(out) == 1
    assert out[0].operation == "delta"
    assert out[0].value == 2
    assert out[0].slot_key == ("wear_count", "", "count", "")


@pytest.mark.unit
def test_present_perfect_single_total_is_a_cumulative_count():
    source = "I've worn my black Converse once."
    out = _extract(
        source,
        [_row(value=1, stated_span="I've worn my black Converse once")],
    )

    assert len(out) == 1
    assert out[0].operation == "absolute"
    assert out[0].value == 1


@pytest.mark.unit
def test_explicit_delta_without_an_anchor_does_not_create_authority():
    source = "I wore my black Converse two more times."
    out = _extract(
        source,
        [_row(
            operation="delta",
            value=2,
            stated_span="I wore my black Converse two more times",
        )],
    )
    assert len(out) == 1

    folded = fold_assertions([_assertion(operation="delta", value=2, assertion_id="delta-only")])
    assert folded.states == []
    assert folded.abstentions and folded.abstentions[0].reason == "no_anchor"


@pytest.mark.unit
def test_activity_frequency_stays_frequency_not_cumulative_count():
    source = "I wear my black Converse twice a week."
    out = _extract(
        source,
        [_row(
            attribute="wear_frequency",
            value_kind="frequency",
            unit="week",
            value=2,
            stated_span="I wear my black Converse twice a week",
        )],
    )

    assert len(out) == 1
    assert out[0].attribute == "wear_frequency"
    assert out[0].value_kind == "frequency"
    assert out[0].unit == "week"
    assert out[0].value == 2
    assert out[0].attribute != "wear_count"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "span", "value"),
    [
        (
            "I might have worn my black Converse six times.",
            "I might have worn my black Converse six times",
            6,
        ),
        (
            "If I wore my black Converse six times, they would be worn in.",
            "I wore my black Converse six times",
            6,
        ),
        (
            "I've worn my black Converse about six times.",
            "I've worn my black Converse about six times",
            6,
        ),
        (
            "I've worn my black Converse a few times.",
            "I've worn my black Converse a few times",
            3,
        ),
        (
            "I've worn my black Converse six or seven times.",
            "I've worn my black Converse six or seven times",
            6,
        ),
    ],
)
def test_tentative_hypothetical_approximate_or_vague_total_abstains(source, span, value):
    assert _extract(source, [_row(value=value, stated_span=span)]) == []


@pytest.mark.unit
def test_modal_activity_count_without_times_token_abstains():
    source = "I might run 12 races this year."
    assert _extract(
        source,
        [_row(
            subject="user",
            attribute="race_count",
            value=12,
            stated_span="I might run 12 races this year",
        )],
    ) == []


@pytest.mark.unit
def test_modal_activity_delta_abstains():
    source = "I might wear my black Converse two more times."
    assert _extract(
        source,
        [_row(
            operation="delta",
            value=2,
            stated_span="I might wear my black Converse two more times",
        )],
    ) == []


@pytest.mark.unit
def test_windowed_present_perfect_activity_total_abstains_until_window_identity_exists():
    source = "I've run the trail six times during the trip."
    assert _extract(
        source,
        [_row(
            subject="the trail",
            attribute="run_count",
            value=6,
            stated_span="I've run the trail six times",
        )],
    ) == []


@pytest.mark.unit
def test_single_known_occurrence_is_event_not_cumulative_scalar():
    source = "I wore my black Converse once at the concert."
    assert _extract(
        source,
        [_row(value=1, stated_span="I wore my black Converse once at the concert")],
    ) == []


@pytest.mark.unit
def test_past_occurrence_is_not_current_count():
    source = "I wore my black Converse yesterday."
    assert _extract(
        source,
        [_row(value=1, stated_span="I wore my black Converse yesterday")],
    ) == []


@pytest.mark.unit
def test_trailing_future_clause_does_not_veto_the_grounded_current_count():
    source = "I have 3 books that I will read."
    out = _extract(
        source,
        [_row(
            subject="user",
            attribute="book_count",
            value=3,
            stated_span="I have 3 books",
        )],
    )
    assert len(out) == 1
    assert out[0].attribute == "book_count"


@pytest.mark.unit
def test_trailing_modal_inside_grounded_span_does_not_veto_current_count():
    source = "I have 3 books I can lend."
    out = _extract(
        source,
        [_row(
            subject="user",
            attribute="book_count",
            value=3,
            stated_span="I have 3 books I can lend",
        )],
    )
    assert len(out) == 1
    assert out[0].value == 3


@pytest.mark.unit
def test_trailing_modal_qualification_does_not_veto_activity_total():
    source = "I've worn my black Converse six times that I can remember."
    out = _extract(
        source,
        [_row(
            value=6,
            stated_span="I've worn my black Converse six times that I can remember",
        )],
    )
    assert len(out) == 1
    assert out[0].value == 6


@pytest.mark.unit
def test_calendar_month_may_is_not_treated_as_a_modal():
    source = "My May reading list has 3 books."
    out = _extract(
        source,
        [_row(
            subject="my May reading list",
            attribute="book_count",
            value=3,
            stated_span="My May reading list has 3 books",
        )],
    )
    assert len(out) == 1
    assert out[0].subject_text == "my May reading list"


@pytest.mark.unit
def test_adjectival_must_read_is_not_treated_as_a_modal():
    source = "I have 3 must-read books."
    out = _extract(
        source,
        [_row(
            subject="user",
            attribute="book_count",
            value=3,
            stated_span="I have 3 must-read books",
        )],
    )
    assert len(out) == 1
    assert out[0].value == 3


# ------------------------------------------------------ grammar/spelling/unrelated activities


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "span", "subject", "value"),
    [
        (
            "Ive worn my black converse 6 times.",
            "Ive worn my black converse 6 times",
            "my black converse",
            6,
        ),
        (
            "I've wore my black sneakers six times.",
            "I've wore my black sneakers six times",
            "my black sneakers",
            6,
        ),
        ("I've visited the museum 4 times.", "I've visited the museum 4 times", "the museum", 4),
        ("Ive run the trail three times.", "Ive run the trail three times", "the trail", 3),
    ],
)
def test_activity_total_grounding_handles_grammar_spelling_and_other_domains(
    source, span, subject, value,
):
    out = _extract(
        source,
        [_row(
            subject=subject,
            attribute=(
                "visit_count"
                if "museum" in source
                else "run_count"
                if "trail" in source
                else "wear_count"
            ),
            value=value,
            stated_span=span,
        )],
    )
    assert len(out) == 1
    assert out[0].subject_text == subject
    assert out[0].value == value
    assert out[0].stated_span == span


# ------------------------------------------------------ scalar fold/correction behavior


@pytest.mark.unit
def test_two_activity_totals_same_slot_keep_history_and_latest_wins():
    folded = fold_assertions([
        _assertion(assertion_id="old", value=4, valid_at="2026-08-01T00:00:00+00:00"),
        _assertion(assertion_id="new", value=6, valid_at="2026-08-08T00:00:00+00:00"),
    ])

    assert len(folded.states) == 1
    state = folded.states[0]
    assert state.value == 6
    assert state.anchor_id == "new"
    assert state.contributor_ids == ["new"]
    assert state.superseded_anchor_ids == ["old"]


@pytest.mark.unit
def test_explicit_delta_after_activity_anchor_folds_into_total():
    folded = fold_assertions([
        _assertion(assertion_id="anchor", value=4, valid_at="2026-08-01T00:00:00+00:00"),
        _assertion(
            assertion_id="delta",
            operation="delta",
            value=2,
            valid_at="2026-08-08T00:00:00+00:00",
        ),
    ])

    assert len(folded.states) == 1
    state = folded.states[0]
    assert state.value == 6
    assert state.anchor_value == 4
    assert state.delta_total == 2.0
    assert state.contributor_ids == ["anchor", "delta"]


@pytest.mark.unit
def test_same_time_correction_resolves_activity_total():
    span = "Actually, I've worn my black Converse six times"
    assert classify_absolute_semantics(span, "absolute") == "correction"

    folded = fold_assertions([
        _assertion(
            assertion_id="ordinary-four",
            value=4,
            valid_at="2026-08-08T00:00:00+00:00",
            learned_at="2026-08-08T00:00:01+00:00",
        ),
        _assertion(
            assertion_id="correction-six",
            value=6,
            valid_at="2026-08-08T00:00:00+00:00",
            learned_at="2026-08-08T00:00:02+00:00",
            absolute_semantics="correction",
        ),
    ])

    assert len(folded.states) == 1
    assert folded.states[0].value == 6
    assert folded.states[0].anchor_id == "correction-six"
