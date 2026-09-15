"""Scheduler-bound settings refuse sub-minimum values at startup (#81).

The Tier 1 contract is a named refusal, not a silent clamp: a negative
interval or a zero consolidation K must raise a ValueError that names the
offending env var, so the process never starts with a scheduler config that
busy-loops or no-ops.
"""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings

pytestmark = pytest.mark.unit


def test_negative_structure_watcher_interval_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_STRUCTURE_WATCHER_INTERVAL_S", "-30")
    with pytest.raises(ValueError, match="MENHIR_STRUCTURE_WATCHER_INTERVAL_S"):
        MemorySettings.from_env()


def test_zero_consolidation_k_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K", "0")
    with pytest.raises(ValueError, match="MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K"):
        MemorySettings.from_env()


def test_exact_minimums_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_STRUCTURE_WATCHER_INTERVAL_S", "1")
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_CONSOLIDATION_K", "1")
    settings = MemorySettings.from_env()
    assert settings.structure_watcher_interval_s == 1.0
    assert settings.personal_memory_consolidation_k == 1
