"""Default-off wiring for the personal-memory event-history settings."""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings


@pytest.mark.unit
def test_event_history_fields_default_off() -> None:
    settings = MemorySettings()
    assert settings.personal_memory_event_history_enabled is False
    assert settings.personal_memory_event_history_perceiver_version == "v1"
    assert settings.personal_memory_event_history_authority_enabled is False


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)])
def test_event_history_enabled_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED", raw)
    assert MemorySettings.from_env().personal_memory_event_history_enabled is expected


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)])
def test_event_history_authority_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED", raw)
    assert MemorySettings.from_env().personal_memory_event_history_authority_enabled is expected


@pytest.mark.unit
def test_event_history_perceiver_version_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MemorySettings().personal_memory_event_history_perceiver_version == "v1"
    monkeypatch.setenv("MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_PERCEIVER_VERSION", "v3")
    assert MemorySettings.from_env().personal_memory_event_history_perceiver_version == "v3"
