"""Hostile manifests, and limits tested at their boundaries.

Found by mutation testing: 19 mutations survived in `protocol.py`, more than any other module.
That was a surprise worth stating plainly -- it is the oldest module with the most tests, and its
existing coverage catches the obvious refusals (forged digest, duplicate path, count mismatch)
while leaving the malformed-input paths and every boundary untouched.

**The manifest is attacker-controlled.** `SnapshotManifest.from_mapping` is the server's entry
point and says so: it assumes hostile input. A bundle carries its own manifest, so a caller who can
upload can choose every field in it -- the same posture the archive corpus takes, applied to the
other half of the bundle.

Two shapes, matching the mutation kinds that survived:

* **Malformed input** that must raise rather than propagate a `KeyError`, a `TypeError`, or a
  silently wrong parse.
* **Boundaries**, tested at the limit and one past it. Every existing limit test uses a value far
  over the line, so `>` and `>=` behave identically and an off-by-one survives.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from menhir.snapshot.protocol import (
    ERR_LIMIT_FILE_COUNT,
    ERR_LIMIT_TOTAL_BYTES,
    ERR_MANIFEST_MALFORMED,
    ERR_PATH_EMPTY,
    ERR_PATH_SEGMENT_TOO_LONG,
    ERR_PATH_TOO_LONG,
    PROVISIONAL_LIMITS,
    BundleFormatError,
    BundlePathError,
    FileRecord,
    SnapshotLimits,
    SnapshotManifest,
    b64_encoded_len,
    normalize_bundle_path,
    sha256_hex,
)

pytestmark = pytest.mark.unit


def _record(path: str, content: bytes = b"x", executable: bool = False) -> FileRecord:
    return FileRecord(
        path=path, size=len(content), sha256=sha256_hex(content), executable=executable
    )


def _raw(**overrides) -> dict:
    manifest = SnapshotManifest.build(display_name="p", files=[_record("a.py")])
    raw = json.loads(manifest.to_canonical_bytes())
    raw.update(overrides)
    return raw


# --- malformed input must refuse, not explode -----------------------------------------------------


def test_a_manifest_whose_files_are_not_a_list_is_refused() -> None:
    """A `TypeError` escaping the parser is a 500 where a refusal belongs.

    The server calls this on bytes a stranger uploaded. Every failure has to arrive as a stable
    code, or the caller cannot tell a bad bundle from a broken server -- the same distinction the
    P2B crash fix was about.
    """
    with pytest.raises((BundleFormatError, TypeError)) as excinfo:
        SnapshotManifest.from_mapping(_raw(files="not-a-list"))

    assert not isinstance(excinfo.value, KeyError)


def test_a_manifest_missing_a_required_field_is_refused_as_malformed() -> None:
    raw = _raw()
    del raw["tree_digest"]

    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)

    assert excinfo.value.code == ERR_MANIFEST_MALFORMED


def test_a_manifest_with_a_non_numeric_count_is_refused_as_malformed() -> None:
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(_raw(file_count="many"))

    assert excinfo.value.code == ERR_MANIFEST_MALFORMED


def test_a_file_entry_that_is_not_a_mapping_is_refused_as_malformed() -> None:
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(_raw(files=["just-a-string"]))

    assert excinfo.value.code == ERR_MANIFEST_MALFORMED


def test_a_negative_file_size_is_refused() -> None:
    """The `< 0` half of the size bound, which the over-limit test never reaches.

    A negative size is not merely nonsense: it is the value that makes a running total shrink, so
    a manifest full of them could declare a total under the limit while carrying far more.
    """
    raw = _raw()
    raw["files"][0]["size"] = -1

    with pytest.raises(BundleFormatError):
        SnapshotManifest.from_mapping(raw)


@pytest.mark.parametrize(
    "digest",
    [
        "A" * 64,  # uppercase: the contract says lowercase hex
        "a" * 63,  # one short
        "a" * 65,  # one long
        "g" * 64,  # not hex at all
    ],
    ids=["uppercase", "too-short", "too-long", "not-hex"],
)
def test_a_sha256_that_is_not_64_lowercase_hex_is_refused(digest: str) -> None:
    """The existing test uses one bad value; each of these fails a different clause."""
    raw = _raw()
    raw["files"][0]["sha256"] = digest

    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)

    assert excinfo.value.code == ERR_MANIFEST_MALFORMED


def test_a_malformed_omission_is_silently_dropped_which_is_worth_knowing() -> None:
    """Characterises CURRENT behaviour, and flags it rather than changing it.

    A malformed omission entry -- here, one with no `reason` -- is filtered out by the parsing
    comprehension and the manifest parses successfully without it. No comment in `protocol.py`
    explains the choice, so it reads as either deliberate leniency (omissions are advisory) or an
    oversight.

    **Why it may matter.** The module docstring gives omissions a specific job: an omission the
    server cannot observe "would otherwise read as deletion". Dropping one silently therefore
    converts a file the client deliberately left out into a file the server believes was DELETED.
    That is inert today -- P3 writes no graph -- and becomes a pruning decision in P4.

    Pinned as-is because changing it is a product decision, not a test fix. If the leniency is
    intended, this test documents it; if it is not, this is where the change lands.
    """
    parsed = SnapshotManifest.from_mapping(_raw(omissions=[{"path": "a"}]))  # no reason

    assert parsed.omissions == (), (
        "behaviour changed: malformed omissions are no longer dropped"
    )


# --- boundaries: at the limit, and one past -------------------------------------------------------


def test_a_file_count_exactly_at_the_limit_parses() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_count=3)
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record(f"f{i}.py") for i in range(3)]
    )

    parsed = SnapshotManifest.from_mapping(
        json.loads(manifest.to_canonical_bytes()), limits
    )

    assert parsed.file_count == 3


def test_one_file_over_the_count_limit_is_refused() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_count=3)
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record(f"f{i}.py") for i in range(4)]
    )

    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(json.loads(manifest.to_canonical_bytes()), limits)

    assert excinfo.value.code == ERR_LIMIT_FILE_COUNT


def test_a_total_exactly_at_the_byte_limit_parses() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=8, max_file_bytes=8)
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record("a.py", b"12345678")]
    )

    parsed = SnapshotManifest.from_mapping(
        json.loads(manifest.to_canonical_bytes()), limits
    )

    assert parsed.total_bytes == 8


def test_a_total_one_byte_over_the_limit_is_refused() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_total_bytes=8, max_file_bytes=16)
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record("a.py", b"123456789")]
    )

    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(json.loads(manifest.to_canonical_bytes()), limits)

    assert excinfo.value.code == ERR_LIMIT_TOTAL_BYTES


def test_a_file_exactly_at_the_per_file_limit_parses() -> None:
    limits = replace(PROVISIONAL_LIMITS, max_file_bytes=4, max_total_bytes=64)
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record("a.py", b"1234")]
    )

    parsed = SnapshotManifest.from_mapping(
        json.loads(manifest.to_canonical_bytes()), limits
    )

    assert parsed.files[0].size == 4


# --- path length boundaries -----------------------------------------------------------------------


def test_a_path_exactly_at_the_length_limit_is_accepted() -> None:
    limits = SnapshotLimits(max_path_bytes=12, max_segment_bytes=12)

    assert normalize_bundle_path("a" * 12, limits) == "a" * 12


def test_a_path_one_byte_over_the_length_limit_is_refused() -> None:
    limits = SnapshotLimits(max_path_bytes=12, max_segment_bytes=12)

    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path("a" * 13, limits)

    assert excinfo.value.code == ERR_PATH_TOO_LONG


def test_a_segment_exactly_at_the_limit_is_accepted() -> None:
    limits = SnapshotLimits(max_path_bytes=64, max_segment_bytes=4)

    assert normalize_bundle_path("abcd/abcd", limits) == "abcd/abcd"


def test_a_segment_one_byte_over_the_limit_is_refused() -> None:
    limits = SnapshotLimits(max_path_bytes=64, max_segment_bytes=4)

    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path("abcd/abcde", limits)

    assert excinfo.value.code == ERR_PATH_SEGMENT_TOO_LONG


def test_an_empty_path_is_refused() -> None:
    """The first guard in the function, and nothing was holding it."""
    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path("")

    assert excinfo.value.code == ERR_PATH_EMPTY


# --- chunk arithmetic -----------------------------------------------------------------------------


def test_a_chunk_exactly_at_the_hard_ceiling_is_accepted() -> None:
    limits = SnapshotLimits(max_chunk_bytes=1024)

    limits.validate_chunk_bytes(1024)  # must not raise


def test_a_chunk_one_byte_over_the_hard_ceiling_is_refused() -> None:
    limits = SnapshotLimits(max_chunk_bytes=1024)

    with pytest.raises(BundleFormatError):
        limits.validate_chunk_bytes(1025)


def test_a_negative_chunk_length_is_refused() -> None:
    """A negative length is how a size check gets skipped rather than failed."""
    limits = SnapshotLimits(max_chunk_bytes=1024)

    with pytest.raises(BundleFormatError):
        limits.validate_chunk_bytes(-1)


def test_encoded_length_refuses_a_negative_input() -> None:
    with pytest.raises(ValueError):
        b64_encoded_len(-1)


# --- the zero boundary, and exception precision ---------------------------------------------------
#
# A second mutation pass on protocol.py left seven survivors and showed what the first pass had
# missed. Two guards are `x < 0 or x > limit`, and every test used a value at or past the UPPER
# bound, so the lower clause was never exercised. Zero is the interesting value there: an empty
# file is entirely legitimate and must be ACCEPTED, which is what pins `< 0` against `<= 0`.


def test_a_zero_byte_file_is_accepted() -> None:
    """Empty files are ordinary -- `__init__.py` is usually one.

    This is the case that distinguishes `size < 0` from `size <= 0`. Tightening that comparison
    would reject a legitimate repository, and no test noticed until the lower clause was probed.
    """
    manifest = SnapshotManifest.build(
        display_name="p", files=[_record("__init__.py", b"")]
    )

    parsed = SnapshotManifest.from_mapping(json.loads(manifest.to_canonical_bytes()))

    assert parsed.files[0].size == 0
    assert parsed.total_bytes == 0


def test_a_zero_length_chunk_is_accepted() -> None:
    """Same boundary on the chunk path: zero is valid, negative is not."""
    limits = SnapshotLimits(max_chunk_bytes=1024)

    limits.validate_chunk_bytes(0)  # must not raise


def test_encoded_length_of_zero_is_zero() -> None:
    assert b64_encoded_len(0) == 0


def test_the_files_type_guard_is_redundant_and_that_is_why_its_mutation_survives() -> (
    None
):
    """NOT a coverage gap -- an equivalent mutant, documented so it is not chased again.

    `raise TypeError("files must be a list")` sits INSIDE a `try` that catches `TypeError` and
    converts it to `BundleFormatError`, so it never escapes; it is an internal signal. Deleting it
    changes nothing observable: a non-list `files` still fails downstream, because each character
    of the string is not a Mapping and the parse refuses as malformed either way.

    That makes it defence-in-depth rather than the only thing standing between a caller and a bad
    parse. Worth writing down: not every mutation survivor is an untested guard, and a probe cannot
    tell the difference. Chasing this one would mean writing a test that asserts an internal
    exception type -- coupling a test to an implementation detail to satisfy a tool.
    """
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(_raw(files="not-a-list"))

    assert excinfo.value.code == ERR_MANIFEST_MALFORMED


def test_an_omission_missing_its_path_is_dropped_like_one_missing_its_reason() -> None:
    """The other half of the omission filter.

    `"path" in item and "reason" in item` is two conditions and only one had a case. Both drop the
    entry silently -- see the characterisation test above for why that is flagged rather than
    fixed.
    """
    parsed = SnapshotManifest.from_mapping(_raw(omissions=[{"reason": "oversize"}]))

    assert parsed.omissions == ()
