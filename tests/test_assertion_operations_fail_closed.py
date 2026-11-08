"""An operation the table does not know means NOTHING to every reader (fail closed).

A TypedAssertion row stored with an operation no reader has learned ("zz_future"), with the
operation key missing, or with operation=None must neither change a result nor appear in one: not
as a value, a contributor, a history entry, a count, a demotion, nor a slot of its own. Each test
pairs the unknown row with a positive control (a known operation in the same position that DOES move
the reader), so a reader that ignores everything cannot pass.
"""

from __future__ import annotations

import pytest

from menhir.domain.scalar_history import build_history
from menhir.domain.scalar_state_fold import NO_ANCHOR, fold_assertions
from menhir.domain.scalar_view_freshness import ObservationTime, newer_unfolded_evidence
from menhir.services.recall_policies import (
    _hydrate_contributor,
    _scalar_history_records_change,
)
from menhir.services.typed_scalar_persistence import _counting_self_echo

pytestmark = pytest.mark.unit

UNKNOWN_ID = "unk"
# (label, how the row's operation is spelled): a future name, an absent key, an explicit None.
UNKNOWN_FORMS = ["zz_future", "__missing__", None]


def _row(**over):
    base = dict(
        assertion_id="a?", subject_uuid="ent-coins", subject_display="my coins",
        attribute="owned", scope="pre-1920", value_kind="count", unit="", operation="absolute",
        value=37, valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="user", episode_uuid="ep-1", turn_id="t1", stated_span="37 coins",
        binding_pending=False,
    )
    base.update(over)
    if base["operation"] == "__missing__":
        del base["operation"]
    return base


def _unknown(form, **over):
    """A same-slot row of unknown operation, NEWER than the known rows and carrying a number."""
    return _row(assertion_id=UNKNOWN_ID, operation=form, value=999,
                valid_at="2026-09-01T00:00:00+00:00", **over)


def _ids(obj) -> str:
    return repr(obj)


# --------------------------------------------------------------------------- scalar fold

@pytest.mark.parametrize("form", UNKNOWN_FORMS)
def test_fold_ignores_an_unknown_operation_in_a_stated_slot(form):
    known = [
        _row(assertion_id="abs", value=10, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="dlt", operation="delta", value=2, valid_at="2026-08-01T00:00:00+00:00"),
    ]
    base = fold_assertions(known)
    assert [s.value for s in base.states] == [12]  # the control: the fold really folds

    with_unknown = fold_assertions(known + [_unknown(form)])
    assert with_unknown == base
    assert UNKNOWN_ID not in _ids(with_unknown)

    # positive control: the same newer row as a known "absolute" DOES move the value.
    moved = fold_assertions(known + [_unknown("absolute")])
    assert [s.value for s in moved.states] == [999]


@pytest.mark.parametrize("form", UNKNOWN_FORMS)
def test_fold_unknown_operation_alone_creates_no_slot_and_no_abstention(form):
    result = fold_assertions([_unknown(form)])
    assert (result.states, result.abstentions, result.expiries) == ([], [], [])

    # positive control: a lone known non-anchor row does create its slot (as an abstention).
    control = fold_assertions([_unknown("delta")])
    assert [a.reason for a in control.abstentions] == [NO_ANCHOR]


@pytest.mark.parametrize("form", UNKNOWN_FORMS)
def test_fold_unknown_operation_does_not_end_or_dedupe_a_value(form):
    # an unknown row newer than the absolute must not read as an "expire" ...
    known = [_row(assertion_id="abs", value=10)]
    result = fold_assertions(known + [_unknown(form)])
    assert result == fold_assertions(known) and result.expiries == []
    # ... positive control: a newer known expire does end it.
    assert fold_assertions(known + [_unknown("expire")]).expiries


def _occ(aid, **over):
    base = dict(
        assertion_id=aid, operation="occurrence", value=1, value_kind="count", attribute="visits",
        scope="", importance="notable", count_status="counted", grounding_id=f"turn-{aid}",
        valid_at="2026-07-01T00:00:00+00:00",
    )
    base.update(over)
    return _row(**base)


@pytest.mark.parametrize("form", UNKNOWN_FORMS)
@pytest.mark.parametrize("count_match", [False, True])
def test_fold_counting_is_unchanged_by_an_unknown_operation(form, count_match):
    known = [_occ("o1"), _occ("o2", valid_at="2026-07-02T00:00:00+00:00")]
    base = fold_assertions(known, count_match=count_match)
    assert [s.value for s in base.states] == [2]

    extra = _occ(UNKNOWN_ID, operation=form, grounding_id="turn-unk", count_status="counted",
                 valid_at="2026-07-03T00:00:00+00:00")
    with_unknown = fold_assertions(known + [extra], count_match=count_match)
    assert with_unknown == base
    assert UNKNOWN_ID not in _ids(with_unknown)

    # positive control: as a real occurrence it is counted.
    counted = fold_assertions(known + [_occ(UNKNOWN_ID, grounding_id="turn-unk",
                                            valid_at="2026-07-03T00:00:00+00:00")],
                              count_match=count_match)
    assert [s.value for s in counted.states] == [3]


# ------------------------------------------------------------------------- scalar history

@pytest.mark.parametrize("form", UNKNOWN_FORMS)
@pytest.mark.parametrize("count_match", [False, True])
def test_history_adds_no_entry_for_an_unknown_operation(form, count_match):
    known = [
        _row(assertion_id="abs", value=10),
        _row(assertion_id="dlt", operation="delta", value=2, valid_at="2026-08-01T00:00:00+00:00"),
    ]
    base = build_history(known, count_match=count_match)
    assert [e.assertion_id for e in base.projections[0].all_entries] == ["abs", "dlt"]

    with_unknown = build_history(known + [_unknown(form)], count_match=count_match)
    assert with_unknown == base  # entries, operation_counts, signature, last_valid_at all equal
    assert UNKNOWN_ID not in _ids(with_unknown)

    # positive control: a known operation in the same position is listed.
    listed = build_history(known + [_unknown("expire")], count_match=count_match)
    assert [e.assertion_id for e in listed.projections[0].all_entries] == ["abs", "dlt", UNKNOWN_ID]


@pytest.mark.parametrize("form", UNKNOWN_FORMS)
@pytest.mark.parametrize("count_match", [False, True])
def test_history_unknown_operation_alone_makes_no_projection_or_abstention(form, count_match):
    result = build_history([_unknown(form)], count_match=count_match)
    assert (result.projections, result.abstentions) == ([], [])
    assert build_history([_unknown("delta")], count_match=count_match).projections  # control


# ------------------------------------------------------------------------- freshness gate

SLOT = ("ent-postcards", "postcard_count", "collection", "count", "postcards")
VIEW_AT = "2026-07-01T00:00:00+00:00"


@pytest.mark.parametrize("op", ["zz_future", None, ""])
def test_freshness_gate_is_not_demoted_by_a_newer_unknown_operation(op):
    def obs(aid, operation):
        return ObservationTime(aid, SLOT, operation, "2026-08-01T00:00:00Z")

    assert newer_unfolded_evidence(SLOT, VIEW_AT, [obs("u1", op)]) == ()
    # positive control: a newer same-slot occurrence still demotes, alongside the unknown one.
    assert newer_unfolded_evidence(SLOT, VIEW_AT, [obs("u1", op), obs("o1", "occurrence")]) == ("o1",)


# ------------------------------------------------------------------------- recall policies

def _entry(op, value=5):
    return {"operation": op, "value": value}


@pytest.mark.parametrize("op", ["zz_future", None, ""])
def test_history_change_check_does_not_count_an_unknown_entry_as_a_change(op):
    restated = {"entries": [_entry("absolute"), _entry("absolute")]}
    assert _scalar_history_records_change(restated) is False  # the control: a mere restatement
    with_unknown = {"entries": [_entry("absolute"), _entry("absolute"), _entry(op, 999)]}
    assert _scalar_history_records_change(with_unknown) is False
    # positive control: a known non-absolute entry is a change.
    assert _scalar_history_records_change(
        {"entries": [_entry("absolute"), _entry("delta", 2)]}) is True


@pytest.mark.parametrize("op", ["zz_future", None])
def test_contributor_hydration_gives_an_unknown_operation_no_occurrence_timing(op):
    row = {"assertion_id": "c1", "operation": op, "value": 3, "stated_span": "I went twice today",
           "valid_at": "2026-07-01T00:00:00+00:00", "time_basis": "episode_reference",
           "turn_text": "I went twice today."}
    unknown = _hydrate_contributor(row)
    known = _hydrate_contributor({**row, "operation": "occurrence"})
    assert known.mentioned_at is not None  # control: an occurrence gets event-time fields
    assert unknown.mentioned_at is None and unknown.event_at is None


# ------------------------------------------------------------------ typed scalar persistence

def _echo(operation):
    return _counting_self_echo(
        operation=operation, claim_ordinal=_counting_ordinal(), subject_text="the museum",
        count_object="museum", count_item="museum visit", count_family="museum",
        attribute="museum_visit_count")


def _counting_ordinal():
    from menhir.services.counting_consolidation import COUNTING_CLAIM_ORDINAL
    return COUNTING_CLAIM_ORDINAL


@pytest.mark.parametrize("op", ["zz_future", None, ""])
def test_self_echo_rewrite_applies_only_to_counting_occurrences(op):
    assert _echo("occurrence") is True  # control: same inputs, known operation
    assert _echo(op) is False
