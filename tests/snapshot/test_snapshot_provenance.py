"""P0 provenance addendum: the snapshot/commit/dirty-state stamp.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` -- "replace the ambiguous
`source_head`-only provenance with base commit, commit-tree OID, branch label, clean/dirty state,
and provenance-quality semantics. Keep `tree_digest` independent of these labels and authoritative
for uploaded bytes."

The property the stamp exists for: a commit id alone reads as "these were the bytes at that
commit" and frequently is not. Everything here is about keeping that distinction legible, and
about keeping it *separate* from the digest, which is the only statement in the manifest that
describes the bytes actually uploaded.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from menhir.snapshot.bundler import build_plan
from menhir.snapshot.protocol import (
    PROVENANCE_SELF_REPORTED,
    PROVENANCE_TRUSTED_AUTOMATION,
    FileRecord,
    GitProvenance,
    SnapshotManifest,
    sha256_hex,
)

pytestmark = pytest.mark.unit


def _record(path: str, body: bytes = b"x") -> FileRecord:
    return FileRecord(path=path, size=len(body), sha256=sha256_hex(body))


# --- the digest stays independent of the labels -------------------------------------------------


def test_tree_digest_is_identical_for_clean_and_dirty_provenance() -> None:
    """Same bytes, same digest -- whatever the source checkout was doing when they were read."""
    files = [_record("a.py", b"one")]
    clean = SnapshotManifest.build(
        display_name="p", files=files,
        provenance=GitProvenance(base_commit="a" * 40, commit_tree="b" * 40, dirty=False),
    )
    dirty = SnapshotManifest.build(
        display_name="p", files=files,
        provenance=GitProvenance(base_commit="c" * 40, commit_tree="d" * 40, dirty=True),
    )
    assert clean.tree_digest == dirty.tree_digest


def test_provenance_travels_in_the_manifest() -> None:
    manifest = SnapshotManifest.build(
        display_name="p",
        files=[_record("a.py")],
        provenance=GitProvenance(
            base_commit="a" * 40, commit_tree="b" * 40, branch="main", dirty=True
        ),
    )
    decoded = json.loads(manifest.to_canonical_bytes())
    assert decoded["provenance"] == {
        "base_commit": "a" * 40,
        "commit_tree": "b" * 40,
        "branch": "main",
        "dirty": True,
        "quality": PROVENANCE_SELF_REPORTED,
    }
    assert "source_head" not in decoded


def test_roundtrip_preserves_the_stamp() -> None:
    manifest = SnapshotManifest.build(
        display_name="p",
        files=[_record("a.py")],
        provenance=GitProvenance(
            base_commit="a" * 40, commit_tree="b" * 40, branch="feature/x", dirty=True
        ),
    )
    parsed = SnapshotManifest.from_mapping(json.loads(manifest.to_canonical_bytes()))
    assert parsed.provenance == manifest.provenance


# --- quality is never self-assigned -------------------------------------------------------------


def test_a_client_cannot_claim_trusted_automation() -> None:
    """Exactly the claim this field exists to refuse. Only the server may raise it."""
    parsed = GitProvenance.from_mapping(
        {"base_commit": "a" * 40, "quality": PROVENANCE_TRUSTED_AUTOMATION}
    )
    assert parsed.quality == PROVENANCE_SELF_REPORTED


def test_a_manifest_claiming_trusted_automation_is_downgraded_on_parse() -> None:
    manifest = SnapshotManifest.build(display_name="p", files=[_record("a.py")])
    raw = json.loads(manifest.to_canonical_bytes())
    raw["provenance"]["quality"] = PROVENANCE_TRUSTED_AUTOMATION

    parsed = SnapshotManifest.from_mapping(raw)

    assert parsed.provenance.quality == PROVENANCE_SELF_REPORTED


def test_missing_or_malformed_provenance_is_absent_not_invented() -> None:
    assert GitProvenance.from_mapping(None) == GitProvenance()
    assert GitProvenance.from_mapping({}) == GitProvenance()
    assert GitProvenance.from_mapping({"base_commit": "   "}).base_commit is None
    manifest = SnapshotManifest.from_mapping(
        json.loads(
            SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes()
        )
    )
    assert manifest.provenance.quality == PROVENANCE_SELF_REPORTED


# --- against a real repository ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    try:
        _git(project, "init", "--quiet")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    _git(project, "config", "user.email", "test@example.invalid")
    _git(project, "config", "user.name", "Test")
    (project / "main.py").write_bytes(b"print('hi')\n")
    _git(project, "add", "-A")
    _git(project, "commit", "--quiet", "-m", "initial")
    return project


def test_a_clean_checkout_reports_clean_and_names_its_commit(repo: Path) -> None:
    provenance = build_plan(repo).manifest.provenance

    assert provenance.dirty is False
    assert provenance.base_commit == _git(repo, "rev-parse", "HEAD").strip()
    assert provenance.commit_tree == _git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert provenance.branch == _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert provenance.quality == PROVENANCE_SELF_REPORTED


def test_an_edited_tracked_file_makes_the_snapshot_dirty(repo: Path) -> None:
    """The case `source_head` could not express: same commit id, different bytes."""
    clean = build_plan(repo).manifest
    (repo / "main.py").write_bytes(b"print('edited')\n")
    dirty = build_plan(repo).manifest

    assert clean.provenance.dirty is False
    assert dirty.provenance.dirty is True
    assert dirty.provenance.base_commit == clean.provenance.base_commit
    assert dirty.tree_digest != clean.tree_digest, (
        "the digest must move with the bytes even though the commit label did not"
    )


def test_a_staged_change_counts_as_dirty(repo: Path) -> None:
    """Staged bytes are in the working tree, so they are in the bundle."""
    (repo / "main.py").write_bytes(b"print('staged')\n")
    _git(repo, "add", "main.py")

    assert build_plan(repo).manifest.provenance.dirty is True


def test_untracked_files_do_not_make_a_snapshot_dirty(repo: Path) -> None:
    """They never enter the bundle, so they cannot make it differ from the base commit.

    Counting them would report nearly every working repository as dirty for content it did not
    send, which makes the flag useless exactly when someone needs to trust it.
    """
    (repo / "scratch.txt").write_bytes(b"not tracked\n")

    assert build_plan(repo).manifest.provenance.dirty is False


def test_a_detached_head_reports_no_branch(repo: Path) -> None:
    _git(repo, "checkout", "--quiet", "--detach", "HEAD")

    provenance = build_plan(repo).manifest.provenance

    assert provenance.branch is None
    assert provenance.base_commit is not None


def test_a_repository_with_no_commits_still_syncs(repo: Path, tmp_path: Path) -> None:
    """No HEAD is a legitimate state, not a failure: the stamp is absent, the bundle is not."""
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "--quiet")
    _git(empty, "config", "user.email", "test@example.invalid")
    _git(empty, "config", "user.name", "Test")
    (empty / "a.py").write_bytes(b"x\n")
    _git(empty, "add", "-A")

    plan = build_plan(empty)

    assert plan.manifest.provenance.base_commit is None
    assert plan.manifest.provenance.commit_tree is None
    assert [r.path for r in plan.manifest.files] == ["a.py"]


def test_the_stamp_carries_no_workstation_path_or_remote_url(repo: Path) -> None:
    """Plan: workstation paths and locator metadata never cross MCP from this protocol."""
    _git(repo, "remote", "add", "origin", "https://user:token@example.invalid/org/repo.git")

    raw = build_plan(repo).manifest.to_canonical_bytes().decode("utf-8")

    assert "example.invalid" not in raw
    assert "token" not in raw
    assert str(repo) not in raw
