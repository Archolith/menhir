"""Menhir's local state must not be located by guessing at the install layout.

A pip, pipx, or container install has no meaningful ancestor directory, so the only
inputs are explicit environment variables and the user's home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.infrastructure import paths

_ENV = ("MENHIR_STATE_DIR", "WORKSPACE_ROOT", "MENHIR_MCP_TELEMETRY_DB", "MENHIR_OAUTH_AS_DIR")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return tmp_path


@pytest.mark.unit
def test_default_state_dir_is_under_home(clean_env: Path) -> None:
    assert paths.state_dir() == clean_env / "home" / ".menhir"
    assert paths.telemetry_db_path() == clean_env / "home" / ".menhir" / "mcp_telemetry.db"
    assert paths.oauth_as_db_path() == clean_env / "home" / ".menhir"


@pytest.mark.unit
def test_menhir_state_dir_wins(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_STATE_DIR", str(clean_env / "state"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(clean_env / "ws"))
    assert paths.state_dir() == clean_env / "state"
    assert paths.telemetry_db_path() == clean_env / "state" / "mcp_telemetry.db"
    assert paths.oauth_as_db_path() == clean_env / "state"


@pytest.mark.unit
def test_legacy_workspace_root_maps_to_dot_agent(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", str(clean_env / "ws"))
    assert paths.state_dir() == clean_env / "ws" / ".agent"
    assert paths.telemetry_db_path() == clean_env / "ws" / ".agent" / "mcp_telemetry.db"
    assert paths.projects_dir() == clean_env / "ws" / "projects"


@pytest.mark.unit
def test_per_file_overrides_beat_the_state_dir(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MENHIR_STATE_DIR", str(clean_env / "state"))
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(clean_env / "elsewhere" / "t.db"))
    monkeypatch.setenv("MENHIR_OAUTH_AS_DIR", str(clean_env / "oauth"))
    assert paths.telemetry_db_path() == clean_env / "elsewhere" / "t.db"
    assert paths.oauth_as_db_path() == clean_env / "oauth"
    assert paths.oauth_as_db_path(str(clean_env / "explicit")) == clean_env / "explicit"


@pytest.mark.unit
def test_no_workspace_means_no_repo_resolution(clean_env: Path) -> None:
    assert paths.workspace_root() is None
    assert paths.projects_dir() is None
    assert paths.repo_root_for_project("anything") is None


@pytest.mark.unit
def test_state_dir_never_derives_from_the_package_location(clean_env: Path) -> None:
    package_root = Path(paths.__file__).resolve().parents[3]
    assert not str(paths.state_dir()).startswith(str(package_root))


@pytest.mark.unit
def test_connect_telemetry_db_creates_the_state_directory(clean_env: Path) -> None:
    from menhir.infrastructure.telemetry.helpers import connect_telemetry_db

    target = clean_env / "fresh" / "nested" / "mcp_telemetry.db"
    assert not target.parent.exists()
    conn = connect_telemetry_db(target)
    try:
        assert target.parent.is_dir()
    finally:
        conn.close()


@pytest.mark.unit
def test_default_workspace_marker_follows_workspace_root(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert paths.default_workspace_marker() is None
    monkeypatch.setenv("WORKSPACE_ROOT", str(clean_env / "IdeaProjects"))
    assert paths.default_workspace_marker() == "/IdeaProjects/"

