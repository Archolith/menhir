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
def test_default_root_is_cwd_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(INGEST_ALLOWED_ROOTS_ENV, raising=False)
    workdir = tmp_path / "work"
    (workdir / "sub").mkdir(parents=True)
    monkeypatch.chdir(workdir)
    inside = workdir / "sub" / "note.md"
    inside.write_text("n", encoding="utf-8")
    assert ensure_ingest_path_allowed(str(inside), tier="agent") == inside.resolve()

    outside = tmp_path / "other.md"
    outside.write_text("o", encoding="utf-8")
    with pytest.raises(IngestPathNotAllowedError):
        ensure_ingest_path_allowed(str(outside), tier="agent")


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
