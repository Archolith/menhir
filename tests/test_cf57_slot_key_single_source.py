"""CF-57 — one shared scalar slot-key helper.

The two byte-identical ``_slot_of`` copies are now a single imported object,
``TypedAssertion.slot_key`` delegates to the same helper, and the object path
and row path must agree exactly. Normalization and the fail-closed View slot
are preserved.
"""

from __future__ import annotations

import pytest

import menhir.domain.scalar_history as scalar_history_mod
import menhir.domain.scalar_state_fold as scalar_state_fold_mod
from menhir.domain.typed_assertion import TypedAssertion, slot_of
from menhir.infrastructure.view_models import ScalarStateKind


@pytest.mark.unit
def test_fold_and_history_share_one_slot_of_object() -> None:
    # The two byte-identical copies are now one imported object.
    assert scalar_history_mod._slot_of is scalar_state_fold_mod._slot_of
    assert scalar_history_mod._slot_of is slot_of


@pytest.mark.unit
def test_object_and_row_paths_agree() -> None:
    # The merge's load-bearing assumption: a TypedAssertion and its equivalent
    # row dict must produce the same key.
    ta = TypedAssertion(
        subject_uuid="ent-1",
        subject_display="display",
        attribute="owned",
        scope="pre-1920",
        value_kind="count",
        unit="usd",
        operation="absolute",
        value=37,
        stated_span="37 coins",
        episode_uuid="ep-1",
        valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00",
    )
    row = {
        "subject_uuid": "ent-1",
        "attribute": "owned",
        "scope": "pre-1920",
        "value_kind": "count",
        "unit": "usd",
    }
    assert ta.slot_key == slot_of(row)
    assert ta.slot_key == ("ent-1", "owned", "pre-1920", "count", "usd")


@pytest.mark.parametrize(
    "over",
    [
        {"attribute": "Owned", "scope": " pre-1920", "value_kind": "count", "unit": ""},
        {"attribute": "  owned  ", "scope": "PRE-1920", "value_kind": " Count ", "unit": "  "},
        {"attribute": "owned", "scope": "pre-1920", "value_kind": "count", "unit": None},
        {"attribute": "owned", "scope": "pre-1920", "value_kind": "count"},
    ],
)
def test_normalization_is_preserved(over) -> None:
    # Case, surrounding whitespace, and blank/missing units must not change the
    # key — properties the duplicates all shared; the merge must not lose one.
    row = {"subject_uuid": "ent-1", **over}
    assert slot_of(row) == ("ent-1", "owned", "pre-1920", "count", "")


@pytest.mark.unit
def test_typed_assertion_slot_key_normalization_is_preserved() -> None:
    # The object path must apply the same case/whitespace/blank-unit rules.
    # value_kind is validated canonically by the constructor, so only the
    # free-form attribute/scope (and blank unit) are varied here.
    ta = TypedAssertion(
        subject_uuid="ent-1",
        subject_display="display",
        attribute="  Owned  ",
        scope="PRE-1920",
        value_kind="count",
        unit=" ",
        operation="absolute",
        value=37,
        stated_span="37 coins",
        episode_uuid="ep-1",
        valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00",
    )
    assert ta.slot_key == ("ent-1", "owned", "pre-1920", "count", "")


@pytest.mark.unit
def test_scalar_state_kind_slot_still_fails_closed() -> None:
    # ScalarStateKind._slot keeps its own fail-closed body (see brief/report);
    # it must still reject what the shared slot_of would accept.
    with pytest.raises(ValueError):
        ScalarStateKind._slot({"attribute": "", "scope": "", "value_kind": "count", "unit": ""})
    with pytest.raises(ValueError):
        ScalarStateKind._slot({"attribute": "owned", "scope": "", "value_kind": "bogus", "unit": ""})


@pytest.mark.unit
def test_positive_control_distinct_slots_differ() -> None:
    # Two genuinely different slots must still produce different keys.
    a = slot_of({"subject_uuid": "ent-1", "attribute": "owned", "scope": "", "value_kind": "count", "unit": ""})
    b = slot_of({"subject_uuid": "ent-1", "attribute": "sold", "scope": "", "value_kind": "count", "unit": ""})
    assert a != b
