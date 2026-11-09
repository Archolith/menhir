"""#519 cause 1: span alignment must re-ground a source-time prefix against the quote it commits.

Each sample's attribute was canonicalised against that sample's own quote, so a sample quoting
"current balance is 1300" keeps ``current_balance``. Alignment then commits the common span
"balance is 1300", which no longer carries the cue; keeping the prefix forks a second slot
(``current_balance`` beside ``balance``) and the older View leads alone.
"""

from __future__ import annotations

import pytest

from menhir.services.typed_scalar_perception import TypedScalarProposal, gate_typed_scalars

pytestmark = pytest.mark.unit

EPISODE = "11111111-1111-1111-1111-111111111111"
TEXT = "My current balance is 1300 now."


def _quoted(start: int, end: int, attribute: str, text: str = TEXT, value: int = 1300):
    return TypedScalarProposal(
        subject_text="user", attribute=attribute, scope="", value_kind="count", unit="",
        operation="absolute", value=value, stated_span=text[start:end], episode_uuid=EPISODE,
        span_start=start, span_end=end, when=None,
    )


def _span(text: str, quote: str) -> tuple[int, int]:
    start = text.index(quote)
    return start, start + len(quote)


def _commit(samples, **flags):
    out = [d for d in gate_typed_scalars(samples, threshold=1.0, **flags) if d.committed]
    assert len(out) == 1, out
    return out[0].proposal


def test_aligned_quote_without_the_cue_drops_the_prefix() -> None:
    with_cue = _span(TEXT, "current balance is 1300")
    without_cue = _span(TEXT, "balance is 1300 now")
    samples = [
        [_quoted(*with_cue, "current_balance")],
        [_quoted(*with_cue, "current_balance")],
        [_quoted(*without_cue, "balance")],
    ]
    committed = _commit(samples, reconcile_attribute=True, align_spans=True)
    assert committed.stated_span == "balance is 1300"
    assert committed.attribute == "balance"
    assert committed.slot_key[0] == "balance"


def test_aligned_quote_that_keeps_the_cue_keeps_the_prefix() -> None:
    """Positive control: a prefix the committed quote still states may name a distinct population
    (``previous employers``), so it stays."""
    text = "My previous balance was 900 then."
    samples = [
        [_quoted(*_span(text, "previous balance was 900"), "previous_balance", text, 900)],
        [_quoted(*_span(text, "previous balance was 900 then"), "previous_balance", text, 900)],
        [_quoted(*_span(text, "My previous balance was 900"), "previous_balance", text, 900)],
    ]
    committed = _commit(samples, reconcile_attribute=True, align_spans=True)
    assert committed.stated_span == "previous balance was 900"
    assert committed.attribute == "previous_balance"


def test_without_alignment_the_committed_name_is_unchanged() -> None:
    """Exact source_key grouping commits a sample's own quote, already canonical at parse time."""
    span = _span(TEXT, "current balance is 1300")
    samples = [[_quoted(*span, "current_balance")] for _ in range(3)]
    assert _commit(samples).attribute == "current_balance"
