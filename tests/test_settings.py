"""Unit tests for configuration settings."""

from __future__ import annotations

import os
from unittest.mock import MagicMock
import sys

# Provide mock stand-ins ONLY for dependencies genuinely missing from the
# environment. Unconditionally assigning into sys.modules here poisons the whole
# pytest session (e.g. a mocked pydantic breaks every later FastAPI route test),
# so only substitute a module that cannot actually be imported.
for _optional_mod in (
    "httpx",
    "typer",
    "fastapi",
    "pydantic",
    "openai",
    "graphiti_core",
    "neo4j",
):
    if _optional_mod in sys.modules:
        continue
    try:
        __import__(_optional_mod)
    except ImportError:
        sys.modules[_optional_mod] = MagicMock()

import pytest
from menhir.config import MemorySettings

@pytest.mark.unit
def test_neo4j_password_default_is_empty() -> None:
    """Verify that the default Neo4j password is empty for security."""
    settings = MemorySettings()
    assert settings.neo4j_password == ""

@pytest.mark.unit
def test_neo4j_password_can_be_set_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that Neo4j password can be overridden via environment variables."""
    monkeypatch.setenv("NEO4J_PASSWORD", "test-secret-123")
    settings = MemorySettings.from_env()
    assert settings.neo4j_password == "test-secret-123"


@pytest.mark.unit
def test_artifact_reconcile_repository_is_explicit_env_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MENHIR_ARTIFACT_RECONCILE_REPOSITORY", "menhir")
    assert MemorySettings.from_env().artifact_reconcile_repository == "menhir"


@pytest.mark.unit
def test_benchmark_mode_defaults_off() -> None:
    """Benchmark mode is off by default so normal runs keep background work."""
    assert MemorySettings().benchmark_mode is False


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True), ("0", False), ("false", False)])
def test_benchmark_mode_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    """MENHIR_BENCHMARK_MODE toggles the Mode-B isolation flag."""
    monkeypatch.setenv("MENHIR_BENCHMARK_MODE", raw)
    assert MemorySettings.from_env().benchmark_mode is expected


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("yes", True), ("on", False), ("0", False)])
def test_client_tokens_enabled_from_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    """Regression (SSOT-07): MENHIR_CLIENT_TOKENS_ENABLED must parse through the
    same canonical boolean set ("true"/"1"/"yes") as every other MENHIR_*_ENABLED
    flag in this module. "on" is deliberately NOT truthy here -- no flag in this
    codebase documents "on" as an accepted value, and
    api.client_token_store.client_tokens_enabled() previously disagreed with this
    exact value by accepting "on" via its own separate, now-removed parser."""
    monkeypatch.setenv("MENHIR_CLIENT_TOKENS_ENABLED", raw)
    assert MemorySettings.from_env().client_tokens_enabled is expected


@pytest.mark.unit
def test_frontier_portion_defaults() -> None:
    """Oracle ranking and the intent lens are always on (#172); the Warden gate and its
    contradiction guard are on by default (#173); the
    other frontier portions default OFF and are opt-in via MENHIR_FRONTIER_* (audit DOC-05)."""
    t = MemorySettings().retrieval_tuning()
    assert t.enable_oracle_ranking is True
    assert t.enable_intent_lens is True
    assert t.enable_assertion_shadow is False
    assert t.enable_warden_gate is True
    assert t.enable_contradiction_interrupt is True


@pytest.mark.unit
def test_frontier_portions_env_can_enable_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default-OFF portion can be turned ON via env (the opt-in toggle works)."""
    monkeypatch.setenv("MENHIR_FRONTIER_SHADOW", "true")
    t = MemorySettings.from_env().retrieval_tuning()
    assert t.enable_assertion_shadow is True


@pytest.mark.unit
def test_always_on_portions_are_on_without_settings() -> None:
    """Warden gate, contradiction interrupt, source memories, spread, said-time are always on (#168 freeze)."""
    t = MemorySettings.from_env().retrieval_tuning()
    assert t.enable_warden_gate is True
    assert t.enable_contradiction_interrupt is True
    assert t.enable_source_memories is True
    assert t.source_memory_spread is True
    assert t.enable_fact_said_time is True


@pytest.mark.unit
def test_frontier_portions_from_env_map_to_tuning(monkeypatch: pytest.MonkeyPatch) -> None:
    """MENHIR_FRONTIER_* env flags map onto RetrievalTuningConfig; shadow drives assertion_shadow."""
    # Set each explicitly so the mapping is asserted independent of field defaults.
    monkeypatch.setenv("MENHIR_FRONTIER_SHADOW", "yes")
    # Retired flags (#172) are ignored: Oracle ranking and the intent lens stay on.
    monkeypatch.setenv("MENHIR_FRONTIER_ORACLE_RANKING", "0")
    monkeypatch.setenv("MENHIR_FRONTIER_INTENT_LENS", "0")
    s = MemorySettings.from_env()
    assert s.frontier_shadow is True
    t = s.retrieval_tuning()
    assert t.enable_oracle_ranking is True
    assert t.enable_warden_gate is True
    assert t.enable_assertion_shadow is True   # frontier_shadow -> tuning.enable_assertion_shadow
    assert t.enable_intent_lens is True


@pytest.mark.unit
def test_graphiti_provider_canonical_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical GRAPHITI_LLM_PROVIDER var selects the graphiti LLM provider."""
    for var in ("GRAPHITI_LLM_PROVIDER", "MEMORY_GRAPHITI_PROVIDER", "GRAPHITI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAPHITI_LLM_PROVIDER", "openai")
    assert MemorySettings.from_env().graphiti_provider == "openai"


@pytest.mark.unit
def test_graphiti_provider_alias_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """GRAPHITI_PROVIDER (intuitive name) is honored as an alias, not silently ignored.

    Regression guard: it was previously dropped, so an explicit GRAPHITI_PROVIDER=local
    had no effect and extraction silently ran on whatever the ambient env selected.
    """
    for var in ("GRAPHITI_LLM_PROVIDER", "MEMORY_GRAPHITI_PROVIDER", "GRAPHITI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAPHITI_PROVIDER", "local")
    assert MemorySettings.from_env().graphiti_provider == "local"


@pytest.mark.unit
def test_graphiti_provider_canonical_beats_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical var wins over the GRAPHITI_PROVIDER alias when both are set."""
    for var in ("GRAPHITI_LLM_PROVIDER", "MEMORY_GRAPHITI_PROVIDER", "GRAPHITI_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GRAPHITI_LLM_PROVIDER", "openai")
    monkeypatch.setenv("GRAPHITI_PROVIDER", "local")
    assert MemorySettings.from_env().graphiti_provider == "openai"


@pytest.mark.unit
def test_parse_flex_timeout_s() -> None:
    from menhir.config.settings_model import parse_flex_timeout_s

    assert parse_flex_timeout_s(None) == 900.0
    assert parse_flex_timeout_s("") == 900.0
    assert parse_flex_timeout_s("  ") == 900.0
    assert parse_flex_timeout_s("120") == 120.0
    for bad in ("15m", "0", "-1", "nan", "inf"):
        with pytest.raises(ValueError, match="MENHIR_OPENAI_FLEX_TIMEOUT_S"):
            parse_flex_timeout_s(bad)


@pytest.mark.unit
def test_settings_reject_invalid_flex_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_OPENAI_FLEX_TIMEOUT_S", "15m")
    with pytest.raises(ValueError, match="MENHIR_OPENAI_FLEX_TIMEOUT_S"):
        MemorySettings.from_env()
