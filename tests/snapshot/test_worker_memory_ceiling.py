"""Does the extraction child's memory ceiling actually fire?

Until now it was a line of code and a claim: `run_extraction` installs an `RLIMIT_AS` ceiling via
`preexec_fn`, the module docstring says so, and nothing ever proved a child hits it. A limit that
has never stopped anything is indistinguishable from a limit that does not work.

**These SKIP on Windows rather than pretending to pass.** `RLIMIT_AS` is POSIX-only, which the
worker states plainly; a test that quietly passed on a platform where the mechanism does not exist
would be worse than no test, because the suite would report the ceiling as proven everywhere.
Menhir deploys on Linux, so CI is where this actually runs.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from menhir.snapshot.extract_worker import ERR_WORKER_CRASHED, run_extraction
from menhir.snapshot.extraction_lease import LeaseStore
from menhir.snapshot.extraction_writer import ExtractionError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.timeout(120),
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="RLIMIT_AS is POSIX-only; the ceiling does not exist on Windows and the worker "
        "says so. Skipped rather than passed, so the suite never reports it as proven here.",
    ),
]

_TTL = 600.0


@pytest.fixture
def leases(tmp_path: Path) -> LeaseStore:
    return LeaseStore(tmp_path / "leases", ttl_s=_TTL)


def _archive(tmp_path: Path) -> Path:
    target = tmp_path / "bundle.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("src/main.py", b"print('hi')\n")
    target.write_bytes(buffer.getvalue())
    return target


def _child_that(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def test_the_memory_ceiling_actually_stops_a_child_that_allocates_past_it(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """A child that asks for more than the ceiling must die, and be reported as a crash.

    The stand-in child allocates well past a deliberately small ceiling. If `preexec_fn` never ran,
    or `RLIMIT_AS` was not applied, the allocation succeeds and this test fails -- which is the
    whole point, because that is the state the code was in for as long as nobody checked.
    """
    lease = leases.acquire(project_key="proj", upload_id="snap-1", owner="worker-a")

    with pytest.raises(ExtractionError) as excinfo:
        run_extraction(
            _archive(tmp_path),
            tmp_path / "roots" / "snap-1",
            lease=lease,
            lease_root=leases.root,
            lease_ttl_s=_TTL,
            memory_limit_bytes=64 * 1024 * 1024,
            # Asks for ~512 MiB against a 64 MiB ceiling. `bytearray` touches the pages, so this
            # is a real allocation rather than a lazy reservation the kernel might permit.
            child_argv=_child_that("x = bytearray(512 * 1024 * 1024); print(len(x))"),
        )

    assert excinfo.value.code == ERR_WORKER_CRASHED


def test_a_child_within_the_ceiling_is_untouched(tmp_path: Path, leases: LeaseStore) -> None:
    """The negative control: the ceiling must not be killing everything.

    A test that only asserts the big allocation dies would also pass if the child could not start
    at all -- if `preexec_fn` raised, or the interpreter itself could not fit under the limit. This
    proves the ceiling is discriminating rather than simply fatal.
    """
    lease = leases.acquire(project_key="proj", upload_id="snap-2", owner="worker-a")

    outcome = run_extraction(
        _archive(tmp_path),
        tmp_path / "roots" / "snap-2",
        lease=lease,
        lease_root=leases.root,
        lease_ttl_s=_TTL,
        memory_limit_bytes=512 * 1024 * 1024,
    )

    assert outcome.file_count == 1
