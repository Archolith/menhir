"""The hostile corpus, asserted against what exists today.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

Two jobs, and keeping them apart is the point:

1. **Prove what is already defended.** Every path-carried attack is refused by the frozen contract,
   asserted here against a real archive rather than a string. `normalize_bundle_path` has always
   been tested with hostile strings; nothing had ever handed the system a ZIP whose central
   directory contains `../../etc/passwd`.
2. **Prove the validator refuses all twelve**, including the four no path rule could reach:
   duplicates, case collisions, bombs and count exhaustion. `plan_archive` decides what an
   extractor would be allowed to write and creates nothing, so the defence is exercised without a
   managed root, a lease or a worker existing.

The file briefly held a third job -- declaring those four as undefended -- and that wording is
gone rather than softened, because it is no longer true. `NEEDS_EXTRACTOR` still names them, but it
now means "not refusable by a path rule alone", not "not refused".
"""

from __future__ import annotations

import io
import zipfile

import pytest

from menhir.snapshot import archive_plan, protocol
from menhir.snapshot.protocol import (
    PROVISIONAL_LIMITS,
    BundleFormatError,
    BundlePathError,
    normalize_bundle_path,
)
from tests.snapshot.hostile_corpus import (
    CORPUS,
    NEEDS_EXTRACTOR,
    PATH_CARRIED,
    expected_codes,
)

pytestmark = pytest.mark.unit


def _names(blob: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.namelist()


@pytest.mark.parametrize("case", PATH_CARRIED, ids=lambda c: c.name)
def test_a_hostile_entry_name_is_refused_when_read_back_from_a_real_archive(case) -> None:
    """The rule must hold on the name as it survives a round trip through the ZIP format.

    Read back rather than asserted against the literal: a format can normalise, re-encode or
    truncate a name on the way in, and the value the extractor will actually see is the one that
    comes out of the central directory.
    """
    stored = _names(case.build())
    assert stored, f"{case.name}: the archive has no entries"

    with pytest.raises(BundlePathError) as excinfo:
        for name in stored:
            normalize_bundle_path(name)

    allowed = expected_codes(case)
    assert excinfo.value.code in allowed, (
        f"{case.name} ({case.attack}) was refused as {excinfo.value.code}, "
        f"expected one of {sorted(allowed)}"
    )


@pytest.mark.parametrize("case", PATH_CARRIED, ids=lambda c: c.name)
def test_a_refusal_never_echoes_the_hostile_path(case) -> None:
    """Invariant 5 at the point it is most tempting to break.

    A rejected path is attacker-controlled text. `str(exc)` is the code alone precisely so a server
    can log the refusal; the path lives on `.path` for local client output only. A terminal escape
    sequence in a filename is in the corpus for this reason.
    """
    stored = _names(case.build())
    with pytest.raises(BundlePathError) as excinfo:
        for name in stored:
            normalize_bundle_path(name)

    assert str(excinfo.value) in expected_codes(case)
    assert case.offending_path is not None
    assert case.offending_path not in str(excinfo.value)


@pytest.mark.parametrize("case", NEEDS_EXTRACTOR, ids=lambda c: c.name)
def test_an_extractor_bound_attack_is_well_formed_and_names_a_real_code(case) -> None:
    """The corpus entry is well-formed and names a code the protocol already defines.

    Separate from the refusal test on purpose. This one asserts the ATTACK is real -- that the
    archive still exhibits the shape its name claims -- because a corpus entry that quietly stopped
    being hostile would pass a refusal test only by accident, or stop passing it for the right
    reason and be "fixed" in the wrong place.
    """
    blob = case.build()
    stored = _names(blob)
    assert stored, f"{case.name}: the archive has no entries"

    # Codes come from two modules now. `protocol` froze paths, manifests and limits in P0;
    # `archive_plan` added two for cases that are none of those -- an unreadable archive and an
    # entry that is not a regular file. Both are stable contracts, so both count.
    frozen_codes = {
        value
        for name, value in vars(protocol).items()
        if name.startswith(("ERR_PATH", "ERR_MANIFEST", "ERR_LIMIT")) and isinstance(value, str)
    } | {
        value
        for name, value in vars(archive_plan).items()
        if name.startswith("ERR_ARCHIVE") and isinstance(value, str)
    }
    assert case.expected_code in frozen_codes, (
        f"{case.name} expects {case.expected_code}, which is not a code the protocol defines"
    )

    # Each structural attack must actually exhibit its shape; a corpus entry that quietly stopped
    # being hostile would pass every other assertion here.
    if case.name == "duplicate_entry":
        assert len(stored) > len(set(stored))
    elif case.name == "case_collision":
        assert len(stored) > len({name.lower() for name in stored})
    elif case.name == "decompression_bomb":
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            info = archive.infolist()[0]
            assert info.file_size / max(info.compress_size, 1) > 100
    elif case.name == "entry_count_exhaustion":
        assert len(stored) >= 1000
    elif case.name == "symlink_escape":
        # The attack is carried by the MODE BITS, not the name, so the shape check has to look
        # there: an ordinary-looking `src/innocent.py` whose type nibble says S_IFLNK.
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            info = archive.infolist()[0]
            assert info.create_system == 3, "not marked as Unix; the mode bits would be ignored"
            assert (info.external_attr >> 16) & 0o170000 == 0o120000, "not stored as a symlink"


def test_the_backslash_refusal_depends_on_the_platform_reading_it() -> None:
    """A finding this corpus produced, corrected twice, pinned so P3 is not designed on a guess.

    The archive really does store a backslash-separated name -- asserted against the RAW BYTES, not
    the reader's view of them. What happens on the way back out depends on the platform, because
    Python's ZIP reader maps the stored separator through `os.sep`:

    * Linux keeps the backslash, so `normalize_bundle_path` refuses it as `path.backslash` -- the
      rule we own, on the platform Menhir runs on.
    * Windows rewrites it to "/", so the name arrives as a traversal and is refused as one.

    Either way the bundle is rejected, so nothing is unsafe. The reason to pin it is that the same
    hostile bytes produce DIFFERENT stated reasons on different machines, which means an extractor
    wanting a platform-independent answer must read names from the central directory itself rather
    than through `namelist()`.

    An earlier version of this test asserted the Windows behaviour as universal and CI disproved
    it. That is why this asserts the relationship rather than one outcome.
    """
    case = next(c for c in CORPUS if c.name == "backslash")
    blob = case.build()

    backslashed = "..\\..\\secret"
    assert backslashed.encode() in blob, (
        "the attack is not present on the wire; the corpus is wrong"
    )
    assert b"../../secret" not in blob

    (stored,) = _names(blob)
    if "\\" in stored:
        assert stored == backslashed
        assert _refusal_code(stored) == protocol.ERR_PATH_BACKSLASH
    else:
        assert stored == "../../secret"
        assert _refusal_code(stored) == protocol.ERR_PATH_TRAVERSAL


def _refusal_code(name: str) -> str:
    """Return the refusal code for `name`, or fail the test if it is not refused at all."""
    try:
        normalize_bundle_path(name)
    except BundlePathError as exc:
        return exc.code
    raise AssertionError(f"{name!r} was not refused")


def test_every_path_carried_attack_is_refused_and_the_rest_are_declared_pending() -> None:
    """A completeness check, so the corpus cannot grow an entry with no expectation.

    Also records the split in one place: if a future change makes the extractor refuse one of the
    structural cases, this is where the count moves and someone has to think about it.
    """
    assert len(CORPUS) == len(PATH_CARRIED) + len(NEEDS_EXTRACTOR)
    assert {case.expected_code for case in CORPUS} == {
        case.expected_code for case in CORPUS if case.expected_code
    }, "every corpus entry must declare a refusal code"
    assert len(NEEDS_EXTRACTOR) == 5, (
        "five attacks are structural rather than path-carried: symlink escape, duplicate entry, "
        "case collision, decompression bomb, entry-count exhaustion. All five ARE refused -- by "
        "`plan_archive`, not by a path rule -- so this number tracks the split, not a gap in the "
        "defence. symlink_escape was added after mutation testing found its guard could be "
        "deleted with no test noticing."
    )


# --- the validator: the decision half of extraction ----------------------------------------------


@pytest.mark.parametrize("case", CORPUS, ids=lambda c: c.name)
def test_the_validator_refuses_every_corpus_attack(case) -> None:
    """All twelve, including the four no path rule could catch.

    `plan_archive` reads the central directory and returns what an extractor would be ALLOWED to
    write. It creates nothing, so this is the whole defence being exercised without a managed root,
    a lease or a worker existing yet -- those depend on design questions still open, and none of
    them are needed to decide that a bundle is hostile.
    """
    from dataclasses import replace

    from menhir.snapshot.archive_plan import plan_archive

    # The count limit is tightened for the test rather than the corpus being grown to 20,001
    # entries. The first run exposed the alternative: a 5,000-entry archive is comfortably UNDER
    # the pilot limit, so the validator correctly did not refuse it and the test was asserting a
    # rule it had not actually exercised. Every other limit stays at the shipping value, so each
    # attack still trips the rule it is named for -- in particular the bomb's 8 MiB entry must stay
    # within `max_file_bytes` or it would be refused for size before the ratio is ever considered.
    limits = replace(PROVISIONAL_LIMITS, max_file_count=100)

    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(case.build(), limits)

    allowed = expected_codes(case)
    assert excinfo.value.code in allowed, (
        f"{case.name} ({case.attack}) was refused as {excinfo.value.code}, "
        f"expected one of {sorted(allowed)}"
    )


def test_the_validator_accepts_an_ordinary_bundle() -> None:
    """The corpus proves refusals; this proves the validator is not simply refusing everything.

    A guard that rejects all input passes every hostile test and is useless, so the positive case
    belongs beside them rather than in a separate file someone reads later.
    """
    from menhir.snapshot.archive_plan import plan_archive
    from tests.snapshot.hostile_corpus import _zip

    planned = plan_archive(_zip([("src/main.py", b"print('hi')\n"), ("README.md", b"# ok\n")]))

    assert [entry.path for entry in planned] == ["src/main.py", "README.md"]
    assert all(entry.declared_size > 0 for entry in planned)


def test_the_validator_refuses_bytes_that_are_not_an_archive() -> None:
    """A caller can upload anything; `SEALED` means the bytes arrived, not that they are a ZIP."""
    from menhir.snapshot.archive_plan import ERR_ARCHIVE_UNREADABLE, plan_archive

    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(b"this is not a zip file, it is just some bytes")

    assert excinfo.value.code == ERR_ARCHIVE_UNREADABLE


def test_a_validator_refusal_never_echoes_the_archive_path() -> None:
    """Invariant 5 across the new boundary.

    `plan_archive` sees attacker-chosen names and raises `BundleFormatError`, which a server may
    log. The name must not travel with it.
    """
    from menhir.snapshot.archive_plan import plan_archive

    case = next(c for c in CORPUS if c.name == "control_character")
    with pytest.raises(BundleFormatError) as excinfo:
        plan_archive(case.build())

    rendered = str(excinfo.value)
    assert "\x1b" not in rendered
    assert "il.py" not in rendered
