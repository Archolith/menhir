"""Put a validated snapshot on disk, or leave nothing behind.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

The three halves of P3 are deliberately separate. `archive_plan` decides WHAT may be written and
touches no disk. `extraction_lease` decides WHO may write it and for how long. This decides
whether the bytes actually land, and it is the only one of the three with consequences outside the
process, so every rule here is enforced again rather than assumed from upstream.

**The root is a boundary, checked at the last line before `open()`.** `plan_archive` already
refuses traversal, and this re-resolves every joined path anyway. The duplication is the point: a
containment check that lives only upstream is one refactor, one second caller, or one plan built
elsewhere away from not existing.

**Bytes are counted, never believed.** `declared_size` comes from the archive's central directory,
which is metadata an attacker wrote. It is useful for refusing early and worthless as a promise, so
the limit is enforced against what has actually been written.

**The lease is re-checked as work proceeds.** An extraction can outrun its own authority; valid at
the start says nothing about the entry being written a minute later.

**A failure leaves no root.** A half-written extraction that survives is indistinguishable from a
complete one to whatever finds it next, which is how a partial snapshot becomes a believed one.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from menhir.snapshot.archive_plan import PlannedEntry, _as_buffer
from menhir.snapshot.extraction_lease import ExtractionLease, LeaseStore
from menhir.snapshot.protocol import (
    ERR_LIMIT_FILE_BYTES,
    ERR_LIMIT_TOTAL_BYTES,
    PROVISIONAL_LIMITS,
    SnapshotLimits,
)

__all__ = [
    "ERR_ENTRY_MISSING",
    "ERR_ROOT_ESCAPE",
    "ERR_ROOT_EXISTS",
    "ExtractionError",
    "ExtractionResult",
    "materialize",
]

ERR_ROOT_ESCAPE = "snapshot.extract.root_escape"
ERR_ROOT_EXISTS = "snapshot.extract.root_exists"
ERR_ENTRY_MISSING = "snapshot.extract.entry_missing"

#: Copy granularity. Small enough that the byte limit is enforced promptly on a lying header --
#: a 1 MiB buffer would let a bomb overshoot by up to 1 MiB before anyone noticed.
_COPY_CHUNK = 64 * 1024

#: How often the lease is re-checked. Per entry is too chatty for a 20,000-file bundle and per
#: bundle is not a check at all; every 64 entries bounds the window in which a superseded worker
#: can still be writing.
_LEASE_CHECK_EVERY = 64


class ExtractionError(RuntimeError):
    """A refusal carrying a stable code. Never carries a path: those are attacker-chosen."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExtractionResult:
    root: Path
    file_count: int
    total_bytes: int
    #: path -> sha256 of the bytes actually written. The scan and any parity check read this
    #: rather than re-hashing the tree, which would open a window for it to change underneath.
    digests: dict[str, str]


def _safe_target(root: Path, relative: str) -> Path:
    """Join and then PROVE the result is inside the root.

    `Path.resolve()` after joining, compared with `relative_to`, is what catches an escape that
    survived normalization -- including one arriving through a forged plan. Symlinked parents
    resolve here too, which is why the comparison is on resolved paths rather than on the string.
    """
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ExtractionError(
            ERR_ROOT_ESCAPE, "an entry resolved to a path outside the extraction root"
        ) from exc
    return candidate


def materialize(
    source: Path | bytes,
    plan: Sequence[PlannedEntry],
    root: Path,
    *,
    lease: ExtractionLease,
    leases: LeaseStore,
    limits: SnapshotLimits = PROVISIONAL_LIMITS,
    on_entry: Callable[[int], None] | None = None,
) -> ExtractionResult:
    """Write `plan` into `root`, or raise having left nothing behind.

    `on_entry` is a test seam, called with the number of entries written so far. It exists because
    the interesting lease failures happen PARTWAY through, and a test that can only arrange them
    before or after the call is not testing the window that matters.
    """
    root = Path(root)
    if root.exists():
        # Refused, not reused and not deleted. A root left by a crash holds bytes nobody verified,
        # attributed to nobody; adopting it merges two extractions, and deleting it here would
        # destroy evidence this function does not own. Reclaiming it is the sweep's job.
        raise ExtractionError(ERR_ROOT_EXISTS, "the extraction root already exists")

    leases.check(lease)

    written = 0
    total = 0
    digests: dict[str, str] = {}
    try:
        root.mkdir(parents=True)
        archive = zipfile.ZipFile(
            source if isinstance(source, Path) else _as_buffer(source)
        )
        with archive:
            names = set(archive.namelist())
            for entry in plan:
                if entry.raw_name not in names:
                    raise ExtractionError(
                        ERR_ENTRY_MISSING,
                        "the plan names an entry the archive does not contain",
                    )

                target = _safe_target(root, entry.path)
                target.parent.mkdir(parents=True, exist_ok=True)

                entry_bytes = 0
                digest = hashlib.sha256()
                with archive.open(entry.raw_name) as reader, target.open("wb") as sink:
                    while True:
                        block = reader.read(_COPY_CHUNK)
                        if not block:
                            break
                        entry_bytes += len(block)
                        total += len(block)
                        # Counted, not believed. `declared_size` is the attacker's number; these
                        # are the bytes that have actually reached the disk.
                        if entry_bytes > limits.max_file_bytes:
                            raise ExtractionError(
                                ERR_LIMIT_FILE_BYTES,
                                "an entry exceeded the per-file limit",
                            )
                        if total > limits.max_total_bytes:
                            raise ExtractionError(
                                ERR_LIMIT_TOTAL_BYTES,
                                "the extraction exceeded the total limit",
                            )
                        digest.update(block)
                        sink.write(block)

                digests[entry.path] = digest.hexdigest()
                written += 1

                if on_entry is not None:
                    on_entry(written)
                if written % _LEASE_CHECK_EVERY == 0 or on_entry is not None:
                    # The `on_entry` clause keeps the test seam honest: without it a test could
                    # arrange a lease failure that the production cadence would not notice for
                    # another 63 entries, and the test would be proving nothing.
                    leases.check(lease)

        leases.check(lease)
    except BaseException:
        # Every failure path, including a lease refusal and a KeyboardInterrupt. A half-written
        # root that survives is indistinguishable from a complete one to whatever finds it next.
        shutil.rmtree(root, ignore_errors=True)
        raise

    return ExtractionResult(
        root=root, file_count=written, total_bytes=total, digests=digests
    )
