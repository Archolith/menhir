"""Filesystem locations for Menhir's own durable state.

Menhir keeps two kinds of local state outside Neo4j: the SQLite telemetry sidecar and the
embedded OAuth authorization-server files (client registry, codes, refresh tokens, signing
key). Both live under one *state directory*:

1. ``MENHIR_STATE_DIR`` when set;
2. otherwise ``WORKSPACE_ROOT/.agent`` when ``WORKSPACE_ROOT`` is set (legacy layout: the
   operator workspace that hosted Menhir before it had its own state directory);
3. otherwise ``~/.menhir``.

Each file keeps its own narrower override (``MENHIR_MCP_TELEMETRY_DB``, ``MENHIR_OAUTH_AS_DIR``)
so a deployment can split them across mounts. Nothing here infers a location from where the
package is installed: a pip, pipx, or container install has no meaningful ancestor directory.

``projects_dir`` / ``repo_root_for_project`` cover one optional convenience -- turning an
ingested project name back into an on-disk checkout for git staleness evidence -- and only
work when ``WORKSPACE_ROOT`` describes a ``projects/<owner>/<name>`` tree. Without it they
return ``None`` and callers treat the evidence as unavailable.
"""

from __future__ import annotations

import os
from pathlib import Path

LEGACY_WORKSPACE_STATE_SUBDIR = ".agent"
DEFAULT_STATE_DIR_NAME = ".menhir"


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def workspace_root() -> Path | None:
    """Return the legacy operator workspace root, or ``None`` when ``WORKSPACE_ROOT`` is unset."""

    return _env_path("WORKSPACE_ROOT")


def state_dir() -> Path:
    """Return the directory that holds Menhir's local durable state (see module docstring)."""

    configured = _env_path("MENHIR_STATE_DIR")
    if configured is not None:
        return configured
    legacy_root = workspace_root()
    if legacy_root is not None:
        return legacy_root / LEGACY_WORKSPACE_STATE_SUBDIR
    return Path.home() / DEFAULT_STATE_DIR_NAME


def projects_dir() -> Path | None:
    """Return ``WORKSPACE_ROOT/projects`` when the legacy workspace layout is configured, else ``None``."""

    root = workspace_root()
    return root / "projects" if root is not None else None


def telemetry_db_path() -> Path:
    """Return the SQLite telemetry sidecar path.

    ``MENHIR_MCP_TELEMETRY_DB`` overrides; the default is ``state_dir()/mcp_telemetry.db``.
    """

    override = _env_path("MENHIR_MCP_TELEMETRY_DB")
    if override is not None:
        return override
    return state_dir() / "mcp_telemetry.db"


def oauth_as_db_path(configured: str = "") -> Path:
    """Return the directory for embedded OAuth AS / client-token state.

    An explicit ``configured`` value (from settings) wins, then ``MENHIR_OAUTH_AS_DIR``, then
    ``state_dir()``.
    """

    if configured:
        return Path(configured).expanduser()
    override = _env_path("MENHIR_OAUTH_AS_DIR")
    if override is not None:
        return override
    return state_dir()


def repo_root_anchor(current_file: Path, marker: str = "pyproject.toml") -> Path:
    """Find a source checkout root by walking up from ``current_file`` looking for ``marker``.

    Only meaningful for repository-managed assets (git hooks, launcher scripts); falls back to
    ``parents[3]`` when no marker is found so callers keep their historical behaviour.
    """

    here = Path(current_file).resolve()
    for p in here.parents:
        if (p / marker).exists():
            return p
    return here.parents[3]


def repo_root_for_project(project: str) -> Path | None:
    """Return the on-disk repo root for an ingested project name, or ``None``.

    Resolves under ``projects_dir()``; accepts ``<project>`` or ``<owner>/<project>``. Returns a
    path only when it exists and contains ``.git``. ``None`` when the legacy workspace layout
    is not configured.
    """

    if not project:
        return None

    pdir = projects_dir()
    if pdir is None or not pdir.is_dir():
        return None

    direct_path = pdir / project
    if direct_path.is_dir() and (direct_path / ".git").exists():
        return direct_path

    for owner_dir in pdir.iterdir():
        if not owner_dir.is_dir():
            continue
        candidate = owner_dir / project
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate

    return None
