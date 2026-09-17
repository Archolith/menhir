"""Decide what an uploaded archive is allowed to become, without writing any of it.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

This is the DECISION half of extraction, deliberately split from the writing half. Everything here
is a pure function of the archive's central directory: it opens nothing, creates nothing, and can
refuse a hostile bundle before any managed root exists. The writing half needs a root, a lease and
a bounded worker, and those depend on design questions the owner has not answered yet -- so the
part that can be built and adversarially tested today is built today.

**This module parses attacker-chosen bytes.** That is new: until now nothing in `src/` could even
import `zipfile`, and P2A's gate is enforced by a test that fails if its tool module does. The
containment is that this module only ever reads the central directory and returns a plan; a caller
that never acts on the plan has still not extracted anything.

**Ambiguity is refused, never resolved.** Duplicate entries and case collisions are rejected rather
than reconciled, because a bundle whose meaning depends on which reader opens it is not a snapshot
of anything. That is a deliberate posture, not a limitation: silently keeping the last of two
entries is how a manifest and its archive come to disagree with nobody noticing.

**Sizes come from counting, never from the header.** `ZipInfo.file_size` is a number the attacker
chose. It is used only to refuse EARLY -- an entry claiming more than the limit is rejected without
reading it -- and never as a reason to trust an entry that claims to be small. The writing half
must still count bytes as it writes and stop at the limit regardless of what the header promised.
"""

from __future__ import annotations

import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

from menhir.snapshot.protocol import (
    ERR_LIMIT_EXPANSION_RATIO,
    ERR_LIMIT_FILE_BYTES,
    ERR_LIMIT_FILE_COUNT,
    ERR_LIMIT_TOTAL_BYTES,
    ERR_MANIFEST_CASE_COLLISION,
    ERR_MANIFEST_DUPLICATE_PATH,
    PROVISIONAL_LIMITS,
    BundleFormatError,
    BundlePathError,
    SnapshotLimits,
    normalize_bundle_path,
)

__all__ = [
    "ERR_ARCHIVE_ENTRY_NOT_REGULAR",
    "ERR_ARCHIVE_UNREADABLE",
    "MAX_EXPANSION_RATIO",
    "PlannedEntry",
    "plan_archive",
]

#: Additive to the frozen protocol's code set, not a change to any existing code. P0 froze codes
#: for paths, manifests and limits; neither case here is any of those -- an archive that will not
#: parse, and an entry that is not a regular file -- so they are declared where they are raised.
ERR_ARCHIVE_UNREADABLE = "snapshot.archive.unreadable"
ERR_ARCHIVE_ENTRY_NOT_REGULAR = "snapshot.archive.entry_not_regular_file"

#: Compressed-to-decompressed ceiling for a single entry. Source text compresses roughly 3-5x and
#: minified or generated files reach perhaps 20x, so 100 is far above anything a real repository
#: produces and far below a zip bomb, which reaches 1000x and beyond. A ratio ceiling catches the
#: bomb whose DECLARED total still fits the byte limit -- the case a byte limit alone misses.
MAX_EXPANSION_RATIO = 100

#: Unix mode bits live in the high half of `external_attr` when the archive was made on a Unix
#: host. `0o170000` masks the file-type nibble; `0o100000` is a regular file.
_UNIX_MODE_SHIFT = 16
_S_IFMT = 0o170000
_S_IFREG = 0o100000


@dataclass(frozen=True)
class PlannedEntry:
    """One entry that may be written, and the name it may be written under."""

    #: The normalized bundle path. Never the raw archive name.
    path: str
    #: The archive's own name for it, kept for opening the entry and for nothing else. It must not
    #: reach a log or an error: it is attacker-chosen text (plan invariant 5).
    raw_name: str
    declared_size: int


def _archive_entry_names(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return [info for info in archive.infolist() if not info.is_dir()]


def _is_regular_file(info: zipfile.ZipInfo) -> bool:
    """Whether the entry is a regular file, treating an unknown origin as acceptable.

    Only archives created on a Unix host carry mode bits; one written on Windows leaves
    `external_attr` without them, and demanding them would refuse every bundle built there. So the
    check refuses what is POSITIVELY not a regular file -- a symlink, device or socket -- rather
    than requiring proof that it is one.

    A symlink entry is the case that matters: extracted naively it is a link the next write follows
    out of the root, which is zip-slip with an extra step.
    """
    if info.create_system != 3:  # 3 == Unix; anything else carries no mode bits to read
        return True
    mode = (info.external_attr >> _UNIX_MODE_SHIFT) & _S_IFMT
    if mode == 0:  # no type recorded
        return True
    return mode == _S_IFREG


def _collision_key(path: str) -> str:
    """The key two paths collide on when the destination filesystem is not case-sensitive.

    Case-folds AND normalizes unicode: `README.md` vs `readme.md` is the obvious collision, and
    NFC vs NFD spellings of the same accented name are the one that is invisible in a diff and
    still produces a single file on macOS.
    """
    return unicodedata.normalize("NFC", path).casefold()


def plan_archive(
    source: Path | bytes, limits: SnapshotLimits = PROVISIONAL_LIMITS
) -> list[PlannedEntry]:
    """Return the entries an extractor may write, or raise `BundleFormatError`.

    Refuses on the first violation rather than collecting them. A hostile bundle does not need a
    full report, and continuing to walk an archive that has already proven hostile is work done on
    an attacker's behalf.
    """
    try:
        archive = zipfile.ZipFile(
            source if isinstance(source, Path) else _as_buffer(source)
        )
    except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
        raise BundleFormatError(
            ERR_ARCHIVE_UNREADABLE, "the archive could not be read"
        ) from exc

    with archive:
        entries = _archive_entry_names(archive)

        # Count first, before any per-entry work: refusing a million entries should not cost a
        # million normalizations.
        if len(entries) > limits.max_file_count:
            raise BundleFormatError(
                ERR_LIMIT_FILE_COUNT,
                f"archive holds more than {limits.max_file_count} files",
            )

        planned: list[PlannedEntry] = []
        seen: set[str] = set()
        collisions: dict[str, str] = {}
        total = 0

        for info in entries:
            if not _is_regular_file(info):
                raise BundleFormatError(
                    ERR_ARCHIVE_ENTRY_NOT_REGULAR,
                    "archive contains an entry that is not a regular file",
                )

            # The path rules are re-applied to the archive's own name. The bundle may not have come
            # from our bundler, so "the bundler would never emit that" is not a defence available
            # here. `BundlePathError` already carries a stable code and never echoes the path.
            try:
                path = normalize_bundle_path(info.filename, limits)
            except BundlePathError as exc:
                raise BundleFormatError(
                    exc.code, "archive contains a refused path"
                ) from exc

            if path in seen:
                raise BundleFormatError(
                    ERR_MANIFEST_DUPLICATE_PATH, "archive contains the same path twice"
                )
            key = _collision_key(path)
            if key in collisions:
                raise BundleFormatError(
                    ERR_MANIFEST_CASE_COLLISION,
                    "archive contains two paths that collide on a case-insensitive filesystem",
                )

            if info.file_size > limits.max_file_bytes:
                raise BundleFormatError(
                    ERR_LIMIT_FILE_BYTES,
                    "an entry declares more than the per-file limit",
                )
            # Ratio before total: a bomb's declared total can sit inside the byte limit while a
            # single entry expands a thousandfold, which is the case a byte limit alone misses.
            if (
                info.compress_size > 0
                and info.file_size / info.compress_size > MAX_EXPANSION_RATIO
            ):
                raise BundleFormatError(
                    ERR_LIMIT_EXPANSION_RATIO,
                    "an entry expands beyond the permitted ratio",
                )

            total += info.file_size
            if total > limits.max_total_bytes:
                raise BundleFormatError(
                    ERR_LIMIT_TOTAL_BYTES,
                    "the archive declares more than the total byte limit",
                )

            seen.add(path)
            collisions[key] = path
            planned.append(
                PlannedEntry(
                    path=path, raw_name=info.filename, declared_size=info.file_size
                )
            )

    return planned


def _as_buffer(blob: bytes):
    import io

    return io.BytesIO(blob)
