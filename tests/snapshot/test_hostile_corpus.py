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
from tests.snapshot.hostile_corpus import CORPUS, NEEDS_EXTRACTOR, PATH_CARRIED

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

    assert excinfo.value.code == case.expected_code, (
        f"{case.name} ({case.attack}) was refused as {excinfo.value.code}, "
        f"expected {case.expected_code}"
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

    assert str(excinfo.value) == case.expected_code
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


def test_python_rewrites_a_backslash_entry_into_a_traversal() -> None:
    """A finding this corpus produced, pinned so P3 cannot be designed against the wrong belief.

    The archive really does store `..\\..\\secret` -- asserted here against the RAW BYTES, not the
    reader's view. Python's ZIP reader rewrites the separator, so `namelist()` yields
    `../../secret` and an extractor built on it never sees a backslash.

    The outcome happens to be safe: one refused attack becomes another refused attack. The reason
    to pin it is that `ERR_PATH_BACKSLASH` therefore cannot fire for an archive entry read this
    way. That rule defends the manifest, which is JSON read verbatim, and NOT the archive -- so
    this case's safety rests on a stdlib detail rather than on a check we own. If P3 wants the
    backslash rule to mean anything for archives, it must read names from the central directory
    itself.
    """
    case = next(c for c in CORPUS if c.name == "backslash")
    blob = case.build()

    assert b"..\\..\\secret" in blob, "the attack is not present on the wire; the corpus is wrong"
    assert b"../../secret" not in blob

    assert _names(blob) == ["../../secret"], (
        "Python no longer rewrites the separator on read. That is a behaviour change: revisit "
        "whether ERR_PATH_BACKSLASH should now be the expected refusal for this case."
    )


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
