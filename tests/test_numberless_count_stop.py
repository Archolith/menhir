"""A count stop with no number ("I don't have cats anymore") states a current count of 0 (#170 item 5).

Before this, the row had no source number, so it was dropped as `count_value_unresolved` and the old
count stayed current. Any number the model supplies is ignored: it is not in the source.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_rules import parse_scalar_row

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str
    reference_time: object = None


def _parse(span: str, *, operation: str = "expire", value: object = 3, content: str | None = None,
           **kw):
    drops: list[str] = []
    row = dict(episode=0, subject="user", attribute="cats", scope="", value_kind="count", unit="",
               operation=operation, value=value, stated_span=span)
    proposal = parse_scalar_row(row, [_Ep("ep-1", content or span)], drops.append, **kw)
    return proposal, drops


@pytest.mark.parametrize("span", [
    "I don't have cats anymore.",
    "I no longer have any cats.",
    "I gave up keeping cats.",
])
@pytest.mark.parametrize("operation,value", [("expire", 3), ("expire", None), ("absolute", 2)])
def test_numberless_stop_is_a_count_of_zero(span, operation, value) -> None:
    proposal, drops = _parse(span, operation=operation, value=value)
    assert drops == []
    assert proposal is not None
    assert (proposal.operation, proposal.value, proposal.unit) == ("absolute", 0, "")


def test_every_sample_agrees_whatever_number_the_model_guessed() -> None:
    labels = {(p.operation, p.value) for p, _ in
              (_parse("I don't have cats anymore.", value=v) for v in (1, 3, 7, None))}
    assert labels == {("absolute", 0)}


@pytest.mark.parametrize("span", [
    "I don't have as many cats anymore.",
    "I no longer have so many cats, only a few.",
    "I don't have more cats anymore.",
])
def test_a_partial_stop_is_not_zero(span) -> None:
    proposal, drops = _parse(span)
    assert proposal is None
    assert drops == ["count_value_unresolved"]


def test_a_stated_number_keeps_the_ordinary_expire() -> None:
    proposal, drops = _parse("I no longer have my 3 cats.", value=9)
    assert drops == []
    assert proposal is not None
    assert (proposal.operation, proposal.value) == ("expire", 3)


def test_a_numberless_current_claim_is_still_dropped() -> None:
    proposal, drops = _parse("I have cats.", operation="absolute", value=2)
    assert proposal is None
    assert drops == ["count_value_unresolved"]


def test_no_conversion_when_absolute_is_not_allowed() -> None:
    proposal, drops = _parse("I don't have cats anymore.",
                             allowed_operations=frozenset({"expire", "delta"}))
    assert proposal is None
    assert drops == ["count_value_unresolved"]


def test_a_restored_state_is_not_a_stop() -> None:
    proposal, _ = _parse("I didn't have cats anymore, but I have them again.")
    assert proposal is None or proposal.value != 0


def test_a_numberless_delta_is_unchanged() -> None:
    proposal, _ = _parse("I don't have cats anymore.", operation="delta", value=-3)
    assert proposal is None or (proposal.operation, proposal.value) != ("absolute", 0)
