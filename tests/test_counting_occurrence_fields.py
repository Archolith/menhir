"""Occurrence assertions carry optional object/item/action text (count_object/count_item/count_action)
from the LLM row to the TypedAssertion, like `importance`, and never enter any key or vote."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.typed_assertion_repair_repository import TypedAssertionRepairMixin
from menhir.services.typed_scalar_persistence import _assertion_from_pending_row
from menhir.services.typed_scalar_rules import (
    TypedScalarProposal,
    _assertion_from_proposal,
    gate_typed_scalars,
    parse_scalar_row,
)

from menhir.domain.typed_assertion import COUNTING_OPERATIONS


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


_EPISODES = [_Ep(uuid="ep-1", content="Today I went to yoga. I finished a Spitfire kit.")]


def _occ(**over):
    base = dict(
        episode=0, subject="user", attribute="yoga_sessions", scope="", value_kind="count",
        unit="", operation="occurrence", importance="tracked", stated_span="went to yoga",
    )
    base.update(over)
    return base


def _parse(row, **kw):
    return parse_scalar_row(row, _EPISODES, lambda _r: None, **kw)


def _prop(**over) -> TypedScalarProposal:
    base = dict(
        subject_text="user", attribute="yoga_sessions", scope="", value_kind="count", unit="",
        operation="occurrence", value=1, stated_span="went to yoga", episode_uuid="ep-1",
        span_start=8, span_end=20, when=None, importance="tracked",
    )
    base.update(over)
    return TypedScalarProposal(**base)


def _assertion(**over) -> TypedAssertion:
    base = dict(
        subject_uuid="s-1", subject_display="User", attribute="yoga_sessions", scope="",
        value_kind="count", unit="", operation="occurrence", value=1, stated_span="went to yoga",
        episode_uuid="ep-1", valid_at="2026-07-01T00:00:00+00:00",
        learned_at="2026-07-01T00:00:00+00:00", span_start=8, span_end=20, importance="tracked",
    )
    base.update(over)
    return TypedAssertion(**base)


@pytest.mark.unit
def test_parse_reads_strips_and_truncates_occurrence_fields():
    p = _parse(_occ(object="  model   kit ", item="x" * 200, action="finished\n"),
               allowed_operations=COUNTING_OPERATIONS)
    assert p.count_object == "model kit"
    assert p.count_item == "x" * 120
    assert p.count_action == "finished"


@pytest.mark.unit
def test_parse_non_string_or_missing_is_empty():
    p = _parse(_occ(object=5, item=None), allowed_operations=COUNTING_OPERATIONS)
    assert (p.count_object, p.count_item, p.count_action) == ("", "", "")


@pytest.mark.unit
def test_parse_ignores_fields_on_absolute_row():
    row = dict(
        episode=0, subject="user", attribute="yoga_sessions", scope="", value_kind="count",
        unit="", operation="absolute", value=3, stated_span="I have done yoga 3 times",
        object="kit", item="spitfire", action="finished",
    )
    episodes = [_Ep(uuid="ep-1", content="I have done yoga 3 times so far.")]
    p = parse_scalar_row(
        row, episodes, lambda _r: None,
        allowed_operations=frozenset({"absolute", "delta", "expire", "occurrence"}))
    assert p is not None  # the row must actually parse, or the assertion below is vacuous
    assert p.operation == "absolute" and p.value == 3
    assert (p.count_object, p.count_item, p.count_action) == ("", "", "")


@pytest.mark.unit
def test_gate_picks_modal_nonempty_and_does_not_split_vote():
    samples = [
        [_prop(count_item="Spitfire", count_object="kit")],
        [_prop(count_item="Hurricane")],
        [_prop(count_item="Spitfire", count_action="finished")],
    ]
    out = gate_typed_scalars(samples, threshold=2 / 3)
    assert out[0].committed
    pr = out[0].proposal
    assert pr.count_item == "Spitfire"
    assert pr.count_object == "kit"
    assert pr.count_action == "finished"


@pytest.mark.unit
def test_gate_tie_goes_to_first_appearance_and_empty_when_none():
    out = gate_typed_scalars([[_prop(count_item="B")], [_prop(count_item="A")]])
    assert out[0].proposal.count_item == "B"
    assert out[0].proposal.count_object == ""


@pytest.mark.unit
def test_assertion_rejects_count_fields_on_non_occurrence():
    for f in ("count_object", "count_item", "count_action"):
        with pytest.raises(ValueError, match="only on occurrence"):
            _assertion(operation="absolute", importance="", **{f: "x"})


@pytest.mark.unit
def test_assertion_keys_unchanged_by_count_fields():
    a = _assertion()
    b = _assertion(count_object="model kit", count_item="Spitfire", count_action="finished")
    assert (a.claim_key, a.source_key, a.assertion_key, a.slot_key) == (
        b.claim_key, b.source_key, b.assertion_key, b.slot_key)


@pytest.mark.unit
def test_assertion_from_proposal_carries_fields():
    a = _assertion_from_proposal(
        _prop(count_object="model kit", count_item="Spitfire", count_action="finished"),
        subject_uuid="s-1", subject_display="User",
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        time_basis="episode_reference", evidence_tier="agent", namespace=None,
        perceiver_version="v1")
    assert (a.count_object, a.count_item, a.count_action) == ("model kit", "Spitfire", "finished")


@pytest.mark.unit
def test_pending_row_carries_fields():
    row = dict(
        attribute="yoga_sessions", scope="", value_kind="count", unit="", operation="occurrence",
        value=1, stated_span="went to yoga", episode_uuid="ep-1",
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        span_start=8, span_end=20, claim_ordinal=0, importance="routine",
        count_object="kit", count_item=None,
    )
    a = _assertion_from_pending_row(row, subject_uuid="s-1", subject_display="User")
    assert (a.count_object, a.count_item, a.count_action) == ("kit", "", "")


@pytest.mark.unit
def test_record_params_include_fields():
    p = TypedAssertionRepairMixin._to_params(
        _assertion(count_object="kit", count_item="Spitfire", count_action="finished"))
    assert (p["count_object"], p["count_item"], p["count_action"]) == ("kit", "Spitfire", "finished")
    q = TypedAssertionRepairMixin._to_params(_assertion())
    assert (q["count_object"], q["count_item"], q["count_action"]) == ("", "", "")
    assert dataclasses.is_dataclass(TypedAssertion)
