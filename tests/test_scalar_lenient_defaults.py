"""Lenient-agreement defaults for the typed-scalar gate (owner decision 2026-09).

MemorySettings defaults to threshold 2/3 with attribute/scope/subject reconciliation ON, while
`personal_memory_scalar_state_enabled` stays OFF (defaults are not activation). At k=3 the default
threshold means two agreeing samples commit and one dissenting sample does not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.services.typed_scalar_perception import (
    extract_typed_scalars_once,
    gate_typed_scalars,
)

_SCALAR_ENV_VARS = (
    "MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD",
    "MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_ATTRIBUTE",
    "MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SCOPE",
    "MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SUBJECT",
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED",
)


@dataclass(frozen=True)
class _Ep:
    uuid: str
    content: str


def _clean_scalar_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SCALAR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _llm(rows):
    def complete(system: str, user: str) -> str:
        return json.dumps(rows)
    return complete


def _row(*, value=25, attribute="postcard_count", stated_span="I have 25 postcards") -> dict:
    return {
        "episode": 0,
        "subject": "user",
        "attribute": attribute,
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": value,
        "when": "",
        "stated_span": stated_span,
    }


@pytest.mark.unit
def test_memory_settings_defaults_are_lenient() -> None:
    settings = MemorySettings()
    assert settings.personal_memory_scalar_threshold == 2 / 3
    assert settings.personal_memory_scalar_reconcile_attribute is True
    assert settings.personal_memory_scalar_reconcile_scope is True
    assert settings.personal_memory_scalar_reconcile_subject is True
    assert settings.personal_memory_scalar_state_enabled is False


@pytest.mark.unit
def test_from_env_clean_environment_round_trips_lenient_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_scalar_env(monkeypatch)
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_threshold == 2 / 3
    assert settings.personal_memory_scalar_threshold == 0.6666666666666666
    assert settings.personal_memory_scalar_reconcile_attribute is True
    assert settings.personal_memory_scalar_reconcile_scope is True
    assert settings.personal_memory_scalar_reconcile_subject is True
    assert settings.personal_memory_scalar_state_enabled is False


@pytest.mark.unit
def test_from_env_explicit_overrides_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_scalar_env(monkeypatch)
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD", "1.0")
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_ATTRIBUTE", "false")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_threshold == 1.0
    assert settings.personal_memory_scalar_reconcile_attribute is False

    _clean_scalar_env(monkeypatch)
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_THRESHOLD", "0.67")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_threshold == 0.67

    _clean_scalar_env(monkeypatch)
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SCOPE", "false")
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_SCALAR_RECONCILE_SUBJECT", "false")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_scalar_reconcile_scope is False
    assert settings.personal_memory_scalar_reconcile_subject is False


@pytest.mark.unit
def test_default_threshold_commits_two_of_three_and_not_one_dissent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_scalar_env(monkeypatch)
    settings = MemorySettings.from_env()
    ep = [_Ep(uuid="lenient-1", content="I have 25 postcards")]

    def samples_with(rows_per_sample: list[list[dict]]):
        return [
            extract_typed_scalars_once(ep, _llm(rows))
            for rows in rows_per_sample
        ]

    gate = lambda samples: gate_typed_scalars(  # noqa: E731
        samples,
        threshold=settings.personal_memory_scalar_threshold,
        reconcile_attribute=settings.personal_memory_scalar_reconcile_attribute,
        reconcile_scope=settings.personal_memory_scalar_reconcile_scope,
        reconcile_subject=settings.personal_memory_scalar_reconcile_subject,
    )

    # Two samples agree; the third misses the claim entirely (absent vote). 2/3 >= 2/3 commits.
    agreed = samples_with([[_row()], [_row()], []])
    decisions = gate(agreed)
    assert len(decisions) == 1
    assert decisions[0].committed is True
    assert decisions[0].agreement == 2 / 3

    # Attribute-name disagreement still reconciles to one claim and commits 3/3.
    smeared = samples_with(
        [[_row(attribute="collection_size")], [_row(attribute="count")], [_row()]])
    decisions = gate(smeared)
    assert len(decisions) == 1
    assert decisions[0].committed is True

    # Only one of three perceives the claim: 1/3 < 2/3, no commit.
    dissent = samples_with([[_row()], [], []])
    decisions = gate(dissent)
    assert len(decisions) == 1
    assert decisions[0].committed is False
