"""Counterexamples for the extraction writer, written before the writer.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

The validator decides WHAT may be written and the lease decides WHO may write it. This is the half
that actually puts attacker-supplied bytes on a disk, so the failures here are the ones with
consequences outside the process.

Four, in the order they hurt:

1. **A path that escapes the root**, even though the validator already normalized it. The check is
   deliberately duplicated: this is the last line before `open()`, and a defence that exists only
   upstream is one refactor away from not existing.
2. **A header that lies.** `ZipInfo.file_size` is attacker-chosen. The writer must stop at the
   limit by COUNTING, not by believing.
3. **A lease that expires mid-write.** Valid when the extraction started proves nothing about the
   entry being written a minute later.
4. **A failure must leave no root.** A half-written extraction that survives is indistinguishable
   from a complete one to anything that finds it later.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from menhir.snapshot.archive_plan import PlannedEntry, plan_archive
from menhir.snapshot.extraction_lease import LeaseError, LeaseStore
from menhir.snapshot.extraction_writer import (
    ERR_ROOT_ESCAPE,
    ERR_ROOT_EXISTS,
    ExtractionError,
    materialize,
)
from menhir.snapshot.protocol import PROVISIONAL_LIMITS

pytestmark = pytest.mark.unit

_TTL = 60.0


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def leases(tmp_path: Path, clock: FakeClock) -> LeaseStore:
    return LeaseStore(tmp_path / "leases", ttl_s=_TTL, clock=clock)


def _archive(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


_SAMPLE = [("src/main.py", b"print('hi')\n"), ("README.md", b"# sample\n")]


def _lease(leases: LeaseStore, upload: str = "snap-1"):
    return leases.acquire(project_key="proj", upload_id=upload, owner="worker-a")


# --- the ordinary path, so the guards are not passing by refusing everything ---------------------


def test_a_clean_bundle_materializes_its_files(
    tmp_path: Path, leases: LeaseStore
) -> None:
    blob = _archive(_SAMPLE)
    root = tmp_path / "roots" / "snap-1"

    result = materialize(
        blob, plan_archive(blob), root, lease=_lease(leases), leases=leases
    )

    assert (root / "src" / "main.py").read_bytes() == b"print('hi')\n"
    assert (root / "README.md").read_bytes() == b"# sample\n"
    assert result.file_count == 2
    assert result.total_bytes == len(b"print('hi')\n") + len(b"# sample\n")


# --- 1. the root is a boundary, not a suggestion -------------------------------------------------


def test_a_planned_entry_that_escapes_the_root_is_refused(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """Defence in depth at the last line before `open()`.

    `plan_archive` already refuses `../`, so this forges a plan the validator would never produce
    -- which is exactly the situation a refactor, a second caller, or a plan built elsewhere
    creates. A containment check that exists only upstream is one change away from not existing.
    """
    blob = _archive(_SAMPLE)
    forged = [
        PlannedEntry(path="../escaped.py", raw_name="src/main.py", declared_size=12)
    ]
    root = tmp_path / "roots" / "snap-1"

    with pytest.raises(ExtractionError) as excinfo:
        materialize(blob, forged, root, lease=_lease(leases), leases=leases)

    assert excinfo.value.code == ERR_ROOT_ESCAPE
    assert not (tmp_path / "roots" / "escaped.py").exists()
    assert not (tmp_path / "escaped.py").exists()


def test_an_existing_root_is_never_adopted(tmp_path: Path, leases: LeaseStore) -> None:
    """Invariant 11 at the filesystem: a directory's existence is not state.

    A root left by a crashed extraction holds bytes nobody verified, attributed to nobody. Writing
    into it would merge two extractions; reusing it would adopt them. It is refused, and reclaiming
    it is the sweep's job, not this function's.
    """
    blob = _archive(_SAMPLE)
    root = tmp_path / "roots" / "snap-1"
    root.mkdir(parents=True)
    (root / "leftover.py").write_bytes(b"from a crash\n")

    with pytest.raises(ExtractionError) as excinfo:
        materialize(blob, plan_archive(blob), root, lease=_lease(leases), leases=leases)

    assert excinfo.value.code == ERR_ROOT_EXISTS
    assert (root / "leftover.py").exists(), (
        "the refusal destroyed evidence it did not own"
    )


# --- 2. the header is a claim, the count is the fact ---------------------------------------------


def test_bytes_are_counted_while_writing_not_believed_from_the_header(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """A lying header must not buy more disk than the limit allows.

    The plan is forged to declare one byte for an entry that expands to far more, which is what an
    attacker controls: the central directory is metadata, and metadata is not evidence. The writer
    must stop when the bytes it has actually written cross the limit.
    """
    payload = b"\0" * (256 * 1024)
    blob = _archive([("big.bin", payload)])
    # The validator would refuse this on ratio; the forged plan is how the bytes get past it.
    forged = [PlannedEntry(path="big.bin", raw_name="big.bin", declared_size=1)]
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=1024)
    root = tmp_path / "roots" / "snap-1"

    with pytest.raises(ExtractionError) as excinfo:
        materialize(
            blob, forged, root, lease=_lease(leases), leases=leases, limits=limits
        )

    assert excinfo.value.code.startswith("snapshot.limit.")
    assert not root.exists(), "a refused extraction left its partial write behind"


# --- 3. the lease can die mid-write --------------------------------------------------------------


def test_an_extraction_that_outruns_its_lease_stops(
    tmp_path: Path, leases: LeaseStore, clock: FakeClock
) -> None:
    """Valid at the start says nothing about the entry being written a minute later.

    The clock is advanced past the TTL by a hook that fires between entries, so the lease dies
    exactly where a real slow extraction would lose it -- partway through, with bytes already on
    disk.
    """
    blob = _archive([(f"f{i}.txt", b"x" * 64) for i in range(10)])
    root = tmp_path / "roots" / "snap-1"
    lease = _lease(leases)

    def expire_after_first(written: int) -> None:
        if written == 1:
            clock.advance(_TTL + 1)

    with pytest.raises(LeaseError) as excinfo:
        materialize(
            blob,
            plan_archive(blob),
            root,
            lease=lease,
            leases=leases,
            on_entry=expire_after_first,
        )

    assert excinfo.value.code == "snapshot.lease.expired"
    assert not root.exists(), "an extraction that lost its lease left its root behind"


def test_a_superseded_extraction_stops_even_with_time_left(
    tmp_path: Path, leases: LeaseStore, clock: FakeClock
) -> None:
    """The store is the authority. Another worker claiming the project ends this extraction."""
    blob = _archive([(f"f{i}.txt", b"x" * 64) for i in range(10)])
    root = tmp_path / "roots" / "snap-1"
    lease = _lease(leases)

    def steal_after_first(written: int) -> None:
        if written == 1:
            clock.advance(_TTL + 1)
            leases.acquire(project_key="proj", upload_id="snap-2", owner="worker-b")
            clock.now = 1000.0 + 1  # rewind: from inside, our lease still looks live

    with pytest.raises(LeaseError) as excinfo:
        materialize(
            blob,
            plan_archive(blob),
            root,
            lease=lease,
            leases=leases,
            on_entry=steal_after_first,
        )

    assert excinfo.value.code == "snapshot.lease.superseded"
    assert not root.exists()


# --- 4. nothing survives a failure ---------------------------------------------------------------


def test_a_failure_partway_through_leaves_no_root(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """A half-written root is indistinguishable from a complete one to whatever finds it later.

    The entry is forged to name something the archive does not contain, so the failure happens
    after several files are already on disk -- the case where cleanup is easiest to get wrong.
    """
    blob = _archive(_SAMPLE)
    forged = [
        PlannedEntry(path="src/main.py", raw_name="src/main.py", declared_size=12),
        PlannedEntry(
            path="ghost.py", raw_name="not-in-the-archive.py", declared_size=1
        ),
    ]
    root = tmp_path / "roots" / "snap-1"

    with pytest.raises(ExtractionError):
        materialize(blob, forged, root, lease=_lease(leases), leases=leases)

    assert not root.exists(), "a failed extraction left a partially written root"
