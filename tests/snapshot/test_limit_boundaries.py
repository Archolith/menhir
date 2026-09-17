"""Limits tested AT their boundary, not far past it.

Mutation testing found these. Every existing limit test uses a value wildly over the line -- 5,000
files against a limit of 100, an 8 MiB entry against 1 KiB -- so `>` and `>=` behave identically
and an off-by-one in any limit survives untouched. Twenty mutations survived the suite, and most of
them were this.

Each limit gets two cases: **exactly at** the limit, which must be ACCEPTED, and **one past**, which
must be REFUSED. That pair is what pins the comparison operator; either case alone does not.

The pairs cover both places a limit is enforced, because they are enforced twice on purpose:
`plan_archive` refuses early from the archive's own metadata, and `materialize` refuses again from
bytes it has actually written. A limit that held in one and not the other would be a real hole.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from menhir.snapshot.archive_plan import PlannedEntry, plan_archive
from menhir.snapshot.extraction_lease import LeaseStore
from menhir.snapshot.extraction_writer import ExtractionError, materialize
from menhir.snapshot.protocol import (
    ERR_LIMIT_FILE_BYTES,
    ERR_LIMIT_FILE_COUNT,
    ERR_LIMIT_TOTAL_BYTES,
    PROVISIONAL_LIMITS,
    BundleFormatError,
)

pytestmark = pytest.mark.unit


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    # Stored, not deflated: these tests are about SIZE boundaries, and compression would put the
    # ratio guard in front of the limit under test.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _files(count: int) -> list[tuple[str, bytes]]:
    return [(f"f{i:04d}.txt", b"x") for i in range(count)]


# --- file count ----------------------------------------------------------------------------------


def test_exactly_the_file_count_limit_is_accepted() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_count=10)

    planned = plan_archive(_zip(_files(10)), limits)

    assert len(planned) == 10


def test_one_file_over_the_count_limit_is_refused() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_count=10)

    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(_zip(_files(11)), limits)

    assert excinfo.value.code == ERR_LIMIT_FILE_COUNT


# --- per-file bytes, in the validator -------------------------------------------------------------


def test_an_entry_of_exactly_the_per_file_limit_is_accepted() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=1024)

    planned = plan_archive(_zip([("big.bin", b"x" * 1024)]), limits)

    assert planned[0].declared_size == 1024


def test_an_entry_one_byte_over_the_per_file_limit_is_refused() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=1024)

    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(_zip([("big.bin", b"x" * 1025)]), limits)

    assert excinfo.value.code == ERR_LIMIT_FILE_BYTES


# --- total bytes, in the validator ----------------------------------------------------------------


def test_a_total_of_exactly_the_limit_is_accepted() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=2048, max_file_bytes=2048)

    planned = plan_archive(_zip([("a.bin", b"x" * 1024), ("b.bin", b"y" * 1024)]), limits)

    assert len(planned) == 2


def test_a_total_one_byte_over_the_limit_is_refused() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=2048, max_file_bytes=2048)

    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(_zip([("a.bin", b"x" * 1024), ("b.bin", b"y" * 1025)]), limits)

    assert excinfo.value.code == ERR_LIMIT_TOTAL_BYTES


# --- the same limits again, in the writer ---------------------------------------------------------
#
# Enforced twice on purpose: the validator reads the archive's metadata, the writer counts bytes it
# has actually written. A forged plan is how these tests reach the writer's copy, because the
# validator would otherwise refuse first -- which is the layering working.


@pytest.fixture
def leases(tmp_path: Path) -> LeaseStore:
    return LeaseStore(tmp_path / "leases", ttl_s=600.0)


def _lease(leases: LeaseStore):
    return leases.acquire(project_key="proj", upload_id="snap-1", owner="worker-a")


def test_the_writer_accepts_an_entry_of_exactly_the_per_file_limit(
    tmp_path: Path, leases: LeaseStore
) -> None:
    blob = _zip([("big.bin", b"x" * 1024)])
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=1024)
    root = tmp_path / "roots" / "at-limit"

    result = materialize(blob, plan_archive(blob, limits), root, lease=_lease(leases), leases=leases, limits=limits)

    assert result.total_bytes == 1024


def test_the_writer_refuses_an_entry_one_byte_over_the_per_file_limit(
    tmp_path: Path, leases: LeaseStore
) -> None:
    """Forged plan: the validator would refuse first, so this is how the writer's guard is reached."""
    blob = _zip([("big.bin", b"x" * 1025)])
    forged = [PlannedEntry(path="big.bin", raw_name="big.bin", declared_size=1)]
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=1024)
    root = tmp_path / "roots" / "over-limit"

    with pytest.raises(ExtractionError) as excinfo:
        materialize(blob, forged, root, lease=_lease(leases), leases=leases, limits=limits)

    assert excinfo.value.code == ERR_LIMIT_FILE_BYTES
    assert not root.exists()


def test_the_writer_accepts_a_total_of_exactly_the_limit(
    tmp_path: Path, leases: LeaseStore
) -> None:
    blob = _zip([("a.bin", b"x" * 512), ("b.bin", b"y" * 512)])
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=1024, max_file_bytes=1024)
    root = tmp_path / "roots" / "total-at-limit"

    result = materialize(blob, plan_archive(blob, limits), root, lease=_lease(leases), leases=leases, limits=limits)

    assert result.total_bytes == 1024


def test_the_writer_refuses_a_total_one_byte_over_the_limit(
    tmp_path: Path, leases: LeaseStore
) -> None:
    blob = _zip([("a.bin", b"x" * 512), ("b.bin", b"y" * 513)])
    forged = [
        PlannedEntry(path="a.bin", raw_name="a.bin", declared_size=1),
        PlannedEntry(path="b.bin", raw_name="b.bin", declared_size=1),
    ]
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=1024, max_file_bytes=1024)
    root = tmp_path / "roots" / "total-over-limit"

    with pytest.raises(ExtractionError) as excinfo:
        materialize(blob, forged, root, lease=_lease(leases), leases=leases, limits=limits)

    assert excinfo.value.code == ERR_LIMIT_TOTAL_BYTES
    assert not root.exists()
