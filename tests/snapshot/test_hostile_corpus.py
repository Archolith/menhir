"""The hostile corpus, asserted against what exists today.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md`.

Two jobs, and keeping them apart is the point:

1. **Prove what is already defended.** Every path-carried attack is refused by the frozen contract,
   asserted here against a real archive rather than a string. `normalize_bundle_path` has always
   been tested with hostile strings; nothing had ever handed the system a ZIP whose central
   directory contains `../../etc/passwd`.
2. **State what is not, without pretending.** Duplicates, case collisions, bombs and count
   exhaustion need the extractor P3 will build. Those cases are asserted to be well-formed and to
   name a real refusal code -- never to be refused, because nothing refuses them yet. A test that
   reported those as protected would be worse than no test.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from menhir.snapshot import protocol
from menhir.snapshot.protocol import BundlePathError, normalize_bundle_path
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
    """These are NOT yet defended, and this test does not claim they are.

    It asserts only that the corpus entry is usable the day the extractor exists: the archive
    builds, it carries the shape the attack needs, and its expected refusal is a code the frozen
    protocol already defines rather than one this corpus invented.
    """
    blob = case.build()
    stored = _names(blob)
    assert stored, f"{case.name}: the archive has no entries"

    frozen_codes = {
        value
        for name, value in vars(protocol).items()
        if name.startswith(("ERR_PATH", "ERR_MANIFEST", "ERR_LIMIT")) and isinstance(value, str)
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
    assert len(NEEDS_EXTRACTOR) == 4, (
        "four attacks still have no defence: duplicate entry, case collision, decompression bomb, "
        "entry-count exhaustion. Update this number when P3 defends one, deliberately."
    )
