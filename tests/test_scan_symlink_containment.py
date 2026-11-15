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


@posix_only
def test_fifo_readme_does_not_hang_the_scan(repo: Path) -> None:
    os.mkfifo(repo / "README.md")
    _scan_with_timeout(repo)


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
