"""P1 tests: selection policy, git enumeration edge cases, and deterministic archive bytes.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`.

Exotic index states (gitlinks, symlinks, unmerged stages) are driven through an injected git
runner rather than by building them on disk: creating a symlink needs privileges on Windows and a
submodule needs `protocol.file.allow`, and neither detour tests anything this module owns. The
P1 gate itself -- identical digests across runs, and a repository untouched by a sync -- runs
against a real repository, because that is the claim being made.
"""

from __future__ import annotations

import io
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from menhir.snapshot.bundler import (
    ERR_CONTENT_CHANGED,
    ERR_UNMERGED,
    BundlerError,
    build_plan,
    estimate_archive_upper_bound,
    write_bundle,
)
from menhir.snapshot.policy import (
    Decision,
    SelectionPolicy,
    excluded_dir_reason,
    is_local_receipt,
    secret_risk_label,
)
from menhir.snapshot.protocol import (
    CONTENT_PREFIX,
    MANIFEST_NAME,
    PROVISIONAL_LIMITS,
    OmissionReason,
    SnapshotLimits,
)

pytestmark = pytest.mark.unit

_FAKE_SHA = "0" * 40
_FAKE_HEAD = "a" * 40


class FakeGit:
    """Minimal `git -C <root>` stand-in: repository root, HEAD, and a scripted index."""

    def __init__(self, root: Path, entries: Sequence[tuple[str, str]], *, stage: str = "0") -> None:
        self.root = root
        self.entries = entries
        self.stage = stage
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> bytes:
        self.calls.append(tuple(args))
        if args[0] == "rev-parse" and args[1] == "--show-toplevel":
            return str(self.root).encode("utf-8")
        if args[0] == "rev-parse" and args[1] == "HEAD":
            return _FAKE_HEAD.encode("ascii")
        if args[0] == "ls-files":
            out = b""
            for mode, path in self.entries:
                # surrogateescape, like real git: it emits raw bytes, not decoded text, so a
                # filename in another encoding must survive the fixture unchanged.
                record = f"{mode} {_FAKE_SHA} {self.stage}\t{path}".encode(
                    "utf-8", "surrogateescape"
                )
                out += record + b"\0"
            return out
        raise AssertionError(f"unexpected git call: {args}")


def _write(root: Path, rel: str, body: bytes = b"content\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)


def _plan(root: Path, entries, *, limits=PROVISIONAL_LIMITS, policy=None, **kwargs):
    return build_plan(
        root,
        limits=limits,
        policy=policy or SelectionPolicy(max_file_bytes=limits.max_file_bytes),
        runner=FakeGit(root, entries),
        **kwargs,
    )


# --- policy units ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["node_modules/left-pad/index.js", "src/__pycache__/x.pyc", "a/.venv/lib/x.py"]
)
def test_scanner_skipped_directories_are_excluded_at_any_depth(path: str) -> None:
    assert excluded_dir_reason(path) == OmissionReason.EXCLUDED_DIR


def test_root_only_names_are_excluded_only_at_the_root() -> None:
    """The scanner's own rule: pruning `logs` at any depth would delete real packages."""
    assert excluded_dir_reason("logs/run.txt") == OmissionReason.EXCLUDED_DIR
    assert excluded_dir_reason("src/menhir/logs/writer.py") is None


def test_policy_sets_track_the_scanner() -> None:
    """Drift here breaks shadow-scan parity in a way no diff of this file would show."""
    from menhir.infrastructure import project_scanner
    from menhir.snapshot import policy as snapshot_policy

    assert snapshot_policy.EXCLUDED_DIR_NAMES == frozenset(project_scanner._ALWAYS_SKIP_DIRS)
    assert snapshot_policy.ROOT_ONLY_EXCLUDED_DIR_NAMES == frozenset(
        project_scanner._ROOT_ONLY_SKIP_DIRS
    )


@pytest.mark.parametrize(
    "path", [".env", ".env.local", "config/.env.production", "certs/server.pem", "id_rsa",
             "deploy/private.key", ".netrc", "app/secrets.yaml"],
)
def test_secret_risk_paths_are_labelled(path: str) -> None:
    assert secret_risk_label(path) is not None


@pytest.mark.parametrize(
    "path", [".env.example", ".env.sample", "docs/.env.template", "src/main.py", "keys.md"]
)
def test_templates_and_ordinary_files_are_not_secret_risk(path: str) -> None:
    assert secret_risk_label(path) is None


def test_local_receipts_never_travel() -> None:
    assert is_local_receipt(".agent/project-id")
    assert is_local_receipt(".menhir/source.json")
    assert not is_local_receipt(".agent/README.md")


# --- enumeration -------------------------------------------------------------------------------


def test_tracked_file_is_bundled_from_working_tree_bytes(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py", b"dirty edit\n")
    plan = _plan(tmp_path, [("100644", "src/main.py")])
    assert [r.path for r in plan.manifest.files] == ["src/main.py"]
    assert plan.manifest.files[0].size == len(b"dirty edit\n")
    assert plan.manifest.source_head == _FAKE_HEAD


def test_executable_bit_comes_from_the_index_not_the_filesystem(tmp_path: Path) -> None:
    """os.access(X_OK) is meaningless on Windows; git's mode is the same everywhere."""
    _write(tmp_path, "run.sh", b"#!/bin/sh\n")
    _write(tmp_path, "plain.txt")
    plan = _plan(tmp_path, [("100755", "run.sh"), ("100644", "plain.txt")])
    by_path = {r.path: r for r in plan.manifest.files}
    assert by_path["run.sh"].executable is True
    assert by_path["plain.txt"].executable is False


def test_tracked_but_missing_file_is_a_deletion_not_an_omission(tmp_path: Path) -> None:
    _write(tmp_path, "kept.py")
    plan = _plan(tmp_path, [("100644", "kept.py"), ("100644", "gone.py")])
    assert plan.deleted == ("gone.py",)
    assert plan.manifest.deleted_count == 1
    assert [r.path for r in plan.manifest.files] == ["kept.py"]
    assert all(o.path != "gone.py" for o in plan.manifest.omissions)


def test_submodule_is_a_declared_omission(tmp_path: Path) -> None:
    _write(tmp_path, "a.py")
    plan = _plan(tmp_path, [("100644", "a.py"), ("160000", "vendor/lib")])
    assert ("vendor/lib", OmissionReason.SUBMODULE) in [
        (o.path, o.reason) for o in plan.manifest.omissions
    ]


def test_symlink_is_a_declared_omission(tmp_path: Path) -> None:
    _write(tmp_path, "a.py")
    plan = _plan(tmp_path, [("100644", "a.py"), ("120000", "link")])
    assert ("link", OmissionReason.SYMLINK) in [
        (o.path, o.reason) for o in plan.manifest.omissions
    ]


def test_unmerged_paths_stop_the_sync(tmp_path: Path) -> None:
    _write(tmp_path, "a.py")
    with pytest.raises(BundlerError) as excinfo:
        build_plan(tmp_path, runner=FakeGit(tmp_path, [("100644", "a.py")], stage="2"))
    assert excinfo.value.code == ERR_UNMERGED


def test_excluded_directory_contents_are_omitted(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py")
    _write(tmp_path, "node_modules/dep/index.js")
    plan = _plan(tmp_path, [("100644", "src/main.py"), ("100644", "node_modules/dep/index.js")])
    assert [r.path for r in plan.manifest.files] == ["src/main.py"]
    assert [o.reason for o in plan.manifest.omissions] == [OmissionReason.EXCLUDED_DIR]


def test_oversized_file_is_omitted_not_silently_dropped(tmp_path: Path) -> None:
    _write(tmp_path, "big.bin", b"x" * 64)
    _write(tmp_path, "small.py", b"ok")
    limits = SnapshotLimits(max_file_bytes=16)
    plan = _plan(tmp_path, [("100644", "big.bin"), ("100644", "small.py")], limits=limits)
    assert [r.path for r in plan.manifest.files] == ["small.py"]
    assert ("big.bin", OmissionReason.OVERSIZE) in [
        (o.path, o.reason) for o in plan.manifest.omissions
    ]


def test_local_receipt_is_omitted(tmp_path: Path) -> None:
    _write(tmp_path, ".agent/project-id", b"proj-1")
    _write(tmp_path, "a.py")
    plan = _plan(tmp_path, [("100644", ".agent/project-id"), ("100644", "a.py")])
    assert [r.path for r in plan.manifest.files] == ["a.py"]
    assert (".agent/project-id", OmissionReason.RECEIPT) in [
        (o.path, o.reason) for o in plan.manifest.omissions
    ]


def test_gitignore_files_are_bundled_so_the_server_scan_matches(tmp_path: Path) -> None:
    """The server re-runs the scanner, which reads `.gitignore`. Drop it and parity breaks."""
    _write(tmp_path, ".gitignore", b"build/\n")
    _write(tmp_path, "a.py")
    plan = _plan(tmp_path, [("100644", ".gitignore"), ("100644", "a.py")])
    assert ".gitignore" in [r.path for r in plan.manifest.files]


# --- refusals ----------------------------------------------------------------------------------


def test_real_dotenv_blocks_the_sync_and_example_does_not(tmp_path: Path) -> None:
    _write(tmp_path, ".env", b"TOKEN=live\n")
    _write(tmp_path, ".env.example", b"TOKEN=\n")
    plan = _plan(tmp_path, [("100644", ".env"), ("100644", ".env.example")])
    assert plan.blocked
    assert [r.path for r in plan.refusals] == [".env"]
    assert [r.path for r in plan.manifest.files] == [".env.example"]


def test_explicit_override_admits_one_path_only(tmp_path: Path) -> None:
    _write(tmp_path, ".env", b"TOKEN=live\n")
    _write(tmp_path, "server.pem", b"KEY\n")
    policy = SelectionPolicy(
        max_file_bytes=PROVISIONAL_LIMITS.max_file_bytes,
        allowed_secret_paths=frozenset({".env"}),
    )
    plan = _plan(tmp_path, [("100644", ".env"), ("100644", "server.pem")], policy=policy)
    assert [r.path for r in plan.manifest.files] == [".env"]
    assert [r.path for r in plan.refusals] == ["server.pem"]


def test_a_blocked_plan_cannot_be_written(tmp_path: Path) -> None:
    _write(tmp_path, ".env", b"TOKEN=live\n")
    plan = _plan(tmp_path, [("100644", ".env")])
    with pytest.raises(BundlerError):
        write_bundle(plan, io.BytesIO())


def test_refuse_decision_is_distinct_from_omit() -> None:
    policy = SelectionPolicy(max_file_bytes=100)
    assert policy.classify(".env", 10).decision is Decision.REFUSE
    assert policy.classify("node_modules/a.js", 10).decision is Decision.OMIT
    assert policy.classify("src/a.py", 10).decision is Decision.INCLUDE


# --- archive determinism -----------------------------------------------------------------------


def _archive_bytes(plan) -> bytes:
    buffer = io.BytesIO()
    write_bundle(plan, buffer)
    return buffer.getvalue()


def test_archive_bytes_are_identical_across_runs(tmp_path: Path) -> None:
    _write(tmp_path, "src/main.py", b"print('x')\n")
    _write(tmp_path, "README.md", b"# doc\n")
    entries = [("100644", "src/main.py"), ("100644", "README.md")]
    first = _archive_bytes(_plan(tmp_path, entries))
    second = _archive_bytes(_plan(tmp_path, entries))
    assert first == second


def test_archive_entry_order_and_timestamps_are_pinned(tmp_path: Path) -> None:
    _write(tmp_path, "b.py")
    _write(tmp_path, "a.py")
    plan = _plan(tmp_path, [("100644", "b.py"), ("100644", "a.py")])
    with zipfile.ZipFile(io.BytesIO(_archive_bytes(plan))) as archive:
        names = archive.namelist()
        assert names == [MANIFEST_NAME, CONTENT_PREFIX + "a.py", CONTENT_PREFIX + "b.py"]
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_archive_carries_the_manifest_and_content(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", b"body\n")
    plan = _plan(tmp_path, [("100644", "a.py")])
    with zipfile.ZipFile(io.BytesIO(_archive_bytes(plan))) as archive:
        assert archive.read(CONTENT_PREFIX + "a.py") == b"body\n"
        assert plan.manifest.tree_digest.encode() in archive.read(MANIFEST_NAME)


def test_write_refuses_when_content_changed_after_planning(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", b"before\n")
    plan = _plan(tmp_path, [("100644", "a.py")])
    _write(tmp_path, "a.py", b"after\n")
    with pytest.raises(BundlerError) as excinfo:
        write_bundle(plan, io.BytesIO())
    assert excinfo.value.code == ERR_CONTENT_CHANGED


def test_archive_actually_compresses(tmp_path: Path) -> None:
    """Regression: a hand-built ZipInfo defaults to ZIP_STORED and silently wins over the
    archive-level compression, so every entry went out uncompressed while every determinism
    test stayed green. Measured on menhir's own tree: 27.4 MB stored vs 11.2 MB deflated."""
    _write(tmp_path, "a.py", b"def f():\n    return 1\n" * 500)
    plan = _plan(tmp_path, [("100644", "a.py")])
    archive = _archive_bytes(plan)
    assert len(archive) < plan.manifest.total_bytes // 4
    with zipfile.ZipFile(io.BytesIO(archive)) as opened:
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in opened.infolist())


def test_estimate_is_an_upper_bound_on_the_real_archive(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", b"compress me " * 100)
    plan = _plan(tmp_path, [("100644", "a.py")])
    assert len(_archive_bytes(plan)) <= estimate_archive_upper_bound(plan.manifest)


# --- the P1 gate, against a real repository ------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    try:
        _git(repo, "init", "--quiet")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    _write(repo, "src/main.py", b"print('hello')\n")
    _write(repo, "README.md", b"# proj\n")
    _write(repo, ".gitignore", b"build/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


def test_gate_digest_stability(real_repo: Path) -> None:
    first = build_plan(real_repo).manifest
    second = build_plan(real_repo).manifest
    assert first.tree_digest == second.tree_digest
    assert first.to_canonical_bytes() == second.to_canonical_bytes()


def test_gate_repository_state_is_untouched_by_a_sync(real_repo: Path) -> None:
    """P1 gate: planning and writing a bundle must not mutate the index or the working tree."""
    before_status = _git(real_repo, "status", "--porcelain")
    before_head = _git(real_repo, "rev-parse", "HEAD")
    index_before = (real_repo / ".git" / "index").read_bytes()

    plan = build_plan(real_repo)
    write_bundle(plan, io.BytesIO())

    assert _git(real_repo, "status", "--porcelain") == before_status
    assert _git(real_repo, "rev-parse", "HEAD") == before_head
    assert (real_repo / ".git" / "index").read_bytes() == index_before


def test_dirty_tracked_edits_are_visible_without_staging(real_repo: Path) -> None:
    clean = build_plan(real_repo).manifest
    _write(real_repo, "src/main.py", b"print('edited')\n")
    dirty = build_plan(real_repo).manifest
    assert dirty.tree_digest != clean.tree_digest
    assert _git(real_repo, "status", "--porcelain").strip().endswith("src/main.py")


def test_git_directory_is_never_bundled(real_repo: Path) -> None:
    paths = [r.path for r in build_plan(real_repo).manifest.files]
    assert paths == [".gitignore", "README.md", "src/main.py"]
    assert not any(p.startswith(".git/") for p in paths)


def test_untracked_and_ignored_files_are_absent(real_repo: Path) -> None:
    _write(real_repo, "build/artifact.bin", b"junk")
    _write(real_repo, "scratch.txt", b"untracked")
    paths = [r.path for r in build_plan(real_repo).manifest.files]
    assert "build/artifact.bin" not in paths
    assert "scratch.txt" not in paths


def test_deleting_a_tracked_file_is_reported_as_a_deletion(real_repo: Path) -> None:
    (real_repo / "README.md").unlink()
    plan = build_plan(real_repo)
    assert plan.deleted == ("README.md",)
    assert "README.md" not in [r.path for r in plan.manifest.files]


def test_syncing_from_a_subdirectory_covers_the_whole_repository(real_repo: Path) -> None:
    """A subtree-only bundle would look complete and be missing most of the project -- and the
    server prunes whatever a complete snapshot does not contain."""
    from_root = build_plan(real_repo).manifest
    from_subdir = build_plan(real_repo / "src").manifest
    assert from_subdir.tree_digest == from_root.tree_digest
    assert [r.path for r in from_subdir.files] == [".gitignore", "README.md", "src/main.py"]


def test_a_non_utf8_filename_is_omitted_not_a_crash(tmp_path: Path) -> None:
    """git decodes with surrogateescape; a lone surrogate in the manifest cannot be serialized."""
    _write(tmp_path, "ok.py")
    broken = "bad-\udcff-name.py"
    plan = _plan(tmp_path, [("100644", "ok.py"), ("100644", broken)])
    assert [r.path for r in plan.manifest.files] == ["ok.py"]
    assert any(o.reason == OmissionReason.UNREADABLE for o in plan.manifest.omissions)
    plan.manifest.to_canonical_bytes()  # must not raise


def test_file_count_limit_stops_before_reading_more(tmp_path: Path) -> None:
    for name in ("a.py", "b.py", "c.py"):
        _write(tmp_path, name)
    limits = SnapshotLimits(max_file_count=2)
    with pytest.raises(BundlerError) as excinfo:
        _plan(tmp_path, [("100644", n) for n in ("a.py", "b.py", "c.py")], limits=limits)
    assert excinfo.value.code == "snapshot.limit.file_count"


def test_not_a_repository_fails_with_an_actionable_message(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(BundlerError) as excinfo:
        build_plan(plain)
    assert "git repositor" in str(excinfo.value)
