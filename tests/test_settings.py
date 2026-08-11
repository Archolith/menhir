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
def test_frontier_portions_default_all_off_recall_is_neutral() -> None:
    """The shipped MemorySettings default has EVERY frontier portion OFF, so the recall path is
    byte-for-byte today's ScoringService behavior (merge-to-main neutrality gate + the read-side
    bench verdict; also closes audit DOC-05). The stack is opt-in via MENHIR_FRONTIER_*."""
    t = MemorySettings().retrieval_tuning()
    assert t.enable_oracle_ranking is False
    assert t.enable_intent_lens is False
    assert t.enable_assertion_shadow is False
    assert t.enable_warden_gate is False
    assert t.enable_belief_gate is False
    assert t.enable_evidence_anchor is False
    assert t.enable_bm25 is False
    assert t.enable_content_vector is False


@pytest.mark.unit
def test_frontier_portions_env_can_enable_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default-OFF portion can be turned ON via env (the opt-in toggle works)."""
    monkeypatch.setenv("MENHIR_FRONTIER_ORACLE_RANKING", "1")
    monkeypatch.setenv("MENHIR_FRONTIER_SHADOW", "true")
    t = MemorySettings.from_env().retrieval_tuning()
    assert t.enable_oracle_ranking is True
    assert t.enable_assertion_shadow is True
    assert t.enable_intent_lens is False   # untouched default stays off


@pytest.mark.unit
def test_content_vector_env_maps_all_tuning_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_FRONTIER_CONTENT_VECTOR", "true")
    monkeypatch.setenv("MENHIR_FRONTIER_CONTENT_VECTOR_K", "17")
    monkeypatch.setenv("MENHIR_FRONTIER_CONTENT_VECTOR_WEIGHT", "0.75")
    monkeypatch.setenv("MENHIR_FRONTIER_CONTENT_VECTOR_REPLACE_NAME", "true")
    monkeypatch.setenv("MENHIR_FRONTIER_FUSION_ADMISSION_POLICY", "production_fused")
    tuning = MemorySettings.from_env().retrieval_tuning()
    assert tuning.enable_content_vector is True
    assert tuning.content_vector_k == 17
    assert tuning.content_vector_weight == 0.75
    assert tuning.content_vector_replace_name is True
    assert tuning.fusion_admission_policy == "production_fused"


@pytest.mark.unit
def test_frontier_portions_from_env_map_to_tuning(monkeypatch: pytest.MonkeyPatch) -> None:
    """MENHIR_FRONTIER_* env flags map onto RetrievalTuningConfig; shadow drives assertion_shadow."""
    # Set all five explicitly so the mapping is asserted independent of field defaults.
    monkeypatch.setenv("MENHIR_FRONTIER_ORACLE_RANKING", "1")
    monkeypatch.setenv("MENHIR_FRONTIER_WARDEN_GATE", "true")
    monkeypatch.setenv("MENHIR_FRONTIER_BELIEF_GATE", "1")
    monkeypatch.setenv("MENHIR_FRONTIER_SHADOW", "yes")
    monkeypatch.setenv("MENHIR_FRONTIER_INTENT_LENS", "0")
    monkeypatch.setenv("MENHIR_FRONTIER_BM25", "false")
    s = MemorySettings.from_env()
    assert (s.frontier_oracle_ranking, s.frontier_warden_gate, s.frontier_belief_gate, s.frontier_shadow) == (True, True, True, True)
    assert (s.frontier_intent_lens, s.frontier_bm25) == (False, False)
    t = s.retrieval_tuning()
    assert t.enable_oracle_ranking is True
    assert t.enable_warden_gate is True
    assert t.enable_belief_gate is True
    assert t.enable_assertion_shadow is True   # frontier_shadow -> tuning.enable_assertion_shadow
    assert t.enable_intent_lens is False
    assert t.enable_bm25 is False


@pytest.mark.unit
def test_shadow_context_composition_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage 1 shadow-mode context composition is off by default (research
    instrumentation, not a recall/extraction behavior change)."""
    monkeypatch.delenv("MENHIR_SHADOW_CONTEXT_COMPOSITION", raising=False)
    monkeypatch.delenv("MENHIR_SHADOW_COMPOSITION_TIMEOUT_S", raising=False)
    s = MemorySettings.from_env()
    assert s.shadow_context_composition is False
    assert s.shadow_composition_timeout_s == 30.0


@pytest.mark.unit
def test_shadow_context_composition_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_SHADOW_CONTEXT_COMPOSITION", "true")
    monkeypatch.setenv("MENHIR_SHADOW_COMPOSITION_TIMEOUT_S", "12.5")
    s = MemorySettings.from_env()
    assert s.shadow_context_composition is True
    assert s.shadow_composition_timeout_s == 12.5


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
