"""Git repository identity and blob access for release specifications."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import menhir_schema

from release_spec_errors import ReleaseSpecError
from release_spec_io import _directory


def _git_run(repo: Path, *args: str, text: bool = True) -> str | bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=text,
        )
    except subprocess.CalledProcessError as exc:
        raise ReleaseSpecError(
            f"git validation failed for {repo}: {' '.join(args)}"
        ) from exc
    return result.stdout


def _canonical_remote(value: str) -> str:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        value.strip(),
    )
    if not match:
        raise ReleaseSpecError("origin must be a GitHub HTTPS/SSH repository URL")
    return f"https://github.com/{match.group(1)}/{match.group(2)}.git"


def _repo_identity(path_value: Any, name: str) -> tuple[Path, str, str]:
    repo = _directory(path_value, f"repositories.{name}")
    if _git_run(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseSpecError(f"repository {name} is not clean")
    head = str(_git_run(repo, "rev-parse", "HEAD")).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ReleaseSpecError(f"repository {name} HEAD is invalid")
    remote = _canonical_remote(
        str(_git_run(repo, "remote", "get-url", "origin")).strip()
    )
    expected = menhir_schema.EXPECTED_REPO_REMOTES[name]
    if remote != expected:
        raise ReleaseSpecError(
            f"repository {name} origin mismatch: expected {expected}, got {remote}"
        )
    tips = set(str(_git_run(
        repo, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin"
    )).splitlines())
    if head not in tips:
        raise ReleaseSpecError(
            f"repository {name} HEAD is not a remote-tracking tip"
        )
    return repo, head, remote


def _git_blob(repo: Path, commit: str, source: str, label: str) -> bytes:
    if not source or source.startswith("/") or "\\" in source or (
        ".." in source.split("/")
    ):
        raise ReleaseSpecError(f"{label} is not a canonical repository path")
    data = _git_run(repo, "show", f"{commit}:{source}", text=False)
    if not isinstance(data, bytes):
        raise AssertionError("binary git output expected")
    return data
