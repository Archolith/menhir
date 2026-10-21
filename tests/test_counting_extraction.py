"""Counting-lane reuse of the scalar machinery: `occurrence` rows parse only when the caller opts in
(`allowed_operations=COUNTING_OPERATIONS`); defaults reproduce the scalar lane exactly."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.domain.typed_assertion import COUNTING_OPERATIONS
from menhir.services.typed_scalar_perception import (
    TYPED_SCALAR_SYSTEM_PROMPT,
    TypedScalarProposal,
    extract_typed_scalars_once,
    gate_typed_scalars,
)
from menhir.services.typed_scalar_persistence import _assertion_from_pending_row
from menhir.services.typed_scalar_rules import _assertion_from_proposal, parse_scalar_row


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


_EPISODES = [_Ep(
    uuid="ep-1",
    content="Today I went to yoga. Then I bought 3 plants. Also a $4.10 coffee, "
            "and I wake at 7:30 on work days.",
)]


def _llm(rows):
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps(rows)

    complete.calls = calls  # type: ignore[attr-defined]
    return complete


def _occ(**over):
    base = dict(
        episode=0, subject="user", attribute="yoga_sessions", scope="", value_kind="count",
        unit="", operation="occurrence", importance="tracked", stated_span="went to yoga",
    )
    base.update(over)
    return base


def _extract(rows, *, drops=None, **kw):
    return extract_typed_scalars_once(
        _EPISODES, _llm(rows), on_drop=(drops.append if drops is not None else None), **kw)


_COUNTING = dict(allowed_operations=COUNTING_OPERATIONS)


@pytest.mark.unit
def test_defaults_still_drop_occurrence():
    drops: list[str] = []
    assert _extract([_occ()], drops=drops) == []
    assert drops == ["bad_attribute_or_operation"]


@pytest.mark.unit
def test_parse_scalar_row_default_drops_occurrence():
    drops: list[str] = []
    assert parse_scalar_row(_occ(), _EPISODES, drops.append) is None
    assert drops == ["bad_attribute_or_operation"]


@pytest.mark.unit
def test_occurrence_without_value_is_one_and_keeps_importance_and_ordinal():
    p0, = _extract([_occ()], **_COUNTING)
    p1, = _extract([_occ()], claim_ordinal=1, **_COUNTING)
    assert p0.value == 1 and p0.unit == "" and p0.importance == "tracked"
    assert p0.claim_ordinal == 0 and p1.claim_ordinal == 1
    assert p1.importance == "tracked"
    assert p0.source_key != p1.source_key


@pytest.mark.unit
def test_importance_is_lowercased():
    p, = _extract([_occ(importance="Notable")], **_COUNTING)
    assert p.importance == "notable"


@pytest.mark.unit
def test_stated_count_in_span_is_kept():
    p, = _extract([_occ(attribute="plants_bought", stated_span="bought 3 plants", value=3)],
                  **_COUNTING)
    assert p.value == 3


@pytest.mark.unit
def test_count_not_in_span_is_dropped():
    drops: list[str] = []
    assert _extract([_occ(value=3)], drops=drops, **_COUNTING) == []
    assert drops == ["value_not_in_span"]


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, 1.5, "abc", [1, 2], True])
def test_bad_count_value_is_dropped(bad):
    drops: list[str] = []
    assert _extract([_occ(value=bad)], drops=drops, **_COUNTING) == []
    assert drops == ["value_failed_validation"]


@pytest.mark.unit
@pytest.mark.parametrize("bad", [None, "", "urgent", 3])
def test_bad_or_missing_importance_is_dropped(bad):
    row = _occ()
    if bad is None:
        del row["importance"]
    else:
        row["importance"] = bad
    drops: list[str] = []
    assert _extract([row], drops=drops, **_COUNTING) == []
    assert drops == ["bad_importance"]


@pytest.mark.unit
def test_occurrence_on_clock_time_is_dropped():
    drops: list[str] = []
    row = _occ(value_kind="clock_time", stated_span="wake at 7:30")
    assert _extract([row], drops=drops, **_COUNTING) == []
    assert drops == ["occurrence_kind_not_countable"]


@pytest.mark.unit
def test_money_occurrence():
    p, = _extract([_occ(attribute="coffee_spend", value_kind="money", value=4.10,
                        stated_span="$4.10 coffee")], **_COUNTING)
    assert str(p.value) == "4.1" and p.unit == "usd" and p.importance == "tracked"


@pytest.mark.unit
def test_money_range_is_dropped():
    drops: list[str] = []
    row = _occ(attribute="coffee_spend", value_kind="money", value=[4, 5],
               stated_span="$4.10 coffee")
    assert _extract([row], drops=drops, **_COUNTING) == []
    assert drops == ["value_failed_validation"]


@pytest.mark.unit
def test_money_without_currency_is_dropped():
    drops: list[str] = []
    row = _occ(attribute="coffee_spend", value_kind="money", value=3, stated_span="bought 3 plants")
    assert _extract([row], drops=drops, **_COUNTING) == []
    assert drops == ["money_currency_unresolved"]


@pytest.mark.unit
def test_importance_is_stripped_from_non_occurrence_rows():
    row = dict(
        episode=0, subject="user", attribute="wake_time", scope="work_days",
        value_kind="clock_time", unit="", operation="absolute", value="07:30",
        stated_span="wake at 7:30 on work days", importance="tracked",
    )
    p, = _extract([row], allowed_operations=frozenset({"absolute", "delta", "expire"}))
    assert p.importance == ""
    p2, = _extract([row])
    assert p2.importance == ""


@pytest.mark.unit
def test_system_prompt_and_context_reach_llm():
    llm = _llm([_occ()])
    extract_typed_scalars_once(
        _EPISODES, llm, system_prompt="COUNT PROMPT", context="known slots: yoga", **_COUNTING)
    system, user = llm.calls[0]
    assert system == "COUNT PROMPT"
    assert user.startswith("known slots: yoga\n\n[0] ")


@pytest.mark.unit
def test_default_prompt_and_user_message_unchanged():
    llm = _llm([])
    extract_typed_scalars_once(_EPISODES, llm)
    system, user = llm.calls[0]
    assert system == TYPED_SCALAR_SYSTEM_PROMPT
    assert user == "[0] " + _EPISODES[0].content


def _prop(importance: str, **over) -> TypedScalarProposal:
    base = dict(
        subject_text="user", attribute="yoga_sessions", scope="", value_kind="count", unit="",
        operation="occurrence", value=1, stated_span="went to yoga", episode_uuid="ep-1",
        span_start=8, span_end=20, when=None, importance=importance,
    )
    base.update(over)
    return TypedScalarProposal(**base)


@pytest.mark.unit
def test_importance_is_not_part_of_identity_or_vote():
    a, b = _prop("tracked"), _prop("routine")
    assert a.source_key == b.source_key and a.normalized_value == b.normalized_value
    out = gate_typed_scalars([[a], [b]], threshold=1.0)
    assert len(out) == 1 and out[0].committed


@pytest.mark.unit
def test_gate_picks_majority_importance():
    out = gate_typed_scalars([[_prop("routine")], [_prop("notable")], [_prop("notable")]])
    assert out[0].committed and out[0].proposal.importance == "notable"


@pytest.mark.unit
def test_gate_importance_tie_goes_to_higher_level():
    out = gate_typed_scalars([[_prop("routine")], [_prop("notable")]])
    assert out[0].proposal.importance == "notable"
    out = gate_typed_scalars([[_prop("notable")], [_prop("tracked")], [_prop("routine")]])
    assert out[0].proposal.importance == "tracked"


@pytest.mark.unit
def test_gate_leaves_non_occurrence_importance_alone():
    p = _prop("", operation="absolute", value_kind="count", value=3)
    out = gate_typed_scalars([[p], [p]])
    assert out[0].proposal.importance == ""


@pytest.mark.unit
def test_assertion_from_proposal_carries_importance():
    a = _assertion_from_proposal(
        _prop("notable"), subject_uuid="s-1", subject_display="User",
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        time_basis="episode_reference", evidence_tier="agent", namespace=None,
        perceiver_version="v1")
    assert a.importance == "notable" and a.operation == "occurrence"


@pytest.mark.unit
def test_assertion_from_pending_row_carries_importance():
    row = dict(
        attribute="yoga_sessions", scope="", value_kind="count", unit="", operation="occurrence",
        value=1, stated_span="went to yoga", episode_uuid="ep-1",
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        span_start=8, span_end=20, claim_ordinal=0, importance="routine",
    )
    a = _assertion_from_pending_row(row, subject_uuid="s-1", subject_display="User")
    assert a.importance == "routine"
    row.update(operation="absolute", importance=None)
    assert _assertion_from_pending_row(row, subject_uuid="s-1", subject_display="User").importance == ""
