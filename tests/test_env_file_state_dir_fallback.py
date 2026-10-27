"""resolve_env_file() falls back to the state-dir .env that `menhir setup` writes.

Bug: on a wheel install `menhir setup` wrote config (including opt-outs such as
MENHIR_FRONTIER_BELIEF_GATE=false) to state_dir()/.env, but `menhir serve` from another cwd
without ENV_FILE ignored it and ran on code defaults.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from menhir import env_file

pytestmark = [pytest.mark.unit]

GATE = "MENHIR_FRONTIER_BELIEF_GATE"


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Empty cwd, fake home, no ENV_FILE / MENHIR_STATE_DIR / WORKSPACE_ROOT."""

    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in ("ENV_FILE", "MENHIR_STATE_DIR", "WORKSPACE_ROOT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(GATE, raising=False)
    return {"cwd": cwd, "home": home, "root": tmp_path}


def test_wheel_style_state_dir_env_is_found_and_loaded_from_other_cwd(
    monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]
) -> None:
    state = isolated["root"] / "state"
    state.mkdir()
    (state / ".env").write_text(f"{GATE}=false\n", encoding="utf-8")
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))

    assert env_file.resolve_env_file() == state / ".env"
    assert env_file.load_menhir_env() == state / ".env"
    assert os.environ.get(GATE) == "false"
    monkeypatch.delenv(GATE, raising=False)  # restore after dotenv wrote it directly


def test_cwd_env_beats_state_dir_env(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]) -> None:
    state = isolated["root"] / "state"
    state.mkdir()
    (state / ".env").write_text("A=state\n", encoding="utf-8")
    (isolated["cwd"] / ".env").write_text("A=cwd\n", encoding="utf-8")
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))

    assert env_file.resolve_env_file() == isolated["cwd"] / ".env"


def test_ENV_FILE_beats_cwd_and_state_dir(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]) -> None:
    state = isolated["root"] / "state"
    state.mkdir()
    (state / ".env").write_text("A=state\n", encoding="utf-8")
    (isolated["cwd"] / ".env").write_text("A=cwd\n", encoding="utf-8")
    explicit = isolated["root"] / "explicit.env"
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))
    monkeypatch.setenv("ENV_FILE", str(explicit))  # need not exist

    assert env_file.resolve_env_file() == explicit


def test_state_dir_without_env_file_resolves_none(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]) -> None:
    state = isolated["root"] / "state"
    state.mkdir()
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))

    assert env_file.resolve_env_file() is None
    assert env_file.load_menhir_env() is None


def test_directory_named_dotenv_in_state_dir_is_not_a_file(
    monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]
) -> None:
    state = isolated["root"] / "state"
    (state / ".env").mkdir(parents=True)
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))

    assert env_file.resolve_env_file() is None


def test_default_state_dir_under_home_is_used(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]) -> None:
    default_state = isolated["home"] / ".menhir"
    default_state.mkdir()
    (default_state / ".env").write_text("A=home\n", encoding="utf-8")

    assert env_file.resolve_env_file() == default_state / ".env"


def test_workspace_root_agent_dir_env_is_used(monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]) -> None:
    ws = isolated["root"] / "ws"
    (ws / ".agent").mkdir(parents=True)
    (ws / ".agent" / ".env").write_text("A=ws\n", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws))

    assert env_file.resolve_env_file() == ws / ".agent" / ".env"


def test_load_menhir_env_does_not_override_existing_process_env(
    monkeypatch: pytest.MonkeyPatch, isolated: dict[str, Path]
) -> None:
    state = isolated["root"] / "state"
    state.mkdir()
    (state / ".env").write_text(f"{GATE}=false\n", encoding="utf-8")
    monkeypatch.setenv("MENHIR_STATE_DIR", str(state))
    monkeypatch.setenv(GATE, "true")

    assert env_file.load_menhir_env() == state / ".env"
    assert os.environ[GATE] == "true"
