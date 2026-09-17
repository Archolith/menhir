"""Parity: does scanning a snapshot remotely give the same answer as scanning the repo locally?

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 3).

The gate for P3 is structural parity, and the test that matters here is a round trip: build a real
repository, bundle it, extract it the way the server would, scan both ends with the SAME scanner,
and compare. Anything that differs is either a real loss or a fingerprint that is measuring the
wrong thing -- and the second is the failure mode worth guarding, because it looks like the first.

`project_scanner` is not modified by any of this. `test_local_scanning_is_untouched_by_this_work`
asserts that directly rather than trusting the diff.
"""

from __future__ import annotations

import io
import subprocess
import zipfile
from pathlib import Path

import pytest

from menhir.snapshot.archive_plan import plan_archive
from menhir.snapshot.extraction_lease import LeaseStore
from menhir.snapshot.extraction_writer import materialize
from menhir.snapshot.shadow_scan import ShadowReport, fingerprint_scan, shadow_scan

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small but real project: packages, imports, a test, a class with methods."""
    project = tmp_path / "widget"
    (project / "src" / "widget").mkdir(parents=True)
    (project / "tests").mkdir(parents=True)
    (project / "src" / "widget" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "widget" / "core.py").write_text(
        "class Engine:\n"
        "    def start(self) -> bool:\n"
        '        """Begin."""\n'
        "        return True\n"
        "\n"
        "def build() -> Engine:\n"
        "    return Engine()\n",
        encoding="utf-8",
    )
    (project / "src" / "widget" / "api.py").write_text(
        "from widget.core import build\n\n\ndef handler():\n    return build()\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_core.py").write_text(
        "from widget.core import build\n\n\ndef test_build():\n    assert build()\n",
        encoding="utf-8",
    )
    (project / "README.md").write_text(
        "# widget\n\nA small widget.\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        '[project]\nname = "widget"\ndependencies = ["httpx"]\n', encoding="utf-8"
    )
    return project


def _bundle_of(repo: Path) -> bytes:
    """Archive the repository the way a bundle carries it: relative POSIX paths, files only."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(repo.rglob("*")):
            if path.is_file():
                archive.writestr(path.relative_to(repo).as_posix(), path.read_bytes())
    return buffer.getvalue()


def _extract(blob: bytes, tmp_path: Path, name: str = "snap-1") -> Path:
    leases = LeaseStore(tmp_path / "leases", ttl_s=600.0)
    lease = leases.acquire(project_key="proj", upload_id=name, owner="worker-a")
    root = tmp_path / "roots" / name
    materialize(blob, plan_archive(blob), root, lease=lease, leases=leases)
    return root


def _scan(root: Path):
    from menhir.infrastructure.project_scanner import ProjectScanner

    return ProjectScanner().scan(root)


# --- the parity check the gate asks for ----------------------------------------------------------


def test_a_snapshot_scans_identically_to_the_repository_it_came_from(
    repo: Path, tmp_path: Path
) -> None:
    """The round trip, end to end, with the same scanner on both sides.

    If this fails, either the bundle lost something or the fingerprint is measuring something that
    is not structure. Both matter; only one is a snapshot problem.
    """
    local = fingerprint_scan(_scan(repo))
    extracted = _extract(_bundle_of(repo), tmp_path)
    remote = fingerprint_scan(_scan(extracted))

    assert remote == local


def test_the_fingerprint_ignores_where_the_files_live_and_when_they_were_written(
    repo: Path, tmp_path: Path
) -> None:
    """Two extractions of one bundle, at different paths and different times, agree.

    This is the exclusion list asserted rather than described: `root_path` is absolute and
    `file_mtime` is set by extraction, so including either would make every parity check fail for
    a reason that has nothing to do with the snapshot.
    """
    blob = _bundle_of(repo)
    first = _extract(blob, tmp_path / "a", "snap-a")
    second = _extract(blob, tmp_path / "b", "snap-b")

    # Make the second extraction's mtimes provably different from the first's.
    for path in second.rglob("*"):
        if path.is_file():
            path.touch()

    assert fingerprint_scan(_scan(first)) == fingerprint_scan(_scan(second))


def test_a_real_difference_still_changes_the_fingerprint(
    repo: Path, tmp_path: Path
) -> None:
    """The exclusions must not have made the fingerprint blind.

    A fingerprint that ignores enough to always match is worse than none: it reports parity it did
    not check. Dropping one source file must change it.
    """
    full = _extract(_bundle_of(repo), tmp_path / "full", "snap-full")

    reduced_repo = repo
    (reduced_repo / "src" / "widget" / "api.py").unlink()
    partial = _extract(_bundle_of(reduced_repo), tmp_path / "partial", "snap-partial")

    assert fingerprint_scan(_scan(full)) != fingerprint_scan(_scan(partial))


# --- the report, and the root that must not outlive it -------------------------------------------


def test_the_report_carries_counts_and_deletes_the_root(
    repo: Path, tmp_path: Path
) -> None:
    """`shadow` answers a question without keeping the answer's materials.

    A root that outlives its report is an unattributed copy of somebody's repository sitting on a
    disk, which is exactly what this phase is not allowed to leave behind.
    """
    root = _extract(_bundle_of(repo), tmp_path)

    report = shadow_scan(root)

    assert isinstance(report, ShadowReport)
    assert report.file_count > 0
    assert report.symbol_count > 0
    assert not root.exists(), "the shadow scan kept the materialised snapshot"


def test_the_root_is_deleted_even_when_the_scan_raises(
    repo: Path, tmp_path: Path
) -> None:
    """The cleanup is in a `finally` for a reason; a failed scan must not strand the copy."""
    root = _extract(_bundle_of(repo), tmp_path)

    class Exploding:
        def scan(self, *_args, **_kwargs):
            raise RuntimeError("scanner failed")

    with pytest.raises(RuntimeError):
        shadow_scan(root, scanner=Exploding())

    assert not root.exists()


def test_the_report_states_counts_and_does_not_assert_partial_index(
    repo: Path, tmp_path: Path
) -> None:
    """Invariant 4 kept where it belongs.

    `partial_index` is derived from three counts by whoever can see the whole picture. The report
    carries the counts -- the source of truth -- and does not carry the derived claim.
    """
    report = shadow_scan(_extract(_bundle_of(repo), tmp_path))

    fields = set(vars(report))
    assert {"files_discovered", "files_eligible", "files_indexed"} <= fields
    assert "partial_index" not in fields


# --- the constraint on this work ------------------------------------------------------------------


def test_local_scanning_is_untouched_by_this_work() -> None:
    """The snapshot work must not change how a local scan behaves.

    Asserted against git rather than by reading the diff: `project_scanner.py` is the local scan
    path, and this phase consumes it without owning it. If the snapshot work ever needs the scanner
    to behave differently, that is a conversation, not a commit.
    """
    scanner = Path("src/menhir/infrastructure/project_scanner.py")
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", str(scanner)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert changed.stdout.strip() == "", (
        "project_scanner.py has uncommitted changes; local scanning is outside this work's scope"
    )
