"""Release configuration contract across checkout setup, wheel setup and upgrade reruns."""
from __future__ import annotations

from pathlib import Path

import pytest

from menhir import env_file
from menhir.cli.setup import apply_setup
from menhir.config import MemorySettings

pytestmark = pytest.mark.unit

# Bounded #168 campaign switches, not the full proposed feature registry.
CAMPAIGN_FLAGS = {
    "MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED": "personal_memory_scalar_state_enabled",
    "MENHIR_PERSONAL_MEMORY_SCALAR_VIEW_AUTHORITY_ENABLED": "personal_memory_scalar_view_authority_enabled",
    "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "personal_memory_scalar_history_enabled",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "personal_memory_event_history_enabled",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED": "personal_memory_event_history_authority_enabled",
    "MENHIR_FRONTIER_BM25": "frontier_bm25",
    "MENHIR_FRONTIER_CONTENT_VECTOR": "frontier_content_vector",
    "MENHIR_FRONTIER_ORACLE_RANKING": "frontier_oracle_ranking",
    "MENHIR_FRONTIER_INTENT_LENS": "frontier_intent_lens",
    "MENHIR_FRONTIER_WARDEN_GATE": "frontier_warden_gate",
    "MENHIR_FRONTIER_DIVERSITY_GATE": "frontier_diversity_gate",
    "MENHIR_FRONTIER_CONTRADICTION_INTERRUPT": "frontier_contradiction_interrupt",
    "MENHIR_FRONTIER_BELIEF_GATE": "frontier_belief_gate",
    "MENHIR_FRONTIER_EVIDENCE_ANCHOR": "frontier_evidence_anchor",
    "MENHIR_FRONTIER_FACT_EDGES": "frontier_fact_edges",
    "MENHIR_FRONTIER_SHADOW": "frontier_shadow",
    "MENHIR_FRONTIER_SOURCE_MEMORIES": "frontier_source_memories",
    "MENHIR_FRONTIER_SOURCE_MEMORY_POOLS": "frontier_source_memory_pools",
    "MENHIR_VERIFIER_SYNC_ENABLED": "verifier_sync_enabled",
    "MENHIR_SCALAR_DETERMINISTIC_SHADOW": "personal_memory_scalar_deterministic_shadow",
}


@pytest.fixture
def clean_campaign_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in CAMPAIGN_FLAGS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("MENHIR_CANONICAL_SELF_BINDING_MODE", raising=False)
    monkeypatch.delenv("ENV_FILE", raising=False)


def _home(tmp_path: Path, checkout: bool) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    if checkout:
        (home / "pyproject.toml").write_text('[project]\nname="archolith-menhir"\n')
        (home / ".env.example").write_bytes((Path(__file__).parents[1] / ".env.example").read_bytes())
    return home


@pytest.mark.parametrize("checkout", [False, True], ids=["wheel-state", "checkout"])
def test_fresh_setup_uses_code_defaults_without_enabling_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None, checkout: bool,
) -> None:
    home = _home(tmp_path, checkout)
    apply_setup(home, provider="openai", configure_git_hooks=False)
    monkeypatch.setenv("ENV_FILE", str(home / ".env"))
    env_file.load_menhir_env()
    settings = MemorySettings.from_env()
    for key, attr in CAMPAIGN_FLAGS.items():
        # Source memories already shipped on in #208; this audit does not promote them.
        assert getattr(settings, attr) is (key == "MENHIR_FRONTIER_SOURCE_MEMORIES")
    assert settings.canonical_self_binding_mode == "off"


@pytest.mark.parametrize("checkout", [False, True], ids=["wheel-state", "checkout"])
def test_upgrade_with_unset_flags_uses_current_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None, checkout: bool,
) -> None:
    home = _home(tmp_path, checkout)
    original = "# existing configuration without campaign settings\n"
    (home / ".env").write_text(original)
    assert apply_setup(home, configure_git_hooks=False) == []
    assert (home / ".env").read_text() == original
    monkeypatch.setenv("ENV_FILE", str(home / ".env"))
    env_file.load_menhir_env()
    settings = MemorySettings.from_env()
    for key, attr in CAMPAIGN_FLAGS.items():
        assert getattr(settings, attr) is (key == "MENHIR_FRONTIER_SOURCE_MEMORIES")


@pytest.mark.parametrize("checkout", [False, True], ids=["wheel-state", "checkout"])
def test_upgrade_keeps_explicit_opt_outs_despite_changed_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None, checkout: bool,
) -> None:
    home = _home(tmp_path, checkout)
    original = "".join(f"{key}=false\n" for key in CAMPAIGN_FLAGS)
    original += "MENHIR_CANONICAL_SELF_BINDING_MODE=off\n# operator configuration\n"
    (home / ".env").write_text(original)
    if checkout:
        (home / ".env.example").write_text("".join(f"{key}=true\n" for key in CAMPAIGN_FLAGS))
    apply_setup(home, provider="openai", configure_git_hooks=False)
    assert (home / ".env").read_text().startswith(original)
    before = (home / ".env").read_bytes()
    assert apply_setup(home, provider="openai", configure_git_hooks=False) == []
    assert (home / ".env").read_bytes() == before
    monkeypatch.setenv("ENV_FILE", str(home / ".env"))
    env_file.load_menhir_env()
    settings = MemorySettings.from_env()
    assert all(getattr(settings, attr) is False for attr in CAMPAIGN_FLAGS.values())


@pytest.mark.parametrize("raw,expected", [("false", False), ("0", False), ("no", False),
    ("", False), ("on", False), ("typo", False), ("TRUE", True), ("1", True), ("yes", True)])
def test_process_environment_wins_over_file_for_campaign_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None, raw: str, expected: bool,
) -> None:
    path = tmp_path / "selected.env"
    path.write_text("".join(f"{key}={'false' if expected else 'true'}\n" for key in CAMPAIGN_FLAGS))
    monkeypatch.setenv("ENV_FILE", str(path))
    for key in CAMPAIGN_FLAGS:
        monkeypatch.setenv(key, raw)
    env_file.load_menhir_env()
    settings = MemorySettings.from_env()
    assert all(getattr(settings, attr) is expected for attr in CAMPAIGN_FLAGS.values())


def test_documented_campaign_names_are_present_but_commented() -> None:
    from dotenv import dotenv_values

    template = Path(__file__).parents[1] / ".env.example"
    text = template.read_text()
    active = dotenv_values(template)
    for key in CAMPAIGN_FLAGS:
        assert f"# {key}=" in text
        assert key not in active


def test_short_event_names_are_not_supported_aliases(
    monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None,
) -> None:
    monkeypatch.setenv("MENHIR_EVENT_HISTORY_ENABLED", "true")
    monkeypatch.setenv("MENHIR_EVENT_HISTORY_AUTHORITY_ENABLED", "true")
    settings = MemorySettings.from_env()
    assert settings.personal_memory_event_history_enabled is False
    assert settings.personal_memory_event_history_authority_enabled is False
