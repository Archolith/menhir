"""Files read during a project scan or corpus audit must stay inside the scanned root.

The ingest guard checks the root a caller names, but the scanners then followed per-file
symlinks: a repository with ``README.md -> /proc/self/environ`` had the server's environment
stored as the project description, and ``x.py -> /dev/zero`` hung the scan. Every read now
requires a regular file inside the root, for every tier.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from menhir.infrastructure import project_scanner
from menhir.infrastructure.artifact_corpus_scanner import (
    build_entry,
    read_index_links,
    scan_corpus,
)
from menhir.infrastructure.path_containment import is_contained_regular_file
from menhir.infrastructure.project_scanner import ProjectScanner, ScanPathRefused

pytestmark = pytest.mark.unit

_SECRET = "MENHIR_TEST_SECRET=Xk9mP2qLw7zQ"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs")


def _symlink(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create symlinks here: {error}")


def _scan_with_timeout(root: Path, seconds: float = 20.0):
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            result["scan"] = ProjectScanner().scan(root, name="symlink-test")
        except BaseException as error:  # pragma: no cover - surfaced below
            result["error"] = error

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "scan did not finish: a read blocked on a non-regular file"
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["scan"]


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text(f'"""{_SECRET}"""\n', encoding="utf-8")
    return secret


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text('"""Real module."""\n\ndef run():\n    pass\n', encoding="utf-8")
    return root


def _all_text(scan) -> str:
    parts = [scan.description or ""]
    parts += [f"{f.rel_path} {f.description}" for f in scan.files]
    parts += [f"{s.name} {s.signature} {s.docstring}" for s in scan.symbols]
    return "\n".join(parts)


def test_readme_symlink_out_of_the_root_is_not_read(repo: Path, outside: Path) -> None:
    _symlink(outside, repo / "README.md")
    scan = _scan_with_timeout(repo)
    assert _SECRET not in _all_text(scan)
    assert "README.md" not in {f.rel_path for f in scan.files}


def test_source_symlink_out_of_the_root_is_not_indexed(repo: Path, outside: Path) -> None:
    _symlink(outside, repo / "leak.py")
    scan = _scan_with_timeout(repo)
    assert "leak.py" not in {f.rel_path for f in scan.files}
    assert _SECRET not in _all_text(scan)
    assert "app.py" in {f.rel_path for f in scan.files}


def test_symlink_inside_the_root_is_still_indexed(repo: Path) -> None:
    (repo / "pkg").mkdir()
    _symlink(repo / "app.py", repo / "pkg" / "alias.py")
    scan = _scan_with_timeout(repo)
    assert "pkg/alias.py" in {f.rel_path for f in scan.files}


def test_read_refused_when_a_file_is_swapped_after_the_walk(repo: Path, outside: Path) -> None:
    link = repo / "late.py"
    _symlink(outside, link)
    token = project_scanner._SCAN_ROOT.set(repo.resolve())
    try:
        with pytest.raises(ScanPathRefused):
            project_scanner._read_text_for_scan(link)
        with pytest.raises(ScanPathRefused):
            project_scanner._getsize_for_scan(link)
        assert project_scanner._extract_module_docstring(str(link)) == ""
    finally:
        project_scanner._SCAN_ROOT.reset(token)


def test_missing_file_keeps_its_read_error(repo: Path) -> None:
    token = project_scanner._SCAN_ROOT.set(repo.resolve())
    try:
        with pytest.raises(project_scanner.ScanReadError):
            project_scanner._read_text_for_scan(repo / "gone.py")
    finally:
        project_scanner._SCAN_ROOT.reset(token)


@posix_only
def test_fifo_source_file_does_not_hang_the_scan(repo: Path) -> None:
    os.mkfifo(repo / "pipe.py")
    scan = _scan_with_timeout(repo)
    assert "pipe.py" not in {f.rel_path for f in scan.files}


def _run_with_timeout(fn, seconds: float = 20.0):
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            result["value"] = fn()
        except BaseException as error:
            result["error"] = error

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), "call did not finish: a read blocked on a non-regular file"
    return result


# --- FU1: the legacy identity file is read before any scan ---------------------------------


def _identity(repo: Path) -> Path:
    path = repo / ".agent" / "project-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_identity_file_symlink_out_of_the_root_is_refused(repo: Path, tmp_path: Path) -> None:
    from menhir.domain.project_id_file import MalformedIdentityFile, read_identity

    foreign = tmp_path / "foreign-id.json"
    foreign.write_text('{"project_id": "stolen"}', encoding="utf-8")
    _symlink(foreign, _identity(repo))
    with pytest.raises(MalformedIdentityFile):
        read_identity(repo)


@posix_only
def test_identity_file_device_symlink_is_refused_without_reading(repo: Path) -> None:
    from menhir.domain.project_id_file import MalformedIdentityFile, read_identity

    _symlink(Path("/dev/zero"), _identity(repo))
    result = _run_with_timeout(lambda: read_identity(repo))
    assert isinstance(result.get("error"), MalformedIdentityFile)


@posix_only
def test_identity_file_fifo_is_refused_without_reading(repo: Path) -> None:
    from menhir.domain.project_id_file import MalformedIdentityFile, read_identity

    os.mkfifo(_identity(repo))
    result = _run_with_timeout(lambda: read_identity(repo))
    assert isinstance(result.get("error"), MalformedIdentityFile)


def test_regular_identity_file_still_reads(repo: Path) -> None:
    from menhir.domain.project_id_file import read_identity

    _identity(repo).write_text('{"project_id": "p-1"}', encoding="utf-8")
    assert read_identity(repo).project_id == "p-1"


# --- FU2: an unexaminable file refuses the scan instead of being pruned --------------------


def test_stat_error_refuses_the_scan(repo: Path, monkeypatch) -> None:
    import errno

    from menhir.infrastructure import path_containment

    real_stat = os.stat
    target = os.path.realpath(repo / "app.py")

    def _flaky_stat(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise OSError(errno.EIO, "Input/output error", target)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(path_containment.os, "stat", _flaky_stat)
    with pytest.raises(OSError, match="could not stat"):
        ProjectScanner().scan(repo, name="flaky")


def test_stat_error_at_read_time_is_a_scan_read_error(repo: Path, monkeypatch) -> None:
    import errno

    from menhir.infrastructure import path_containment

    real_stat = os.stat
    target = os.path.realpath(repo / "app.py")

    def _flaky_stat(path, *args, **kwargs):
        if os.fspath(path) == target:
            raise OSError(errno.ESTALE, "Stale file handle", target)
        return real_stat(path, *args, **kwargs)

    token = project_scanner._SCAN_ROOT.set(repo.resolve())
    monkeypatch.setattr(path_containment.os, "stat", _flaky_stat)
    try:
        with pytest.raises(project_scanner.ScanReadError):
            project_scanner._read_text_for_scan(repo / "app.py")
    finally:
        project_scanner._SCAN_ROOT.reset(token)


# --- FU3: a project that lives under a system directory keeps its files ---------------------


def _pretend_system(monkeypatch, system_dir: Path) -> None:
    from menhir.infrastructure import path_containment

    marker = os.path.realpath(system_dir)

    def _is_system(parts: tuple[str, ...]) -> bool:
        joined = os.path.join(*parts) if parts else ""
        return joined == marker or joined.startswith(marker + os.sep)

    monkeypatch.setattr(path_containment, "is_host_system_path", _is_system)
    monkeypatch.setattr(project_scanner, "is_host_system_path", _is_system)


def test_root_under_a_system_directory_keeps_its_files(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "run" / "media" / "proj"
    root.mkdir(parents=True)
    (root / "app.py").write_text('"""Mounted drive project."""\n', encoding="utf-8")
    _pretend_system(monkeypatch, tmp_path / "run")
    scan = _scan_with_timeout(root)
    assert "app.py" in {f.rel_path for f in scan.files}


def test_system_file_under_a_non_system_root_is_still_refused(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "run").mkdir()
    (tmp_path / "run" / "environ.py").write_text('"""secret"""\n', encoding="utf-8")
    (tmp_path / "app.py").write_text('"""ok"""\n', encoding="utf-8")
    _pretend_system(monkeypatch, tmp_path / "run")
    scan = _scan_with_timeout(tmp_path)
    files = {f.rel_path for f in scan.files}
    assert "app.py" in files
    assert "run/environ.py" not in files


# --- FU4: a .gitignore outside the root still applies --------------------------------------


def test_shared_gitignore_outside_the_root_still_applies(repo: Path, tmp_path: Path) -> None:
    shared = tmp_path / "shared-gitignore"
    shared.write_text("local_secrets.py\n", encoding="utf-8")
    _symlink(shared, repo / ".gitignore")
    (repo / "local_secrets.py").write_text('"""API_KEY=abc"""\n', encoding="utf-8")
    scan = _scan_with_timeout(repo)
    assert "local_secrets.py" not in {f.rel_path for f in scan.files}


def test_gitignore_resolving_to_a_system_file_refuses_the_scan(
    repo: Path, tmp_path: Path, monkeypatch
) -> None:
    sysdir = tmp_path / "etc"
    sysdir.mkdir()
    (sysdir / "passwd").write_text("root:x:0:0\n", encoding="utf-8")
    _symlink(sysdir / "passwd", repo / ".gitignore")
    _pretend_system(monkeypatch, sysdir)
    with pytest.raises(project_scanner.ScanReadError):
        ProjectScanner().scan(repo, name="sys-gitignore")


# --- FU5: directory symlinks and Windows junctions out of the root are not walked -----------


def _dir_link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        _symlink(target, link)


def test_directory_link_out_of_the_root_is_not_walked(repo: Path, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside-tree"
    (outside_dir / "private_dir_name").mkdir(parents=True)
    (outside_dir / "private_dir_name" / "mod.py").write_text('"""x"""\n', encoding="utf-8")
    _dir_link(outside_dir, repo / "j")
    scan = _scan_with_timeout(repo)
    dirs = {d.rel_path for d in scan.directories}
    assert not any(d == "j" or d.startswith("j/") for d in dirs), dirs
    assert not any(f.rel_path.startswith("j/") for f in scan.files)


@posix_only
def test_device_symlink_is_not_read(repo: Path) -> None:
    _symlink(Path("/dev/zero"), repo / "zero.py")
    scan = _scan_with_timeout(repo)
    assert "zero.py" not in {f.rel_path for f in scan.files}


def test_corpus_entry_symlink_out_of_the_root_is_skipped(repo: Path, outside: Path) -> None:
    plans = repo / ".agent" / "plans"
    plans.mkdir(parents=True)
    (plans / "real.md").write_text("# Real plan\n", encoding="utf-8")
    _symlink(outside, plans / "leak.md")
    assert build_entry(repo, ".agent/plans/leak.md", repository="r") is None
    paths = {entry.path for entry in scan_corpus(repo, repository="r")}
    assert ".agent/plans/real.md" in paths
    assert ".agent/plans/leak.md" not in paths


def test_corpus_index_symlink_out_of_the_root_is_ignored(repo: Path, tmp_path: Path) -> None:
    plans = repo / ".agent" / "plans"
    plans.mkdir(parents=True)
    foreign = tmp_path / "foreign.md"
    foreign.write_text("see secret-plan.md\n", encoding="utf-8")
    _symlink(foreign, plans / "README.md")
    assert read_index_links(repo, ".agent/plans") == frozenset()


def test_containment_predicate(repo: Path, outside: Path) -> None:
    assert is_contained_regular_file(repo / "app.py", repo)
    assert not is_contained_regular_file(repo, repo)  # a directory
    assert not is_contained_regular_file(outside, repo)
    assert not is_contained_regular_file(repo / "missing.py", repo)
