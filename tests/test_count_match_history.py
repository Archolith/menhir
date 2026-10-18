"""4R: scalar history under the match flag is keyed by object family, like the counted View, and
leaves out the rows the match fold leaves out (a repeat or same_event of a current counted row).
Before, history kept the raw extracted slots and listed every repeat."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.domain.scalar_history import build_history
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.services.scalar_state_service import ScalarStateService
from tests.test_scalar_history_service import FakeAssertionSource, FakeViewSink


def _occ(aid, day, *, attribute="model_kit_finished_count", status=None, target=None,
         obj="model kit", scope="", subject="s1"):
    return dict(
        assertion_id=aid, subject_uuid=subject, subject_display="me", attribute=attribute,
        scope=scope, value_kind="count", unit="", operation="occurrence", value=1,
        valid_at=f"2026-07-{day:02d}T00:00:00+00:00", evidence_tier="agent",
        episode_uuid=f"ep-{aid}", turn_id=f"ep-{aid}", stated_span=aid, importance="tracked",
        count_object=obj, count_status=status, count_target_id=target, binding_pending=False)


def _slots(result):
    return {(p.attribute, p.scope): [e.assertion_id for e in p.all_entries]
            for p in result.projections}


_ROWS = [
    _occ("a1", 1, status="counted"),
    _occ("a2", 2, attribute="kit_bought_count", scope="hobby", status="counted"),
    _occ("r1", 3, status="repeat", target="a1"),                       # left out
    _occ("e1", 4, attribute="kit_bought_count", status="same_event", target="a2"),  # left out
    _occ("d1", 5, status="repeat", target="gone"),                     # dead target: kept
    _occ("u1", 6, status="uncertain"),                                 # kept
    _occ("n1", 7),                                                     # undecided: kept
    _occ("x1", 8, attribute="museum_visit_count", obj=""),             # no family: own slot
]


@pytest.mark.unit
def test_match_history_is_family_keyed_and_leaves_out_repeats_of_counted_rows():
    # Fails if history keeps the raw extracted slots under match, lists a repeat/same_event of a
    # counted row, or drops a row the fold still shows (dead-target repeat, uncertain, undecided).
    got = _slots(build_history(_ROWS, count_match=True))
    assert got == {("model_kit_count", ""): ["a1", "a2", "d1", "u1", "n1"],
                   ("museum_visit_count", ""): ["x1"]}


@pytest.mark.unit
def test_match_off_history_is_unchanged():
    got = _slots(build_history(_ROWS))
    assert got == {
        ("model_kit_finished_count", ""): ["a1", "r1", "d1", "u1", "n1"],
        ("kit_bought_count", "hobby"): ["a2"],
        ("kit_bought_count", ""): ["e1"],
        ("museum_visit_count", ""): ["x1"],
    }


@pytest.mark.unit
def test_a_repeat_of_a_not_yet_active_counted_row_stays_listed():
    # Same as the fold: under as_of, a counted target that is still in the future is not counted,
    # so its repeat is uncertain and must stay listed.
    rows = [_occ("a1", 20, status="counted"), _occ("r1", 3, status="repeat", target="a1")]
    as_of = datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert _slots(build_history(rows, as_of=as_of, count_match=True)) == {
        ("model_kit_count", ""): ["r1"]}


@pytest.mark.unit
def test_non_occurrence_rows_are_untouched_by_match():
    absolute = dict(_occ("t1", 1), operation="absolute", attribute="kit_total", value=5,
                    count_status=None, importance="")
    assert _slots(build_history([absolute], count_match=True)) == {("kit_total", ""): ["t1"]}


@pytest.mark.unit
def test_history_lists_what_the_counted_view_counts_or_reports_uncertain():
    # Parity with the fold: every occurrence the match fold counts or reports as uncertain is in
    # the history of the same family slot, and nothing it excludes is.
    fold = fold_assertions(_ROWS, count_match=True)
    (state,) = [s for s in fold.states if s.attribute == "model_kit_count"]
    history = _slots(build_history(_ROWS, count_match=True))[("model_kit_count", "")]
    assert set(state.contributed_occurrence_ids) == {"a1", "a2"}
    assert state.count_uncertain == 3
    assert set(history) == {"a1", "a2"} | {"d1", "u1", "n1"}


def _same_turn(row, turn, item):
    return {**row, "turn_id": turn, "episode_uuid": turn, "count_item": item}


@pytest.mark.unit
def test_match_history_collapses_one_happening_per_turn_like_the_fold():
    # "I bought and finished the Gundam kit" is one turn, one item, two family-keyed rows: the
    # fold counts it once, so history must list it once. A counted row and an uncertain row for
    # the same turn and item likewise collapse to the counted one. Fails without the collapse.
    rows = [
        _same_turn(_occ("b1", 1, attribute="kit_bought_count", status="counted"), "t1", "gundam"),
        _same_turn(_occ("f1", 1, status="counted"), "t1", "gundam"),
        _same_turn(_occ("c2", 2, status="counted"), "t2", "zaku"),
        _same_turn(_occ("u2", 2, status="uncertain"), "t2", "zaku"),
        _same_turn(_occ("k3", 3, status="counted"), "t3", "rx78"),
    ]
    fold = fold_assertions(rows, count_match=True)
    (state,) = [s for s in fold.states if s.attribute == "model_kit_count"]
    history = _slots(build_history(rows, count_match=True))[("model_kit_count", "")]
    assert len(history) == len(state.contributed_occurrence_ids) + state.count_uncertain == 3
    assert set(history) == set(state.contributed_occurrence_ids)
    assert "u2" not in history


@pytest.mark.unit
def test_service_rebuild_under_match_writes_family_views_and_retires_raw_slot_ones():
    # Fails if a match rebuild keeps the old raw-slot history Views current next to the family View.
    sink = FakeViewSink()
    sink._history_views = [
        {"subject_uuid": "s1", "view_key": "raw-finished", "attribute": "model_kit_finished_count",
         "scope": "", "value_kind": "count", "unit": ""},
        {"subject_uuid": "s1", "view_key": "family", "attribute": "model_kit_count",
         "scope": "", "value_kind": "count", "unit": ""},
    ]
    svc = ScalarStateService(FakeAssertionSource(_ROWS), sink, scalar_history_enabled=True,
                             count_match=True)
    out = svc.rebuild_scalar_history("s1", namespace="ns-1")
    assert out["complete"] is True
    assert sorted(h["attribute"] for h in sink.scalar_histories) == [
        "model_kit_count", "museum_visit_count"]
    assert sink.retired_history_keys == ["raw-finished"]


@pytest.mark.unit
def test_service_with_counting_off_ignores_match():
    # count_match only applies while counting is on (the service folds the same way).
    sink = FakeViewSink()
    svc = ScalarStateService(FakeAssertionSource(_ROWS), sink, counting_enabled=False,
                             count_match=True)
    svc.rebuild_scalar_history("s1", namespace="ns-1")
    assert sink.scalar_histories == []
