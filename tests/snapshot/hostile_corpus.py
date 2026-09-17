"""Deliberately hostile bundles, each declaring the refusal it must produce.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

P3 is the first phase that opens an archive a stranger uploaded, and the design argues for building
this corpus BEFORE the extractor -- because both bugs found in P2B were found by writing the
adversarial case first and watching it succeed.

These are real ZIP files, not path strings. That distinction is the point: `normalize_bundle_path`
is already tested against hostile strings, but nothing has ever handed the system an archive whose
central directory contains `../../etc/passwd`. A rule that holds for a string and not for an
archive entry is exactly the gap an extractor introduces.

**Two populations, deliberately kept in one list.**

*Covered now* -- the frozen path contract already refuses these, and `test_hostile_corpus.py`
proves it against these archives today.

*Pending P3* -- duplicate entries, case collisions, bombs and count exhaustion cannot be refused by
a path rule; they need the extractor. Their refusal codes already exist in the frozen protocol, so
the corpus states what the extractor must do rather than waiting to find out. They are NOT marked
as passing, because an unimplemented protection that reports success is worse than an absent one.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

from menhir.snapshot.protocol import (
    ERR_LIMIT_EXPANSION_RATIO,
    ERR_LIMIT_FILE_COUNT,
    ERR_MANIFEST_CASE_COLLISION,
    ERR_MANIFEST_DUPLICATE_PATH,
    ERR_PATH_ABSOLUTE,
    ERR_PATH_CONTROL_CHAR,
    ERR_PATH_DRIVE,
    ERR_PATH_EMPTY_SEGMENT,
    ERR_PATH_RESERVED_NAME,
    ERR_PATH_TRAILING_DOT_SPACE,
    ERR_PATH_TRAVERSAL,
)

__all__ = ["CORPUS", "HostileArchive"]


@dataclass(frozen=True)
class HostileArchive:
    """One hostile bundle and the refusal it must produce."""

    name: str
    #: What an attacker is attempting, in one line.
    attack: str
    #: The stable code this bundle must be refused with. Always a code that already exists in the
    #: frozen protocol -- a corpus entry inventing its own code would be describing a contract
    #: nobody agreed to.
    expected_code: str
    #: The single entry path under test, when the attack is carried by a path. None when the
    #: attack is structural (duplicates, size, count) and no single path is at fault.
    offending_path: str | None
    build: Callable[[], bytes]


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Write entries verbatim, permitting names a well-behaved writer would refuse.

    `ZipFile.writestr` does not validate names, which is precisely why an archive is a different
    threat surface from a path string: the hostile name survives into the central directory and is
    read back by whatever opens it.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _one(name: str) -> Callable[[], bytes]:
    return lambda: _zip([(name, b"x")])


def _zip_verbatim_name(name: str) -> bytes:
    """Write one entry whose stored name is exactly `name`, separators included.

    `ZipInfo.__init__` replaces `os.sep` with "/", so on Windows `ZipInfo("a\\b")` is already "a/b"
    before anything is written. Assigning `filename` after construction bypasses that and puts the
    bytes on the wire verbatim -- confirmed by asserting against the raw archive bytes, not the
    reader's view of them.

    Worth stating precisely, because the first version of this docstring got it wrong: the
    round-trip loss is on READ, not write. The archive does contain the backslash; Python's reader
    rewrites it. See `test_python_rewrites_a_backslash_entry_into_a_traversal`.
    """
    info = zipfile.ZipInfo("placeholder")
    info.filename = name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(info, b"x")
    return buffer.getvalue()


CORPUS: tuple[HostileArchive, ...] = (
    # --- carried by a path: the frozen contract already refuses these ---------------------------
    HostileArchive(
        "traversal",
        "escape the extraction root with ../ segments (zip-slip)",
        ERR_PATH_TRAVERSAL,
        "../../etc/passwd",
        _one("../../etc/passwd"),
    ),
    HostileArchive(
        "absolute",
        "write to an absolute path, ignoring the root entirely",
        ERR_PATH_ABSOLUTE,
        "/etc/passwd",
        _one("/etc/passwd"),
    ),
    HostileArchive(
        "windows_drive",
        "escape via a drive-relative path, which has no leading separator to catch it",
        ERR_PATH_DRIVE,
        "C:windows/system32/drivers/etc/hosts",
        _one("C:windows/system32/drivers/etc/hosts"),
    ),
    HostileArchive(
        "backslash",
        "traverse on Windows using a separator POSIX treats as an ordinary filename character",
        # NOT `ERR_PATH_BACKSLASH`, and the reason is a finding this corpus produced.
        # The archive genuinely stores `..\..\secret` -- verified against the raw bytes in
        # `test_python_rewrites_a_backslash_entry_into_a_traversal`. Python's ZIP READER rewrites
        # the separator, so an extractor calling `namelist()` is handed `../../secret` and never
        # sees a backslash at all.
        #
        # The outcome is safe: the rewrite turns one refused attack into another refused attack.
        # But it means `ERR_PATH_BACKSLASH` cannot fire on an archive entry read through Python,
        # so that rule defends the MANIFEST (read as JSON, verbatim) and not the archive -- and
        # this case's safety currently rests on a stdlib implementation detail rather than on our
        # own check. P3 should re-derive entry names from the raw central directory if it wants
        # the backslash rule to mean anything for archives.
        ERR_PATH_TRAVERSAL,
        "..\\..\\secret",
        lambda: _zip_verbatim_name("..\\..\\secret"),
    ),
    HostileArchive(
        "control_character",
        "smuggle a terminal escape or NUL through a filename into a log or a C string",
        ERR_PATH_CONTROL_CHAR,
        "src/ev\x1b[2Jil.py",
        _one("src/ev\x1b[2Jil.py"),
    ),
    HostileArchive(
        "empty_segment",
        "double separator, which some path joiners collapse and others do not",
        ERR_PATH_EMPTY_SEGMENT,
        "src//main.py",
        _one("src//main.py"),
    ),
    HostileArchive(
        "windows_reserved",
        "a name that is a device on Windows, so writing the file writes to the device",
        ERR_PATH_RESERVED_NAME,
        "CON",
        _one("CON"),
    ),
    HostileArchive(
        "trailing_dot",
        "a trailing dot Windows silently strips, so two entries become one file",
        ERR_PATH_TRAILING_DOT_SPACE,
        "src/main.py.",
        _one("src/main.py."),
    ),
    # --- structural: no single path is at fault, and only the extractor can refuse them ---------
    HostileArchive(
        "duplicate_entry",
        "the same path twice, where readers disagree about which one wins",
        ERR_MANIFEST_DUPLICATE_PATH,
        None,
        lambda: _zip([("src/main.py", b"first"), ("src/main.py", b"second")]),
    ),
    HostileArchive(
        "case_collision",
        "two names that are distinct on Linux and one file on macOS or Windows",
        ERR_MANIFEST_CASE_COLLISION,
        None,
        lambda: _zip([("README.md", b"a"), ("readme.md", b"b")]),
    ),
    HostileArchive(
        "decompression_bomb",
        "a small archive that expands to far more than it declares it costs",
        ERR_LIMIT_EXPANSION_RATIO,
        None,
        # Highly compressible, so the ratio is enormous while the archive stays tiny. The point is
        # not the absolute size: it is that the ratio must be measured while writing, because the
        # header's declared size is a number the attacker chose.
        lambda: _zip([("big.bin", b"\0" * (8 * 1024 * 1024))]),
    ),
    HostileArchive(
        "entry_count_exhaustion",
        "many tiny entries, to exhaust inodes or the file-count limit rather than bytes",
        ERR_LIMIT_FILE_COUNT,
        None,
        lambda: _zip([(f"f{i:05d}.txt", b"x") for i in range(5000)]),
    ),
)

#: Entries whose refusal is carried by a path, so the frozen contract already covers them.
PATH_CARRIED = tuple(case for case in CORPUS if case.offending_path is not None)

#: Entries that need the P3 extractor. Listed so a reader can see what is NOT yet defended.
NEEDS_EXTRACTOR = tuple(case for case in CORPUS if case.offending_path is None)
