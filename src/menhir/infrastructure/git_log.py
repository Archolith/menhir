"""Low-level git adapter for belief-gate staleness signals.

Produces GitChange records from git log --name-status over a commit range,
with exact field construction matching the signature expected by
derive_structural_staleness (git_staleness.py).

Pure subprocess execution; no graph/extraction access. The caller supplies
repo path and optional bounds; this module only does git output parsing.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from menhir.domain.git_staleness import GitChange


def current_head(repo_path: str, *, runner: Callable[[list[str]], str] | None = None) -> tuple[str, str]:
    """Get the current HEAD commit SHA and branch name.

    Returns (head_sha, branch) via git rev-parse HEAD and git rev-parse --abbrev-ref HEAD.

    Args:
        repo_path: Path to the git repository.
        runner: Optional subprocess runner. Defaults to subprocess.run with git -C repo_path.

    Returns:
        Tuple of (commit_sha, branch_name).

    Raises:
        subprocess.CalledProcessError: If git command fails.
        subprocess.FileNotFoundError: If git is not available.
        subprocess.TimeoutExpired: If git command times out.
    """
    if runner is None:
        def default_runner(args: list[str]) -> str:
            return subprocess.run(
                ["git", "-C", repo_path, *args],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=True,
            ).stdout.strip()
        runner = default_runner

    head_sha = runner(["rev-parse", "HEAD"]).strip()
    branch = runner(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    return (head_sha, branch)


def capture_changes(
    repo_path: str,
    *,
    since_commit: str | None = None,
    paths: list[str] | None = None,
    timeout_s: float = 5.0,
    runner: Callable[[list[str]], str] | None = None,
) -> list[GitChange]:
    """Capture file changes from git log over a commit range.

    Parses git log --name-status output into GitChange records, one per touched path.
    Supports file modifications (M), additions (A), deletions (D), and renames (R).

    Args:
        repo_path: Path to the git repository.
        since_commit: Optional starting commit. If set, logs from since_commit..HEAD;
            otherwise logs HEAD only. Each returned change has descends_from={since_commit}
            when since_commit is set (indicating the change descends from that commit).
        paths: Optional list of paths to filter. If set, only changes to these paths
            are returned.
        timeout_s: Subprocess timeout in seconds. Defaults to 5.0.
        runner: Optional subprocess runner. Defaults to subprocess.run with git -C repo_path.

    Returns:
        List of GitChange records, one per touched path. Empty if no changes.

    Raises:
        subprocess.CalledProcessError: If git command fails.
        subprocess.FileNotFoundError: If git is not available.
        subprocess.TimeoutExpired: If git command times out.
    """
    if runner is None:
        def default_runner(args: list[str]) -> str:
            return subprocess.run(
                ["git", "-C", repo_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=True,
            ).stdout

        runner = default_runner

    # Build revision range: since_commit..HEAD or HEAD
    if since_commit:
        rev_range = f"{since_commit}..HEAD"
    else:
        rev_range = "HEAD"

    # Build git log command with record/field separators for unambiguous parsing
    # \x01 = record separator (between commits)
    # \x1f = field separator (between commit hash and date)
    git_args = [
        "log",
        rev_range,
        "--name-status",
        "-M",  # detect renames with default similarity threshold
        "--date=iso-strict",
        "--format=%x01%H%x1f%cI",
    ]
    if since_commit is None:
        # Bare "HEAD" is a REVISION, not a commit: it selects every ancestor of HEAD. The
        # docstring has always said this path "logs HEAD only", so bound it explicitly.
        # -1 rather than HEAD~1..HEAD because the latter fails on a root commit.
        # This must precede the "--" separator: after it, git reads -1 as a PATHSPEC.
        git_args.append("-1")
    git_args.append("--")
    if paths:
        git_args.extend(paths)

    try:
        output = runner(git_args)
    except (subprocess.CalledProcessError, subprocess.FileNotFoundError, subprocess.TimeoutExpired):
        raise

    if not output:
        return []

    changes: list[GitChange] = []
    descends_from = frozenset({since_commit}) if since_commit else frozenset()

    # Parse output: each record starts with \x01, contains commit info, followed by status lines
    records = output.split("\x01")
    for record in records:
        if not record.strip():
            continue

        lines = record.split("\n")
        if not lines:
            continue

        # First line: commit_sha \x1f commit_date
        header = lines[0].strip()
        if not header:
            continue

        try:
            commit_sha, changed_at = header.split("\x1f", 1)
            commit_sha = commit_sha.strip()
            changed_at = changed_at.strip()
        except ValueError:
            # Malformed header, skip
            continue

        # Parse status lines (M/A/D/R<score> followed by path(s))
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) < 2:
                continue

            status = parts[0]
            renamed_from = None

            if status.startswith("R"):
                # Rename: R<score> old_path new_path
                if len(parts) >= 3:
                    renamed_from = parts[1]
                    target = parts[2]
                else:
                    continue
            elif status in ("M", "A", "D"):
                # Modify, Add, Delete: status path
                target = parts[1]
            else:
                # Unknown status, skip
                continue

            change = GitChange(
                target=target,
                changed_at=changed_at,
                kind="file",
                commit=commit_sha,
                branch=None,
                descends_from=descends_from,
                worktree_hash=None,
                renamed_from=renamed_from,
            )
            changes.append(change)

    return changes
