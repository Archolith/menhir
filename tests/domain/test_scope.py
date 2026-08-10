"""Tests for memory scope identity + conflict (SelfToleranceGate substrate)."""

from __future__ import annotations

from menhir.domain.scope import MemoryScope, scope_conflict


def test_no_conflict_when_same() -> None:
    q = MemoryScope(repo="menhir", project="archolith")
    c = MemoryScope(repo="menhir", project="archolith")
    assert scope_conflict(q, c) == (False, ())


def test_conflict_on_repo() -> None:
    conflict, dims = scope_conflict(MemoryScope(repo="menhir"), MemoryScope(repo="yawn.dashboard"))
    assert conflict is True and dims == ("repo",)


def test_same_filename_different_project_conflicts() -> None:
    conflict, dims = scope_conflict(MemoryScope(project="yawn.seed"), MemoryScope(project="yawn.market"))
    assert conflict is True and "project" in dims


def test_unspecified_dimension_never_conflicts() -> None:
    # query has no branch; candidate is on a branch -> not a conflict (unknown != non-self)
    assert scope_conflict(MemoryScope(repo="menhir"), MemoryScope(repo="menhir", branch="feature/x")) == (False, ())


def test_multiple_conflicting_dimensions_reported_in_order() -> None:
    conflict, dims = scope_conflict(
        MemoryScope(repo="a", project="p", branch="main"),
        MemoryScope(repo="b", project="q", branch="dev"),
    )
    assert conflict is True
    assert dims == ("repo", "project", "branch")
