"""Tests for ingest path containment (SEC-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.core.ingest_guard import (
    INGEST_ALLOWED_ROOTS_ENV,
    IngestPathNotAllowedError,
    ensure_ingest_path_allowed,
)


@pytest.mark.unit
def test_operator_tier_bypasses_containment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path / "allowed"))
    outside = tmp_path / "outside" / "secret.txt"
    resolved = ensure_ingest_path_allowed(str(outside), tier="operator")
    assert resolved == outside.resolve()


@pytest.mark.unit
def test_empty_tier_is_unrestricted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No API keys configured (local dev): tier is None and enforcement is off.
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path / "allowed"))
    outside = tmp_path / "outside" / "secret.txt"
    assert ensure_ingest_path_allowed(str(outside), tier=None) == outside.resolve()
    assert ensure_ingest_path_allowed(str(outside), tier="") == outside.resolve()


@pytest.mark.unit
def test_agent_path_inside_allowed_root_is_permitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    (root / "docs").mkdir(parents=True)
    target = root / "docs" / "readme.md"
    target.write_text("hi", encoding="utf-8")
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(root))
    assert ensure_ingest_path_allowed(str(target), tier="agent") == target.resolve()


@pytest.mark.unit
def test_agent_path_outside_allowed_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(root))
    with pytest.raises(IngestPathNotAllowedError):
        ensure_ingest_path_allowed(str(outside), tier="agent")


@pytest.mark.unit
def test_readonly_tier_is_also_confined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(root))
    with pytest.raises(IngestPathNotAllowedError):
        ensure_ingest_path_allowed(str(outside), tier="readonly")


@pytest.mark.unit
def test_default_config_refuses_agent_ingest_and_names_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #83: there is no default root. With MENHIR_INGEST_ALLOWED_ROOTS unset, a
    # non-operator ingest is refused with the setup message instead of silently
    # allowing the server's working directory.
    monkeypatch.delenv(INGEST_ALLOWED_ROOTS_ENV, raising=False)
    workdir = tmp_path / "work"
    (workdir / "sub").mkdir(parents=True)
    monkeypatch.chdir(workdir)
    inside = workdir / "sub" / "note.md"
    inside.write_text("n", encoding="utf-8")
    with pytest.raises(IngestPathNotAllowedError, match=INGEST_ALLOWED_ROOTS_ENV):
        ensure_ingest_path_allowed(str(inside), tier="agent")


@pytest.mark.unit
def test_dotfile_is_refused_for_every_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #83 second layer: .env-style dotfiles are denied by name even for the
    # operator tier and even when the path sits inside an allowed root.
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path))
    env_file = tmp_path / ".env"
    for tier in ("agent", "readonly", "operator", None, ""):
        with pytest.raises(IngestPathNotAllowedError, match="denied path pattern"):
            ensure_ingest_path_allowed(str(env_file), tier=tier)


@pytest.mark.unit
def test_logs_and_backups_directories_are_denied_for_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path))
    for rel in ("logs/run.txt", "backups/db.dump"):
        with pytest.raises(IngestPathNotAllowedError, match="denied path pattern"):
            ensure_ingest_path_allowed(str(tmp_path / rel), tier="operator")


@pytest.mark.unit
@pytest.mark.parametrize("tier", ["agent", "readonly", "operator", None, ""])
@pytest.mark.parametrize(
    "rel",
    ("logs/2026/sept/run.txt", "backups/old/db.dump", ".git/objects/pack/file"),
)
def test_nested_denied_directories_are_refused_for_every_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str | None, rel: str
) -> None:
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path))
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic", encoding="utf-8")
    with pytest.raises(IngestPathNotAllowedError, match="denied path pattern"):
        ensure_ingest_path_allowed(str(target), tier=tier)


@pytest.mark.unit
@pytest.mark.parametrize("rel", ("logs/2026", "backups/snapshots", ".git/objects"))
def test_configured_root_cannot_exempt_its_denied_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    root = tmp_path / rel
    root.mkdir(parents=True)
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(root))
    for path in (root, root / "note.txt"):
        with pytest.raises(IngestPathNotAllowedError, match="denied path pattern"):
            ensure_ingest_path_allowed(str(path), tier="agent")


@pytest.mark.unit
def test_symlink_into_nested_logs_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "logs" / "2026" / "run.txt"
    target.parent.mkdir(parents=True)
    target.write_text("synthetic", encoding="utf-8")
    link = tmp_path / "shortcut.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(tmp_path))
    for tier in ("agent", "operator", None):
        with pytest.raises(IngestPathNotAllowedError, match="denied path pattern"):
            ensure_ingest_path_allowed(str(link), tier=tier)


@pytest.mark.unit
def test_symlink_escape_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported in this environment")
    monkeypatch.setenv(INGEST_ALLOWED_ROOTS_ENV, str(root))
    # The link lives under the allowed root but resolves outside it — must be rejected.
    with pytest.raises(IngestPathNotAllowedError):
        ensure_ingest_path_allowed(str(link), tier="agent")
