"""Tests for the cached change-log provider (swap seam for a future sidecar backend)."""

from __future__ import annotations

from menhir.domain.git_staleness import GitChange
from menhir.services.change_log_provider import CachedGitChangeLog


def test_caches_by_repo_head_since() -> None:
    """The cache key includes repo, HEAD SHA, since_commit, and paths.
    A second call with identical args serves from cache; capture_fn is called exactly once."""

    call_count = 0

    def mock_head_fn(repo_path: str) -> tuple[str, str]:
        return ("HEADSHA", "main")

    def mock_capture_fn(repo_path: str, *, since_commit: str | None, paths: list[str]) -> list[GitChange]:
        nonlocal call_count
        call_count += 1
        return [GitChange(target="a.py", changed_at="2026-01-01", commit="x")]

    provider = CachedGitChangeLog(head_fn=mock_head_fn, capture_fn=mock_capture_fn)

    # First call
    result1 = provider.changes("/r", since_commit="C", paths=["a.py"])
    assert call_count == 1
    assert result1 == [GitChange(target="a.py", changed_at="2026-01-01", commit="x")]

    # Second call with identical args — should be served from cache
    result2 = provider.changes("/r", since_commit="C", paths=["a.py"])
    assert call_count == 1  # capture_fn not called again
    assert result2 == result1


def test_new_head_busts_cache() -> None:
    """When HEAD moves, the cache key changes, so the same .changes(...) call triggers capture_fn again."""

    call_count = 0
    head_sequence = ["H1", "H2"]
    head_index = 0

    def mock_head_fn(repo_path: str) -> tuple[str, str]:
        nonlocal head_index
        result = (head_sequence[head_index], "main")
        head_index += 1
        return result

    def mock_capture_fn(repo_path: str, *, since_commit: str | None, paths: list[str]) -> list[GitChange]:
        nonlocal call_count
        call_count += 1
        return [GitChange(target="a.py", changed_at="2026-01-01", commit="x")]

    provider = CachedGitChangeLog(head_fn=mock_head_fn, capture_fn=mock_capture_fn)

    # First call with HEAD=H1
    result1 = provider.changes("/r", since_commit="C", paths=["a.py"])
    assert call_count == 1

    # Second call with identical args but HEAD=H2 — cache miss because key includes HEAD
    result2 = provider.changes("/r", since_commit="C", paths=["a.py"])
    assert call_count == 2  # capture_fn called again despite identical args
    assert result1 == result2
