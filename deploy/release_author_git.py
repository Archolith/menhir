"""Git repository identity, remote binding, and blob access for release authoring."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from release_author_constants import EXPECTED_REPO_REMOTES


def _canonical_remote(value: str) -> str:
    match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+?)(?:\.git)?/?",
        value.strip(),
    )
    if not match:
        raise ValueError("origin must be a GitHub HTTPS/SSH repository URL")
    return f"https://github.com/{match.group(1)}/{match.group(2)}.git"


def _repo_identity(path_value: Any, label: str) -> tuple[Path, str, str]:
    if not isinstance(path_value, str) or not path_value:
        raise ValueError(f"repository {label} path is required")
    path = Path(path_value)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(f"repository {label} must be an absolute non-symlink directory")
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain=v1", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ValueError(f"repository {label} is not clean")
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError(f"repository {label} HEAD is not an immutable commit")
    remote = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    canonical = _canonical_remote(remote)
    if canonical != EXPECTED_REPO_REMOTES[label]:
        raise ValueError(
            f"repository {label} origin identity mismatch: "
            f"expected {EXPECTED_REPO_REMOTES[label]}, got {canonical}"
        )
    return path, head, canonical


def _git_blob(repo: Path, commit: str, source_path: str, label: str) -> tuple[bytes, str]:
    if not isinstance(source_path, str) or not source_path or source_path.startswith("/") \
            or ".." in source_path.split("/") or "\\" in source_path:
        raise ValueError(f"{label} must be a canonical repository-relative path")
    try:
        data = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{source_path}"],
            check=True,
            capture_output=True,
        ).stdout
        blob_oid = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", f"{commit}:{source_path}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{label} is not a committed blob at {commit}") from exc
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob_oid):
        raise ValueError(f"{label} git blob object id is invalid")
    return data, blob_oid


def _git_package_files(repo: Path, commit: str) -> dict[str, bytes]:
    prefix = "src/archolith_oauth/"
    raw = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", "-z",
         commit, "--", prefix],
        check=True,
        capture_output=True,
    ).stdout
    paths = [item.decode("utf-8") for item in raw.split(b"\0") if item]
    if not paths or any(not path.startswith(prefix) for path in paths):
        raise ValueError("reviewed OAuth source commit has no canonical package tree")
    return {
        path.removeprefix(prefix): subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        for path in paths
    }


def _source_tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, payload in sorted(files.items()):
        encoded = path.encode("utf-8")
        digest.update(b"oauth-wheel-source-v1\0")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


def _git_package_tree_digest(repo: Path, commit: str) -> str:
    return _source_tree_digest(_git_package_files(repo, commit))

