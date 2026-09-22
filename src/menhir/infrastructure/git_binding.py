"""Read the git commit a project scan describes (Beacon evidence binding).

Fixed, read-only local git queries: no network (``protocol.allow=never``), no index refresh
(``--no-optional-locks``), no prompts, no repository-configured programs (fsmonitor and hooks
are disabled), bounded by a timeout. Anything unexpected yields ``None`` -- a scan never fails
because git is missing or misbehaves; the binding is simply absent and Beacon evidence for that
project is refused.
"""

from __future__ import annotations

import os
import re

# Fixed local git inspection only: fixed argv, no shell, no network.
import subprocess  # nosec B404
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_TIMEOUT_SECONDS = 30
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HARDENING = (
    "-c", "protocol.allow=never",
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
)


def _git(root: Path, *args: str) -> str | None:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    try:
        completed = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["git", "--no-optional-locks", *_HARDENING, *args],
            cwd=str(root),
            capture_output=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _without_credentials(url: str) -> str:
    """Drop userinfo from a URL origin; scp-style ``git@host:path`` is left as is."""
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def read_git_binding(root: Path) -> tuple[str, str, bool] | None:
    """Return ``(commit, repository, dirty)`` for the repository at *root*, or ``None``.

    Only a repository whose top level is *root* counts: a project directory nested inside
    another repository is not bound to that repository's commit.
    """
    dot_git = root / ".git"
    if not (dot_git.is_dir() or dot_git.is_file()):
        return None
    commit = (_git(root, "rev-parse", "--verify", "HEAD") or "").strip()
    if not _OID.match(commit):
        return None
    origin = (_git(root, "remote", "get-url", "origin") or "").strip()
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    dirty = status is None or bool(status.strip())
    return commit, _without_credentials(origin), dirty
