"""Change-log provider: the swap seam between recall and the git adapter.

CachedGitChangeLog runs the git adapter at most once per (repo, HEAD, since, paths) and
memoizes in-process. The key includes the current HEAD, so a new commit transparently
busts the entry. A persistent sidecar backend (survives restarts; pre-populatable when
repos are absent at recall) is a Forward item and can replace this class behind the same
Protocol without touching callers.
"""
from __future__ import annotations

from typing import Callable, Protocol

from menhir.domain.git_staleness import GitChange
from menhir.infrastructure.git_log import capture_changes, current_head


class ChangeLogProvider(Protocol):
    def changes(self, repo_path: str, *, since_commit: str | None,
                paths: list[str]) -> list[GitChange]: ...


class CachedGitChangeLog:
    def __init__(self, *, head_fn: Callable[..., tuple[str, str]] = current_head,
                 capture_fn: Callable[..., list[GitChange]] = capture_changes) -> None:
        self._head_fn = head_fn
        self._capture_fn = capture_fn
        self._cache: dict[tuple, list[GitChange]] = {}

    def changes(self, repo_path: str, *, since_commit: str | None,
                paths: list[str]) -> list[GitChange]:
        head, _branch = self._head_fn(repo_path)
        key = (repo_path, head, since_commit, frozenset(paths))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        out = self._capture_fn(repo_path, since_commit=since_commit, paths=paths)
        self._cache[key] = out
        return out
