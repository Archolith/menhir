"""Regression coverage for grounded frequency identity normalization.

Interval frequency values are source-derived before the k-sample vote.  This prevents model
float precision from splitting one grounded claim into multiple interpretations while preserving
full precision for unrelated numeric scalar kinds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    extract_typed_scalars_once,
    gate_typed_scalars,
)


@dataclass(frozen=True)
class _Episode:
    uuid: str
    content: str


def _llm(value: float):
    row = {
        "episode": 0,
        "subject": "user",
        "attribute": "run_frequency",
        "scope": "",
        "value_kind": "frequency",
        "unit": "week",
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": "I run every three days",
    }

    def complete(_system: str, _user: str) -> str:
        return json.dumps([row])

    return complete


@pytest.mark.unit
def test_grounded_interval_frequency_ignores_model_float_precision_before_vote() -> None:
    """The grounded interval, not the model's decimal rendering, owns frequency identity."""
    episode = _Episode(uuid="ep-frequency", content="I run every three days.")

    samples = [
        extract_typed_scalars_once([episode], _llm(1.0)),
        extract_typed_scalars_once([episode], _llm(0.333333)),
        extract_typed_scalars_once([episode], _llm(999.0)),
    ]

    proposals = [sample[0] for sample in samples]
    assert {proposal.source_key for proposal in proposals} == {proposals[0].source_key}
    assert {proposal.unit for proposal in proposals} == {"day"}
    assert {proposal.value for proposal in proposals} == {1 / 3}
    assert {proposal.normalized_value for proposal in proposals} == {"0.3333333333333333"}

    decisions = gate_typed_scalars(samples)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 1.0
    assert decisions[0].proposal is not None
    assert decisions[0].proposal.value == 1 / 3


@pytest.mark.unit
def test_frequency_regression_does_not_round_other_numeric_scalar_kinds() -> None:
    """Do not solve frequency identity by globally quantizing measurement precision."""
    common = dict(
        subject_text="user",
        attribute="ratio",
        scope="",
        value_kind="measurement",
        unit="",
        operation="absolute",
        stated_span="ratio is one third",
        episode_uuid="ep-ratio",
        span_start=0,
        span_end=18,
        when=None,
    )
    rounded = TypedScalarProposal(value=0.333333, **common)
    precise = TypedScalarProposal(value=1 / 3, **common)

    assert rounded.source_key == precise.source_key
    assert rounded.normalized_value != precise.normalized_value

    decisions = gate_typed_scalars([[rounded], [precise]])
    assert len(decisions) == 1
    assert decisions[0].committed is False
