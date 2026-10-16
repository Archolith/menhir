"""Occurrence groundwork for the counting lane (#170, plan menhir-scalar-counting-plan-2026-10-15).

Pure/offline. An `occurrence` is one happening on a count/money slot. A slot with NO stated absolute
folds its occurrences into a lower-bound value with basis=counted; a slot WITH a stated absolute is
untouched by occurrences (adding them onto a stated total is a separate, later step). The scalar
parser can never emit an occurrence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from menhir.domain.scalar_state_fold import (
    BASIS_COUNTED,
    NO_ANCHOR,
    ROUTINE_ONLY,
    fold_assertions,
)
from menhir.domain.typed_assertion import OPERATIONS, TypedAssertion
from menhir.infrastructure.typed_assertion_models import _RECORD_CYPHER
from menhir.infrastructure.typed_assertion_repair_repository import (
    TypedAssertionRepairMixin as TypedAssertionRepairRepository,
)
from menhir.services.scalar_state_service import ScalarStateService
from menhir.services.typed_scalar_rules import parse_scalar_row


def _row(**over):
    base = dict(
        assertion_id="o?", subject_uuid="ent-self", subject_display="me",
        attribute="yoga_count", scope="", value_kind="count", unit="", operation="occurrence",
        value=1, importance="notable", valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", evidence_tier="user", episode_uuid="ep-1",
        binding_pending=False,
    )
    base.update(over)
    return base


def _occ(i, day, **over):
    return _row(assertion_id=f"o{i}", episode_uuid=f"ep-{i}",
                valid_at=f"2026-07-{day:02d}T00:00:00+00:00", **over)


# ------------------------------------------------------------------ counted slots (no stated total)

@pytest.mark.unit
def test_occurrences_without_a_stated_total_fold_to_a_counted_lower_bound():
    r = fold_assertions([_occ(1, 1), _occ(2, 5), _occ(3, 9)])
    assert not r.abstentions
    s = r.states[0]
    assert s.value == 3 and s.basis == BASIS_COUNTED
    assert s.contributor_ids == ["o1", "o2", "o3"] == s.contributed_occurrence_ids
    assert s.valid_at == "2026-07-09T00:00:00+00:00"
    assert s.anchor_value is None and s.anchor_id == ""
    assert s.episode_uuids == ["ep-1", "ep-2", "ep-3"]


@pytest.mark.unit
def test_routine_only_slot_does_not_materialize():
    r = fold_assertions([_occ(1, 1, importance="routine"), _occ(2, 2, importance="routine")])
    assert r.states == [] and r.abstentions[0].reason == ROUTINE_ONLY


@pytest.mark.unit
def test_unlabelled_importance_fails_closed_as_routine():
    r = fold_assertions([_occ(1, 1, importance=""), _occ(2, 2, importance=None)])
    assert r.states == [] and r.abstentions[0].reason == ROUTINE_ONLY


@pytest.mark.unit
def test_one_notable_occurrence_materializes_and_routine_ones_still_count():
    r = fold_assertions([_occ(1, 1, importance="routine"), _occ(2, 2, importance="tracked")])
    assert r.states[0].value == 2


@pytest.mark.unit
def test_a_single_tracked_occurrence_is_enough():
    assert fold_assertions([_occ(1, 1, importance="tracked")]).states[0].value == 1


@pytest.mark.unit
def test_occurrence_from_a_turn_that_gave_the_slot_a_delta_is_absorbed():
    # "visited three more times" -> the scalar lane's +3 delta; the counting lane's occurrence from
    # the SAME turn must not add one more. The other turn's occurrence still counts.
    rows = [
        _occ(1, 1),
        _occ(2, 2),
        _row(assertion_id="d2", operation="delta", value=3, importance="", episode_uuid="ep-2",
             valid_at="2026-07-02T00:00:00+00:00"),
    ]
    s = fold_assertions(rows).states[0]
    assert s.value == 1 and s.contributor_ids == ["o1"]
    assert s.absorbed_occurrence_ids == ["o2"]


@pytest.mark.unit
def test_all_occurrences_absorbed_leaves_no_anchor():
    rows = [
        _occ(1, 1),
        _row(assertion_id="d1", operation="delta", value=2, importance="", episode_uuid="ep-1",
             valid_at="2026-07-01T00:00:00+00:00"),
    ]
    r = fold_assertions(rows)
    assert r.states == [] and r.abstentions[0].reason == NO_ANCHOR


@pytest.mark.unit
def test_money_occurrences_sum_exactly():
    rows = [
        _occ(1, 1, attribute="coffee_spend", value_kind="money", unit="usd", value=Decimal("4.10")),
        _occ(2, 2, attribute="coffee_spend", value_kind="money", unit="usd", value=Decimal("4.20")),
    ]
    s = fold_assertions(rows).states[0]
    assert s.value == Decimal("8.30") and s.basis == BASIS_COUNTED


@pytest.mark.unit
def test_future_occurrences_wait_for_their_valid_at():
    rows = [_occ(1, 1), _occ(2, 20)]
    as_of = datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert fold_assertions(rows, as_of=as_of).states[0].value == 1


@pytest.mark.unit
def test_a_later_expire_still_ends_the_slot():
    rows = [_occ(1, 1), _row(assertion_id="e", operation="expire", value=1, importance="",
                             valid_at="2026-07-05T00:00:00+00:00")]
    r = fold_assertions(rows)
    assert r.states == [] and len(r.expiries) == 1


@pytest.mark.unit
def test_deltas_alone_still_abstain_no_anchor():
    rows = [_row(assertion_id="d1", operation="delta", value=2, importance="")]
    r = fold_assertions(rows)
    assert r.states == [] and r.abstentions[0].reason == NO_ANCHOR


# ------------------------------------------------------------------ stated slots are untouched

@pytest.mark.unit
def test_occurrences_do_not_change_a_slot_with_a_stated_total():
    stated = [
        _row(assertion_id="a1", operation="absolute", value=7, importance="",
             valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=1, importance="", episode_uuid="ep-d",
             valid_at="2026-07-03T00:00:00+00:00"),
    ]
    baseline = fold_assertions(stated)
    with_occ = fold_assertions(stated + [_occ(8, 8), _occ(9, 9)])
    assert with_occ.states == baseline.states and with_occ.abstentions == baseline.abstentions
    assert with_occ.states[0].value == 8 and with_occ.states[0].basis == "stated"


# ------------------------------------------------------------------ domain validation

def _assertion(**over):
    base = dict(
        subject_uuid="ent-self", subject_display="me", attribute="yoga_count", scope="",
        value_kind="count", unit="", operation="occurrence", value=1,
        stated_span="went to yoga", episode_uuid="ep-1", valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", importance="notable",
    )
    base.update(over)
    return TypedAssertion(**base)


@pytest.mark.unit
def test_occurrence_assertion_validates():
    a = _assertion()
    assert a.operation == "occurrence" and a.importance == "notable"


@pytest.mark.unit
@pytest.mark.parametrize("over, msg", [
    ({"importance": ""}, "importance"),
    ({"importance": "huge"}, "importance"),
    ({"value": 0}, "positive"),
    ({"value": -1}, "positive"),
    ({"value": 1.5}, "positive integer"),
    ({"value": [1, 2]}, "positive"),
    ({"value_kind": "clock_time", "value": "07:30"}, "occurrence requires"),
])
def test_bad_occurrences_are_rejected(over, msg):
    with pytest.raises(ValueError, match=msg):
        _assertion(**over)


@pytest.mark.unit
def test_importance_is_rejected_on_scalar_operations():
    with pytest.raises(ValueError, match="only on occurrence"):
        _assertion(operation="absolute", value=7, importance="notable")


@pytest.mark.unit
def test_importance_is_not_part_of_assertion_identity():
    a = _assertion(importance="notable")
    assert a.assertion_key == replace(a, importance="tracked").assertion_key


# ------------------------------------------------------------------ the scalar lane cannot emit one

@pytest.mark.unit
def test_scalar_operations_exclude_occurrence():
    assert "occurrence" not in OPERATIONS


@pytest.mark.unit
def test_scalar_parser_drops_an_occurrence_row():
    dropped: list[str] = []
    row = {"subject": "I", "attribute": "yoga_count", "operation": "occurrence",
           "stated_span": "went to yoga", "value_kind": "count", "value": 1}
    assert parse_scalar_row(row, [], dropped.append) is None
    assert dropped == ["bad_attribute_or_operation"]


# ------------------------------------------------------------------ persistence + View audit

@pytest.mark.unit
def test_record_params_carry_importance_and_blank_for_scalars():
    occ = TypedAssertionRepairRepository._to_params(_assertion())
    stated = TypedAssertionRepairRepository._to_params(
        _assertion(operation="absolute", value=7, importance=""))
    assert occ["importance"] == "notable" and stated["importance"] == ""
    assert "a.importance = CASE WHEN $importance = '' THEN null ELSE $importance END" in _RECORD_CYPHER


class _Assertions:
    def __init__(self, rows):
        self._rows = rows

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        return [r for r in self._rows if r["subject_uuid"] == subject_uuid]


class _Views:
    def __init__(self):
        self.calls = []

    def record_scalar_state(self, **kwargs):
        self.calls.append(kwargs)
        return {"view_key": f"vk-{kwargs['attribute']}"}

    def list_scalar_state_views(self, *, subject_uuid, namespace=None):
        return []

    def retire_scalar_state(self, *, view_key):
        return True


@pytest.mark.unit
def test_counted_view_audit_carries_basis_and_stated_audit_is_unchanged():
    rows = [
        _occ(1, 1),
        _row(assertion_id="a1", attribute="owned", operation="absolute", value=37, importance=""),
    ]
    views = _Views()
    ScalarStateService(_Assertions(rows), views).rebuild_scalar_state("ent-self")
    by_attr = {c["attribute"]: c["audit"] for c in views.calls}
    assert by_attr["yoga_count"]["scalar_basis"] == BASIS_COUNTED
    assert set(by_attr["owned"]) == {
        "scalar_contributors", "scalar_effective_tier", "scalar_anchor_value", "scalar_delta_total"}
