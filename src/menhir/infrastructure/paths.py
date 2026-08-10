"""Workspace path resolution helpers.

Eliminates fragile Path(__file__).parents[N] navigation by using environment
variables and anchor-based discovery.
"""

from __future__ import annotations

import os
from pathlib import Path


def workspace_root() -> Path:
    """Return workspace root, preferring env var, falling back to anchor search.

    Priority:
    1. WORKSPACE_ROOT environment variable
    2. Search upward for workspace marker (CLAUDE.md + projects/ dir)
    3. Fallback: use parents traversal (preserves backward compatibility)
    """
    configured = os.getenv("WORKSPACE_ROOT", "").strip()
    if configured:
        return Path(configured)

    # Walk up from this file until we find the workspace marker
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "CLAUDE.md").exists() and (p / "projects").is_dir():
            return p

    # Last resort: assume standard structure (memory/src/menhir/infrastructure/paths.py)
    return here.parents[4]


def projects_dir() -> Path:
    """Return the projects directory (workspace_root/projects)."""
    return workspace_root() / "projects"


def scheduler_project_dir() -> Path:
    """Return the scheduler project directory.

    Uses SCHEDULER_DIR_NAME env var (default: cth.mcp.scheduler).
    """
    scheduler_name = os.getenv("SCHEDULER_DIR_NAME", "cth.mcp.scheduler")
    return projects_dir() / "ctharvey" / scheduler_name


def telemetry_db_path() -> Path:
    """Return the default telemetry database path.

    Override with menhir_MCP_TELEMETRY_DB env var.
    """
    override = os.getenv("MENHIR_MCP_TELEMETRY_DB")
    if override:
        return Path(override)
    return workspace_root() / ".agent" / "mcp_telemetry.db"


def oauth_as_db_path(configured: str = "") -> Path:
    """Return the directory for embedded OAuth AS / client-token state.

    Override with MENHIR_OAUTH_AS_DIR env var.
    """
    if configured:
        return Path(configured)
    override = os.getenv("MENHIR_OAUTH_AS_DIR")
    if override:
        return Path(override)
    return workspace_root() / ".agent"


def repo_root_anchor(current_file: Path, marker: str = "pyproject.toml") -> Path:
    """Find project root by walking up from current file looking for marker.

    Args:
        current_file: __file__ from the calling module
        marker: Filename to search for (default: pyproject.toml)

    Returns:
        Path to directory containing marker, or parents[3] as fallback
    """
    here = Path(current_file).resolve()
    for p in here.parents:
        if (p / marker).exists():
            return p
    return here.parents[3]


def repo_root_for_project(project: str) -> Path | None:
    """Return the on-disk repo root for a project name, or None if not a git repo.

    Resolves under projects_dir(); accepts either '<project>' or '<owner>/<project>'.
    Returns the path only when it exists and contains a .git entry; else None.

    Args:
        project: Project name (e.g., 'myproject' or 'owner/myproject').

    Returns:
        Path to the repo root if .git exists; None if not found or not a git repo.
    """
    if not project:
        return None

    pdir = projects_dir()

    # Try direct match: projects_dir/project
    direct_path = pdir / project
    if direct_path.is_dir() and (direct_path / ".git").exists():
        return direct_path

    # Try owner/project: projects_dir/*/project
    for owner_dir in pdir.iterdir():
        if not owner_dir.is_dir():
            continue
        candidate = owner_dir / project
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate

    return None
