"""Tests for git_log module — parsing git log --name-status into GitChange records.

Exercises end-to-end parsing via real git repositories created in temp directories.
Skipped if git is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from menhir.infrastructure.git_log import capture_changes, current_head


# Skip all tests in this module if git is not available
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not available",
)


@pytest.fixture
def git_repo():
    """Create a temporary git repository with initial commit.

    Sets user.email and user.name. Yields the repo path; cleans up on teardown.
    """
    tmpdir = tempfile.mkdtemp()
    repo_path = Path(tmpdir)

    try:
        # Initialize repo and configure user
        subprocess.run(
            ["git", "-C", str(repo_path), "init", "-q"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.email", "test@example.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "config", "user.name", "Test User"],
            check=True,
            capture_output=True,
        )

        # Create initial commit
        initial_file = repo_path / "initial.txt"
        initial_file.write_text("initial content\n")
        subprocess.run(
            ["git", "-C", str(repo_path), "add", "initial.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-q", "-m", "Initial commit"],
            check=True,
            capture_output=True,
        )

        yield str(repo_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_no_changes_since_head_is_empty(git_repo):
    """capture_changes(since=HEAD) should return [] because HEAD..HEAD is empty."""
    # Get current HEAD
    head_sha, _ = current_head(git_repo)

    # Capture changes since HEAD — should be empty (no commits after HEAD)
    changes = capture_changes(git_repo, since_commit=head_sha, paths=["initial.txt"])

    assert changes == []


def test_change_after_belief_commit_is_captured(git_repo):
    """After recording HEAD, modify+commit a file; change should be captured."""
    # Record the belief commit
    head_at_belief, _ = current_head(git_repo)

    # Modify and commit a.py
    a_file = Path(git_repo) / "a.py"
    a_file.write_text("def foo():\n    pass\n")
    subprocess.run(
        ["git", "-C", git_repo, "add", "a.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Add a.py"],
        check=True,
        capture_output=True,
    )

    # Capture changes since belief commit
    changes = capture_changes(git_repo, since_commit=head_at_belief, paths=["a.py"])

    assert len(changes) == 1
    change = changes[0]
    assert change.target == "a.py"
    assert change.kind == "file"
    assert change.commit is not None
    assert change.changed_at is not None
    assert head_at_belief in change.descends_from
    assert change.renamed_from is None


def test_unrelated_path_change_is_not_captured(git_repo):
    """Modify b.py after belief, but request changes for a.py only — should be empty."""
    head_at_belief, _ = current_head(git_repo)

    # Commit b.py
    b_file = Path(git_repo) / "b.py"
    b_file.write_text("# b.py\n")
    subprocess.run(
        ["git", "-C", git_repo, "add", "b.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Add b.py"],
        check=True,
        capture_output=True,
    )

    # Capture changes for a.py only — b.py should be filtered out
    changes = capture_changes(git_repo, since_commit=head_at_belief, paths=["a.py"])

    assert changes == []


def test_rename_sets_renamed_from(git_repo):
    """git mv a.py c.py; capture should include renamed_from=='a.py', target=='c.py'."""
    # Create and commit a.py
    a_file = Path(git_repo) / "a.py"
    a_file.write_text("# original a.py\n")
    subprocess.run(
        ["git", "-C", git_repo, "add", "a.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Add a.py"],
        check=True,
        capture_output=True,
    )

    # Record belief commit before the rename
    head_at_belief, _ = current_head(git_repo)

    # Rename a.py to c.py
    subprocess.run(
        ["git", "-C", git_repo, "mv", "a.py", "c.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Rename a.py to c.py"],
        check=True,
        capture_output=True,
    )

    # Capture changes since belief — should include rename
    changes = capture_changes(
        git_repo,
        since_commit=head_at_belief,
        paths=["a.py", "c.py"],
    )

    assert len(changes) == 1
    change = changes[0]
    assert change.target == "c.py"
    assert change.renamed_from == "a.py"
    assert change.kind == "file"
    assert change.commit is not None


def test_bad_repo_path_via_injected_runner(git_repo):
    """Injected runner returning empty string should return [] (no output parsed)."""

    def bad_runner(args: list[str]) -> str:
        return ""

    changes = capture_changes(
        git_repo,
        since_commit="HEAD",
        paths=["a.py"],
        runner=bad_runner,
    )

    assert changes == []


def test_multiple_changes_in_same_commit(git_repo):
    """Commit multiple files; all should appear in changes."""
    head_at_belief, _ = current_head(git_repo)

    # Create and commit multiple files
    files = ["file1.py", "file2.py", "file3.txt"]
    for fname in files:
        (Path(git_repo) / fname).write_text(f"content of {fname}\n")
    subprocess.run(
        ["git", "-C", git_repo, "add"] + files,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Add multiple files"],
        check=True,
        capture_output=True,
    )

    # Capture all changes
    changes = capture_changes(git_repo, since_commit=head_at_belief)

    assert len(changes) == 3
    targets = {c.target for c in changes}
    assert targets == {"file1.py", "file2.py", "file3.txt"}


def test_delete_is_captured(git_repo):
    """Deleting a file should produce a GitChange with target set."""
    # Create and commit a file
    del_file = Path(git_repo) / "to_delete.py"
    del_file.write_text("delete me\n")
    subprocess.run(
        ["git", "-C", git_repo, "add", "to_delete.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Add file to delete"],
        check=True,
        capture_output=True,
    )

    head_at_belief, _ = current_head(git_repo)

    # Delete the file
    del_file.unlink()
    subprocess.run(
        ["git", "-C", git_repo, "add", "to_delete.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", git_repo, "commit", "-q", "-m", "Delete file"],
        check=True,
        capture_output=True,
    )

    # Capture changes
    changes = capture_changes(git_repo, since_commit=head_at_belief)

    assert len(changes) == 1
    change = changes[0]
    assert change.target == "to_delete.py"


def test_current_head_returns_sha_and_branch(git_repo):
    """current_head should return valid SHA and branch name."""
    sha, branch = current_head(git_repo)

    # SHA should be 40 hex characters
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)

    # Branch should be a non-empty string (typically 'master' for new repos)
    assert isinstance(branch, str)
    assert len(branch) > 0
