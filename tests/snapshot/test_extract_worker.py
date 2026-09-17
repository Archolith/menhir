"""Counterexamples for the subprocess boundary.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 1).

The writer already refuses hostile bundles and cleans up after itself. This exists for the failures
the writer structurally cannot handle, because they do not raise:

1. **A hung parser.** No exception, no cleanup, no end. The parent's deadline is the only thing
   that stops it.
2. **A killed child.** SIGKILL runs no `except`, so the writer's own `rmtree` never happens and the
   half-written root survives. The parent must remove it. This is the case that is easy to miss
   precisely because the in-process cleanup is correct and already tested.
3. **A crash.** A segfault or an OOM kill produces no usable reply, and the caller still needs a
   stable code rather than a traceback from a process that was parsing attacker-chosen bytes.

The hanging and dying children are injected as a different command rather than hooked into the
worker with a test-only flag. A production module with a test branch in it is a production module
that can take that branch in production.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

from menhir.snapshot.extract_worker import (
    ERR_WORKER_CRASHED,
    ERR_WORKER_TIMEOUT,
    ERR_WORKER_UNREADABLE,
    run_extraction,
)
from menhir.snapshot.extraction_lease import LeaseStore
from menhir.snapshot.extraction_writer import ExtractionError

pytestmark = [pytest.mark.unit, pytest.mark.timeout(120)]

_TTL = 600.0


@pytest.fixture
def leases(tmp_path: Path) -> LeaseStore:
    return LeaseStore(tmp_path / "leases", ttl_s=_TTL)


def _archive(tmp_path: Path, entries: list[tuple[str, bytes]]) -> Path:
    target = tmp_path / "bundle.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    target.write_bytes(buffer.getvalue())
    return target


_SAMPLE = [("src/main.py", b"print('hi')\n"), ("README.md", b"# sample\n")]


def _run(tmp_path: Path, leases: LeaseStore, **kwargs):
    lease = leases.acquire(project_key="proj", upload_id="snap-1", owner="worker-a")
    return run_extraction(
        _archive(tmp_path, _SAMPLE),
        tmp_path / "roots" / "snap-1",
        lease=lease,
        lease_root=leases.root,
        lease_ttl_s=_TTL,
        **kwargs,
    )


def _child_that(script: str) -> list[str]:
    """A stand-in child process, so no test-only branch exists in the worker itself."""
    return [sys.executable, "-c", script]


# --- the real thing, end to end through a real process -------------------------------------------


def test_a_real_child_process_extracts_the_bundle(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """Not a mock: an actual interpreter, an actual archive, an actual root.

    The point of decision 1 is that extraction happens somewhere else, so a test that never starts
    a process would be testing the opposite of the thing chosen.
    """
    outcome = _run(tmp_path, leases)

    root = tmp_path / "roots" / "snap-1"
    assert (root / "src" / "main.py").read_bytes() == b"print('hi')\n"
    assert outcome.file_count == 2
    assert outcome.digests["README.md"]


# --- 1 and 2. the hang, and the cleanup a killed child cannot do ---------------------------------


def test_a_hung_child_is_killed_at_its_deadline(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """A parser that never returns is the failure a thread could not contain."""
    with pytest.raises(ExtractionError) as excinfo:
        _run(
            tmp_path,
            leases,
            timeout_s=1.0,
            child_argv=_child_that("import time; time.sleep(60)"),
        )

    assert excinfo.value.code == ERR_WORKER_TIMEOUT


def test_the_parent_removes_a_root_the_killed_child_could_not_clean(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """THE counterexample for this module.

    The child writes into the root and then hangs. SIGKILL raises nothing, so the writer's `except`
    never runs and its own cleanup never happens. If the parent does not remove the root, a
    half-written extraction survives -- and to anything that finds it later it is indistinguishable
    from a complete one.
    """
    root = tmp_path / "roots" / "snap-1"
    script = (
        "import pathlib, time, sys;"
        f"p = pathlib.Path(r'{root}');"
        "p.mkdir(parents=True, exist_ok=True);"
        "(p / 'half-written.py').write_bytes(b'partial');"
        "sys.stderr.write('wrote');"
        "time.sleep(60)"
    )

    with pytest.raises(ExtractionError) as excinfo:
        _run(tmp_path, leases, timeout_s=2.0, child_argv=_child_that(script))

    assert excinfo.value.code == ERR_WORKER_TIMEOUT
    assert not root.exists(), (
        "the parent left behind a root its killed child could not clean"
    )


# --- 3. crashes and nonsense ---------------------------------------------------------------------


def test_a_child_that_dies_is_reported_as_a_crash_not_a_traceback(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """A segfault or OOM kill produces no usable reply; the caller still needs a stable code."""
    with pytest.raises(ExtractionError) as excinfo:
        _run(tmp_path, leases, child_argv=_child_that("import sys; sys.exit(3)"))

    assert excinfo.value.code == ERR_WORKER_CRASHED


def test_a_crashed_child_leaves_no_root(tmp_path: Path, leases: LeaseStore) -> None:
    root = tmp_path / "roots" / "snap-1"
    script = (
        "import pathlib, sys;"
        f"p = pathlib.Path(r'{root}');"
        "p.mkdir(parents=True, exist_ok=True);"
        "(p / 'half.py').write_bytes(b'partial');"
        "sys.exit(9)"
    )

    with pytest.raises(ExtractionError):
        _run(tmp_path, leases, child_argv=_child_that(script))

    assert not root.exists()


def test_an_unreadable_reply_is_not_mistaken_for_success(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """Exit code 0 is not a result.

    A child that printed a warning, or anything else non-JSON, must not be read as an extraction
    that worked -- that is how an empty root becomes a believed snapshot.
    """
    with pytest.raises(ExtractionError) as excinfo:
        _run(tmp_path, leases, child_argv=_child_that("print('not json')"))

    assert excinfo.value.code == ERR_WORKER_UNREADABLE


def test_a_childs_refusal_crosses_as_a_code_and_its_message_does_not(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """The child refuses in an orderly way; only the code travels.

    A message from a process that was parsing attacker-chosen bytes is both useless to the caller
    and a place for those bytes to surface.
    """
    script = (
        "import json, sys;"
        "sys.stdin.read();"
        "sys.stdout.write(json.dumps({'ok': False, 'code': 'snapshot.path.traversal',"
        " 'message': 'SECRET-PATH-../../etc/passwd'}))"
    )

    with pytest.raises(ExtractionError) as excinfo:
        _run(tmp_path, leases, child_argv=_child_that(script))

    assert excinfo.value.code == "snapshot.path.traversal"
    assert "SECRET-PATH" not in str(excinfo.value)
    assert "etc/passwd" not in str(excinfo.value)
