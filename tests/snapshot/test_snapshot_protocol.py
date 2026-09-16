"""P0 contract tests: canonical manifest, tree digest, path rules, and limit arithmetic.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`.

These pin the wire contract itself. A change that breaks one of them is a protocol version bump,
not a test fix.
"""

from __future__ import annotations

import json

import pytest

from menhir.snapshot.protocol import (
    ERR_LIMIT_FILE_BYTES,
    ERR_MANIFEST_BYTES_MISMATCH,
    ERR_MANIFEST_CASE_COLLISION,
    ERR_MANIFEST_COUNT_MISMATCH,
    ERR_MANIFEST_DIGEST_MISMATCH,
    ERR_MANIFEST_DUPLICATE_PATH,
    ERR_MANIFEST_UNSUPPORTED_VERSION,
    ERR_PATH_ABSOLUTE,
    ERR_PATH_BACKSLASH,
    ERR_PATH_CONTROL_CHAR,
    ERR_PATH_DRIVE,
    ERR_PATH_EMPTY_SEGMENT,
    ERR_PATH_RESERVED_NAME,
    ERR_PATH_SEGMENT_TOO_LONG,
    ERR_PATH_TRAILING_DOT_SPACE,
    ERR_PATH_TRAVERSAL,
    PROVISIONAL_LIMITS,
    BundleFormatError,
    BundlePathError,
    FileRecord,
    GitProvenance,
    Omission,
    OmissionReason,
    SnapshotLimits,
    SnapshotManifest,
    b64_encoded_len,
    chunk_count,
    collision_key,
    compute_tree_digest,
    normalize_bundle_path,
    sha256_hex,
)

pytestmark = pytest.mark.unit


def _record(path: str, body: bytes = b"x", executable: bool = False) -> FileRecord:
    return FileRecord(
        path=path, size=len(body), sha256=sha256_hex(body), executable=executable
    )


# --- path rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["a.py", "src/menhir/main.py", "a/b/c/d.txt", ".gitignore", "dir.with.dots/file"],
)
def test_normalize_accepts_relative_posix_paths(path: str) -> None:
    assert normalize_bundle_path(path) == path


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/etc/passwd", ERR_PATH_ABSOLUTE),
        ("C:/Users/x", ERR_PATH_DRIVE),
        ("C:file", ERR_PATH_DRIVE),
        ("src\\menhir\\main.py", ERR_PATH_BACKSLASH),
        ("../outside", ERR_PATH_TRAVERSAL),
        ("a/../../b", ERR_PATH_TRAVERSAL),
        ("a/./b", ERR_PATH_TRAVERSAL),
        ("a//b", ERR_PATH_EMPTY_SEGMENT),
        ("a/b/", ERR_PATH_EMPTY_SEGMENT),
        ("a\x00b", ERR_PATH_CONTROL_CHAR),
        ("a\nb", ERR_PATH_CONTROL_CHAR),
        ("con", ERR_PATH_RESERVED_NAME),
        ("dir/NUL.txt", ERR_PATH_RESERVED_NAME),
        ("dir/com4", ERR_PATH_RESERVED_NAME),
        ("trailing.", ERR_PATH_TRAILING_DOT_SPACE),
        ("trailing ", ERR_PATH_TRAILING_DOT_SPACE),
    ],
)
def test_normalize_refuses_unrepresentable_paths(path: str, code: str) -> None:
    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path(path)
    assert excinfo.value.code == code


def test_unc_path_is_refused() -> None:
    # `//server/share/x` splits into an empty first segment; either refusal is correct, what
    # matters is that it never becomes a bundle path.
    with pytest.raises(BundlePathError):
        normalize_bundle_path("//server/share/x")


def test_rejected_path_is_not_in_the_exception_message() -> None:
    """Invariant 5: a server that logs a rejection must not thereby log a source path."""
    secret = "src/very-secret-project-name/file.py"
    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path("/" + secret)
    assert secret not in str(excinfo.value)
    assert excinfo.value.path == "/" + secret


def test_segment_length_is_bounded() -> None:
    long_segment = "a" * 300
    with pytest.raises(BundlePathError) as excinfo:
        normalize_bundle_path(f"src/{long_segment}/x.py")
    assert excinfo.value.code == ERR_PATH_SEGMENT_TOO_LONG


def test_backslash_is_refused_not_translated() -> None:
    """`a\\b` is one legal POSIX filename; translating it would merge it with `a/b`."""
    with pytest.raises(BundlePathError):
        normalize_bundle_path("a\\b")
    assert normalize_bundle_path("a/b") == "a/b"


# --- collision folding -------------------------------------------------------------------------


def test_collision_key_folds_case_and_unicode_form() -> None:
    assert collision_key("src/Main.py") == collision_key("SRC/main.PY")
    # NFC vs NFD of "é"
    assert collision_key("caf\u00e9.txt") == collision_key("cafe\u0301.txt")


# --- tree digest -------------------------------------------------------------------------------


def test_tree_digest_is_stable_and_order_independent_after_build() -> None:
    records = [_record("b.py", b"two"), _record("a.py", b"one")]
    first = SnapshotManifest.build(display_name="p", files=records)
    second = SnapshotManifest.build(display_name="p", files=list(reversed(records)))
    assert first.tree_digest == second.tree_digest
    assert [r.path for r in first.files] == ["a.py", "b.py"]


def test_tree_digest_covers_content_size_and_executable_bit() -> None:
    base = [_record("a.py", b"one")]
    assert compute_tree_digest(base) != compute_tree_digest([_record("a.py", b"other")])
    assert compute_tree_digest(base) != compute_tree_digest([_record("b.py", b"one")])
    assert compute_tree_digest(base) != compute_tree_digest(
        [_record("a.py", b"one", executable=True)]
    )


def test_tree_digest_ignores_name_ids_and_head() -> None:
    """The same bytes digest the same however the snapshot is labelled."""
    files = [_record("a.py", b"one")]
    plain = SnapshotManifest.build(display_name="alpha", files=files)
    labelled = SnapshotManifest.build(
        display_name="beta",
        files=files,
        project_id="proj-1",
        source_id="src-1",
        provenance=GitProvenance(base_commit="deadbeef" * 5, dirty=True),
    )
    assert plain.tree_digest == labelled.tree_digest


def test_digest_framing_resists_field_run_together() -> None:
    """NUL framing: `a` + `bc` must not hash like `ab` + `c` through concatenation."""
    left = compute_tree_digest([_record("a", b"x"), _record("bc", b"y")])
    right = compute_tree_digest([_record("ab", b"x"), _record("c", b"y")])
    assert left != right


# --- canonical serialization -------------------------------------------------------------------


def test_canonical_bytes_are_byte_identical_across_builds() -> None:
    files = [_record("b.py", b"two"), _record("a.py", b"one")]
    first = SnapshotManifest.build(display_name="p", files=files).to_canonical_bytes()
    second = SnapshotManifest.build(
        display_name="p", files=list(reversed(files))
    ).to_canonical_bytes()
    assert first == second


def test_canonical_bytes_have_sorted_keys_and_no_padding() -> None:
    raw = SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes()
    assert b", " not in raw and b": " not in raw
    decoded = json.loads(raw)
    assert list(decoded) == sorted(decoded)


def test_manifest_has_no_partial_index_field() -> None:
    """Invariant 4: completeness is the server's finding, so the client cannot state it."""
    decoded = json.loads(
        SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes()
    )
    assert "partial_index" not in decoded


def test_omissions_are_declared_and_sorted() -> None:
    manifest = SnapshotManifest.build(
        display_name="p",
        files=[_record("a.py")],
        omissions=[
            Omission("vendor/lib", OmissionReason.SUBMODULE),
            Omission("big.bin", OmissionReason.OVERSIZE),
        ],
    )
    decoded = json.loads(manifest.to_canonical_bytes())
    assert decoded["omissions"] == [
        {"path": "big.bin", "reason": "oversize"},
        {"path": "vendor/lib", "reason": "submodule"},
    ]


# --- untrusted parse ---------------------------------------------------------------------------


def _roundtrip(manifest: SnapshotManifest) -> SnapshotManifest:
    return SnapshotManifest.from_mapping(json.loads(manifest.to_canonical_bytes()))


def test_roundtrip_preserves_records_and_digest() -> None:
    manifest = SnapshotManifest.build(
        display_name="p",
        files=[_record("a.py", b"one"), _record("b/c.py", b"two", executable=True)],
        project_id="proj-1",
        deleted_count=3,
    )
    parsed = _roundtrip(manifest)
    assert parsed.tree_digest == manifest.tree_digest
    assert parsed.files == manifest.files
    assert parsed.project_id == "proj-1"
    assert parsed.deleted_count == 3


def test_parse_refuses_declared_count_that_does_not_match() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["file_count"] = 99
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_COUNT_MISMATCH


def test_parse_refuses_declared_bytes_that_do_not_match() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["total_bytes"] = 4096
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_BYTES_MISMATCH


def test_parse_refuses_a_forged_tree_digest() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["tree_digest"] = "sha256:" + "0" * 64
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_DIGEST_MISMATCH


def test_parse_refuses_a_traversal_path_however_it_was_declared() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["files"][0]["path"] = "../../etc/passwd"
    with pytest.raises(BundlePathError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_PATH_TRAVERSAL


def test_parse_refuses_duplicate_paths() -> None:
    raw = json.loads(
        SnapshotManifest.build(display_name="p", files=[_record("a.py"), _record("b.py")]).to_canonical_bytes()
    )
    raw["files"][1]["path"] = "a.py"
    with pytest.raises(BundlePathError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_DUPLICATE_PATH


def test_parse_refuses_case_colliding_paths() -> None:
    raw = json.loads(
        SnapshotManifest.build(display_name="p", files=[_record("a.py"), _record("b.py")]).to_canonical_bytes()
    )
    raw["files"][1]["path"] = "A.py"
    with pytest.raises(BundlePathError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_CASE_COLLISION


def test_parse_refuses_an_unsupported_protocol_version() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["protocol_version"] = 99
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw)
    assert excinfo.value.code == ERR_MANIFEST_UNSUPPORTED_VERSION


def test_parse_refuses_a_file_over_the_size_limit_before_trusting_it() -> None:
    tiny = SnapshotLimits(max_file_bytes=4)
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py", b"12345")]).to_canonical_bytes())
    with pytest.raises(BundleFormatError) as excinfo:
        SnapshotManifest.from_mapping(raw, tiny)
    assert excinfo.value.code == ERR_LIMIT_FILE_BYTES


def test_parse_refuses_a_malformed_sha() -> None:
    raw = json.loads(SnapshotManifest.build(display_name="p", files=[_record("a.py")]).to_canonical_bytes())
    raw["files"][0]["sha256"] = "NOTHEX"
    with pytest.raises(BundleFormatError):
        SnapshotManifest.from_mapping(raw)


# --- limit arithmetic --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decoded", "encoded"),
    [(0, 0), (1, 4), (2, 4), (3, 4), (4, 8), (6, 8), (7, 12), (1024, 1368)],
)
def test_base64_expansion_is_exact(decoded: int, encoded: int) -> None:
    assert b64_encoded_len(decoded) == encoded


def test_base64_expansion_matches_the_stdlib() -> None:
    import base64

    for size in (0, 1, 2, 3, 5, 100, 4096):
        assert b64_encoded_len(size) == len(base64.b64encode(b"a" * size))


@pytest.mark.parametrize(
    ("total", "chunk", "expected"),
    [(0, 1024, 0), (1, 1024, 1), (1024, 1024, 1), (1025, 1024, 2), (4096, 1024, 4)],
)
def test_chunk_count_covers_every_byte(total: int, chunk: int, expected: int) -> None:
    assert chunk_count(total, chunk) == expected


def test_chunk_count_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        chunk_count(10, 0)
    with pytest.raises(ValueError):
        chunk_count(-1, 10)


def test_chunk_ceiling_is_enforced_before_allocation() -> None:
    limits = SnapshotLimits(max_chunk_bytes=1024)
    limits.validate_chunk_bytes(1024)
    with pytest.raises(BundleFormatError):
        limits.validate_chunk_bytes(1025)


def test_owner_approved_pilot_quotas_are_pinned() -> None:
    """Decided 2026-09-16, not derived. A silent edit here changes an approved envelope."""
    assert PROVISIONAL_LIMITS.max_compressed_bytes == 64 * 1024 * 1024
    assert PROVISIONAL_LIMITS.max_total_bytes == 256 * 1024 * 1024
    assert PROVISIONAL_LIMITS.max_file_count == 20_000


def test_provisional_limits_are_internally_consistent() -> None:
    """The negotiated default must fit inside the hard ceiling, and files inside the total."""
    limits = PROVISIONAL_LIMITS
    assert limits.chunk_bytes <= limits.max_chunk_bytes
    assert limits.max_file_bytes <= limits.max_total_bytes
    assert limits.max_compressed_bytes <= limits.max_total_bytes
    assert limits.max_segment_bytes <= limits.max_path_bytes
