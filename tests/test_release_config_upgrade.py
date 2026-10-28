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
    "MENHIR_PERSONAL_MEMORY_SCALAR_HISTORY_ENABLED": "personal_memory_scalar_history_enabled",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_ENABLED": "personal_memory_event_history_enabled",
    "MENHIR_PERSONAL_MEMORY_EVENT_HISTORY_AUTHORITY_ENABLED": "personal_memory_event_history_authority_enabled",
    "MENHIR_FRONTIER_BM25": "frontier_bm25",
    "MENHIR_FRONTIER_CONTENT_VECTOR": "frontier_content_vector",
    "MENHIR_FRONTIER_DIVERSITY_GATE": "frontier_diversity_gate",
    "MENHIR_FRONTIER_BELIEF_GATE": "frontier_belief_gate",
    "MENHIR_FRONTIER_EVIDENCE_ANCHOR": "frontier_evidence_anchor",
    "MENHIR_FRONTIER_SHADOW": "frontier_shadow",
    "MENHIR_FRONTIER_SOURCE_MEMORY_POOLS": "frontier_source_memory_pools",
    "MENHIR_VERIFIER_SYNC_ENABLED": "verifier_sync_enabled",
    "MENHIR_SCALAR_DETERMINISTIC_SHADOW": "personal_memory_scalar_deterministic_shadow",
}


# Campaign switches that ship on by default (the scalar lane is a cost switch kept on by the #168
# freeze). The Warden gate, source memories, scalar view authority and the others retired by the
# freeze have no switch.
DEFAULT_ON: set[str] = {"MENHIR_PERSONAL_MEMORY_SCALAR_STATE_ENABLED"}


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
        assert getattr(settings, attr) is (key in DEFAULT_ON), key
    assert settings.canonical_self_binding_mode == "enforce"  # default since #169


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
        assert getattr(settings, attr) is (key in DEFAULT_ON), key


@pytest.mark.parametrize("pinned,expected", [(None, "enforce"), ("off", "off"),
                                              ("observe", "observe")])
def test_upgrade_canonical_self_mode_follows_default_unless_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None,
    pinned: str | None, expected: str,
) -> None:
    """Upgrades without the key get enforce (#169); an explicit value is kept as the opt-out."""
    home = _home(tmp_path, checkout=False)
    original = "# existing configuration\n"
    if pinned is not None:
        original += f"MENHIR_CANONICAL_SELF_BINDING_MODE={pinned}\n"
    (home / ".env").write_text(original)
    apply_setup(home, provider="openai", configure_git_hooks=False)
    assert (home / ".env").read_text().startswith(original)
    monkeypatch.setenv("ENV_FILE", str(home / ".env"))
    env_file.load_menhir_env()
    assert MemorySettings.from_env().canonical_self_binding_mode == expected


@pytest.mark.parametrize("pinned,expected", [(None, "enforce"), ("off", "off"),
                                              ("observe", "observe")])
def test_upgrade_inferred_self_mode_follows_default_unless_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_campaign_env: None,
    pinned: str | None, expected: str,
) -> None:
    """Upgrades without the key get enforce (#343); an explicit value is kept as the opt-out."""
    monkeypatch.delenv("MENHIR_INFERRED_SELF_MODE", raising=False)
    home = _home(tmp_path, checkout=False)
    original = "# existing configuration\n"
    if pinned is not None:
        original += f"MENHIR_INFERRED_SELF_MODE={pinned}\n"
    (home / ".env").write_text(original)
    apply_setup(home, provider="openai", configure_git_hooks=False)
    assert (home / ".env").read_text().startswith(original)
    monkeypatch.setenv("ENV_FILE", str(home / ".env"))
    env_file.load_menhir_env()
    assert MemorySettings.from_env().inferred_self_mode == expected


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
