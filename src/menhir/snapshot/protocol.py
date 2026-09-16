"""Wire contract for MCP-bundled remote project snapshots.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P0 -- freeze the protocol).

This module is the ONE definition of the snapshot format, shared by the client bundler and, from
P3, by the server-side extractor. Both halves must agree byte-for-byte on what a path may be, how
the manifest is ordered, and how ``tree_digest`` is computed, so neither side re-derives those
rules locally.

Three contract decisions are load-bearing and easy to undo by accident:

**Paths are rejected, never repaired.** ``normalize_bundle_path`` refuses a backslash rather than
translating it to ``/``: a backslash is a legal character in a POSIX filename, so translating it
would silently merge two distinct files into one bundle entry. Every other refusal (absolute,
``..``, drive/UNC, control characters, Windows-reserved names) follows the same rule -- the bundle
carries what the repository actually holds or it carries nothing.

**Offending paths never enter an exception message.** Plan invariant 5 forbids manifest path lists
in telemetry, logs, errors and traces, and a server that logs a rejection would do exactly that.
:class:`BundlePathError` therefore carries the code in its message and the path only as an
attribute, which the CLI may print on the user's own machine and the server must not. Anything
formatting one of these server-side should use ``.code``.

**The client never states completeness.** There is deliberately no ``partial_index`` field here
(invariant 4). The server derives it by scanning the bytes it extracted. What the client MAY state
is what it left out on purpose -- see :class:`Omission` -- because an omission the server cannot
observe (a submodule that was never uploaded) would otherwise read as deletion.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUNDLE_POLICY_VERSION",
    "CONTENT_PREFIX",
    "MANIFEST_NAME",
    "PROVISIONAL_LIMITS",
    "SNAPSHOT_PROTOCOL_VERSION",
    "BundleFormatError",
    "BundlePathError",
    "FileRecord",
    "Omission",
    "OmissionReason",
    "SnapshotLimits",
    "SnapshotManifest",
    "b64_encoded_len",
    "chunk_count",
    "collision_key",
    "compute_tree_digest",
    "normalize_bundle_path",
    "sha256_hex",
]

#: Bumped when the wire shape changes in a way an older peer cannot read. The server advertises
#: the versions it accepts; a client that cannot meet one refuses locally rather than uploading.
SNAPSHOT_PROTOCOL_VERSION = 1

#: Bumped when the SELECTION policy changes (what is included, excluded, or refused) without the
#: wire shape changing. Recorded in the manifest so a snapshot's content rules are attributable
#: after the fact -- two bundles with the same `tree_digest` but different policy versions were
#: produced by different rules and only coincidentally agree.
BUNDLE_POLICY_VERSION = 1

MANIFEST_NAME = "snapshot.json"
CONTENT_PREFIX = "content/"

#: Domain separator for `tree_digest`. Present so the digest of a file list can never collide with
#: some other sha256 in this system that happens to hash similar bytes.
_TREE_DIGEST_DOMAIN = b"menhir-snapshot-tree-v1\n"

# --- stable machine-readable error codes -------------------------------------------------------
# These cross the wire and appear in status results, so they are API. Add, never repurpose.
ERR_PATH_EMPTY = "snapshot.path.empty"
ERR_PATH_ABSOLUTE = "snapshot.path.absolute"
ERR_PATH_DRIVE = "snapshot.path.drive_or_unc"
ERR_PATH_BACKSLASH = "snapshot.path.backslash"
ERR_PATH_TRAVERSAL = "snapshot.path.traversal"
ERR_PATH_EMPTY_SEGMENT = "snapshot.path.empty_segment"
ERR_PATH_CONTROL_CHAR = "snapshot.path.control_character"
ERR_PATH_TOO_LONG = "snapshot.path.too_long"
ERR_PATH_SEGMENT_TOO_LONG = "snapshot.path.segment_too_long"
ERR_PATH_RESERVED_NAME = "snapshot.path.windows_reserved_name"
ERR_PATH_TRAILING_DOT_SPACE = "snapshot.path.trailing_dot_or_space"
ERR_MANIFEST_DUPLICATE_PATH = "snapshot.manifest.duplicate_path"
ERR_MANIFEST_CASE_COLLISION = "snapshot.manifest.case_collision"
ERR_MANIFEST_COUNT_MISMATCH = "snapshot.manifest.file_count_mismatch"
ERR_MANIFEST_BYTES_MISMATCH = "snapshot.manifest.total_bytes_mismatch"
ERR_MANIFEST_DIGEST_MISMATCH = "snapshot.manifest.tree_digest_mismatch"
ERR_MANIFEST_MALFORMED = "snapshot.manifest.malformed"
ERR_MANIFEST_UNSUPPORTED_VERSION = "snapshot.manifest.unsupported_protocol_version"
ERR_LIMIT_FILE_COUNT = "snapshot.limit.file_count"
ERR_LIMIT_TOTAL_BYTES = "snapshot.limit.total_bytes"
ERR_LIMIT_FILE_BYTES = "snapshot.limit.file_bytes"
ERR_LIMIT_COMPRESSED_BYTES = "snapshot.limit.compressed_bytes"
ERR_LIMIT_EXPANSION_RATIO = "snapshot.limit.expansion_ratio"
ERR_LIMIT_CHUNK_BYTES = "snapshot.limit.chunk_bytes"

#: Windows device names. A bundle containing one cannot be extracted on a Windows host at all, so
#: it is refused at build time rather than discovered at extraction. Mirrors
#: `project_scanner._WINDOWS_RESERVED`; kept here because this module must not import the scanner
#: (the server extractor uses it before any scan exists).
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class BundlePathError(ValueError):
    """A path that may not appear in a bundle.

    ``str(exc)`` is the stable code ALONE. The rejected path is available as ``.path`` for local,
    user-facing client output; server-side code must not put it in a log, error body or metric
    label (plan invariant 5).
    """

    def __init__(self, code: str, path: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.path = path
        self.detail = detail


class BundleFormatError(ValueError):
    """A manifest or archive that violates the contract. Same message discipline as above."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SnapshotLimits:
    """Bounds enforced before allocation and again after decode/extraction (invariant 6).

    **Every value here is PROVISIONAL.** Plan P0 requires measuring the real MCP/Cloudflare path
    (64 KiB / 256 KiB / 1 MiB / 2 MiB binary chunks) and setting `chunk_bytes`, the quota and the
    TTL from that evidence; the plan says in as many words that 1 MiB is a test point and not an
    unreviewed constant. These defaults exist so the contract tests have arithmetic to exercise
    and the CLI can estimate an upload -- they are not an approved operating envelope, and P2 must
    not ship until an owner sets them (open decision 1).
    """

    #: Decoded bytes per chunk. The encoded call is ~4/3 of this plus envelope.
    chunk_bytes: int = 256 * 1024
    #: Hard ceiling on a single decoded chunk, independent of the negotiated size.
    max_chunk_bytes: int = 2 * 1024 * 1024
    #: Files in one bundle.
    max_file_count: int = 20_000
    #: One file's bytes. Larger files become a declared omission rather than a silent drop.
    max_file_bytes: int = 8 * 1024 * 1024
    #: Sum of file bytes (pre-compression).
    max_total_bytes: int = 256 * 1024 * 1024
    #: The uploaded archive itself.
    max_compressed_bytes: int = 64 * 1024 * 1024
    #: Zip-bomb guard: expanded bytes divided by compressed bytes, checked during extraction.
    max_expansion_ratio: float = 20.0
    #: One path, UTF-8 bytes.
    max_path_bytes: int = 1024
    #: One path segment, UTF-8 bytes. 255 is the common filesystem ceiling.
    max_segment_bytes: int = 255

    def validate_chunk_bytes(self, decoded_len: int) -> None:
        """Refuse a chunk larger than the hard ceiling, before any allocation."""
        if decoded_len < 0 or decoded_len > self.max_chunk_bytes:
            raise BundleFormatError(
                ERR_LIMIT_CHUNK_BYTES,
                f"{decoded_len} bytes exceeds max_chunk_bytes={self.max_chunk_bytes}",
            )


#: The provisional envelope. Import this rather than constructing SnapshotLimits() ad hoc, so a
#: measured update lands in one place.
PROVISIONAL_LIMITS = SnapshotLimits()


class OmissionReason:
    """Why a tracked path is absent from the bundle on purpose.

    The distinction that matters to the server: an omission is NOT a deletion. The extracted tree
    cannot tell them apart by itself, so a snapshot that omits a submodule would otherwise look
    like a snapshot in which those files were removed -- and structure writes prune.
    """

    SUBMODULE = "submodule"
    SYMLINK = "symlink"
    OVERSIZE = "oversize"
    EXCLUDED_DIR = "excluded_dir"
    RECEIPT = "local_receipt"
    SECRET_RISK = "secret_risk"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class FileRecord:
    """One regular file in the bundle."""

    path: str
    size: int
    sha256: str
    executable: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class Omission:
    """A declared, deliberate absence."""

    path: str
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_bundle_path(raw: str, limits: SnapshotLimits = PROVISIONAL_LIMITS) -> str:
    """Return *raw* as a bundle path, or raise :class:`BundlePathError`.

    Accepts only a relative POSIX path of non-empty segments. Rejection is the whole point; see
    the module docstring for why nothing here is repaired.
    """
    if not raw:
        raise BundlePathError(ERR_PATH_EMPTY, raw)
    if "\\" in raw:
        raise BundlePathError(ERR_PATH_BACKSLASH, raw)
    if _CONTROL_CHARS.search(raw):
        raise BundlePathError(ERR_PATH_CONTROL_CHAR, raw)
    if raw.startswith("/"):
        raise BundlePathError(ERR_PATH_ABSOLUTE, raw)
    # `C:` style drives and `//server/share` UNC. Checked before segment splitting because a
    # drive-relative path ("C:file") has no leading separator to catch it.
    if re.match(r"^[A-Za-z]:", raw):
        raise BundlePathError(ERR_PATH_DRIVE, raw)
    if len(raw.encode("utf-8")) > limits.max_path_bytes:
        raise BundlePathError(ERR_PATH_TOO_LONG, raw)

    segments = raw.split("/")
    for segment in segments:
        if segment == "":
            # Covers a trailing slash, a leading slash already caught above, and `a//b`.
            raise BundlePathError(ERR_PATH_EMPTY_SEGMENT, raw)
        if segment in (".", ".."):
            raise BundlePathError(ERR_PATH_TRAVERSAL, raw)
        if len(segment.encode("utf-8")) > limits.max_segment_bytes:
            raise BundlePathError(ERR_PATH_SEGMENT_TOO_LONG, raw)
        if segment[-1] in {".", " "}:
            # Windows silently strips these at creation, so `a.` and `a` would collide after
            # extraction -- a collision the manifest check above could not see.
            raise BundlePathError(ERR_PATH_TRAILING_DOT_SPACE, raw)
        stem = segment.split(".", 1)[0]
        if stem.lower() in _WINDOWS_RESERVED:
            raise BundlePathError(ERR_PATH_RESERVED_NAME, raw)
    return raw


def collision_key(path: str) -> str:
    """Key under which two paths would collide on a case-insensitive or NFD filesystem.

    Windows and macOS both fold case; macOS additionally stores NFD. Two manifest entries sharing
    this key cannot both exist after extraction on those hosts, so the bundle is refused rather
    than extracted into a tree that quietly holds one of them.
    """
    return unicodedata.normalize("NFC", path).casefold()


def compute_tree_digest(records: Sequence[FileRecord]) -> str:
    """Hash the ordered file records. Returns ``sha256:<hex>``.

    Covers path, size, content digest and the executable bit -- and nothing else. Project name,
    ids, HEAD and timestamps are deliberately outside it, so the same bytes produce the same
    digest whatever the snapshot is called or when it was taken. Records must already be sorted
    (:func:`sort_records`); the caller's order is hashed as given so that a mis-ordered manifest
    fails the digest check instead of being silently accepted.
    """
    digest = hashlib.sha256()
    digest.update(_TREE_DIGEST_DOMAIN)
    for record in records:
        digest.update(record.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\0")
        digest.update(b"1" if record.executable else b"0")
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def sort_records(records: Iterable[FileRecord]) -> list[FileRecord]:
    """Canonical order: by UTF-8 bytes of the path.

    Byte order, not locale or code-point order on decoded strings, so two machines with different
    locales produce the same digest.
    """
    return sorted(records, key=lambda r: r.path.encode("utf-8"))


def b64_encoded_len(decoded_len: int) -> int:
    """Length of the standard base64 encoding of *decoded_len* bytes, padding included."""
    if decoded_len < 0:
        raise ValueError("decoded_len must be non-negative")
    return 4 * ((decoded_len + 2) // 3)


def chunk_count(total_bytes: int, chunk_bytes: int) -> int:
    """Number of chunks a bundle of *total_bytes* needs. Zero bytes is zero chunks."""
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if total_bytes < 0:
        raise ValueError("total_bytes must be non-negative")
    return (total_bytes + chunk_bytes - 1) // chunk_bytes


@dataclass(frozen=True)
class SnapshotManifest:
    """``snapshot.json``: the complete description of one snapshot.

    ``project_id`` and ``source_id`` are absent on a first sync and supplied by the server's
    identity settlement; they are carried here so a later sync is addressed by id rather than by
    display name (invariant 3).
    """

    display_name: str
    files: tuple[FileRecord, ...]
    omissions: tuple[Omission, ...] = ()
    project_id: str | None = None
    source_id: str | None = None
    source_head: str | None = None
    protocol_version: int = SNAPSHOT_PROTOCOL_VERSION
    policy_version: int = BUNDLE_POLICY_VERSION
    deleted_count: int = 0
    _tree_digest: str = field(default="", repr=False, compare=False)

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(record.size for record in self.files)

    @property
    def tree_digest(self) -> str:
        return self._tree_digest or compute_tree_digest(self.files)

    @classmethod
    def build(
        cls,
        *,
        display_name: str,
        files: Iterable[FileRecord],
        omissions: Iterable[Omission] = (),
        project_id: str | None = None,
        source_id: str | None = None,
        source_head: str | None = None,
        deleted_count: int = 0,
    ) -> SnapshotManifest:
        """Sort, validate and digest in one step. The only supported way to make a manifest."""
        ordered = tuple(sort_records(files))
        _check_unique(ordered)
        return cls(
            display_name=display_name,
            files=ordered,
            omissions=tuple(sorted(omissions, key=lambda o: (o.reason, o.path))),
            project_id=project_id,
            source_id=source_id,
            source_head=source_head,
            deleted_count=deleted_count,
            _tree_digest=compute_tree_digest(ordered),
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "policy_version": self.policy_version,
            "display_name": self.display_name,
            "project_id": self.project_id,
            "source_id": self.source_id,
            "source_head": self.source_head,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "tree_digest": self.tree_digest,
            "deleted_count": self.deleted_count,
            "files": [record.as_json() for record in self.files],
            "omissions": [omission.as_json() for omission in self.omissions],
        }

    def to_canonical_bytes(self) -> bytes:
        """Serialize deterministically: sorted keys, no insignificant whitespace, UTF-8.

        Two runs over identical inputs must produce identical bytes, because the manifest sits
        inside the archive whose digest the server verifies.
        """
        return json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], limits: SnapshotLimits = PROVISIONAL_LIMITS
    ) -> SnapshotManifest:
        """Parse and fully validate an untrusted manifest.

        This is the server's entry point, so it assumes hostile input: every path is
        re-normalized, uniqueness and case collisions are re-checked, and the declared counts,
        byte total and tree digest must match the records rather than being believed.
        """
        try:
            protocol_version = int(raw["protocol_version"])
            policy_version = int(raw["policy_version"])
            display_name = str(raw["display_name"])
            declared_count = int(raw["file_count"])
            declared_bytes = int(raw["total_bytes"])
            declared_digest = str(raw["tree_digest"])
            raw_files = raw["files"]
            if not isinstance(raw_files, list):
                raise TypeError("files must be a list")
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleFormatError(ERR_MANIFEST_MALFORMED, str(exc)) from exc

        if protocol_version != SNAPSHOT_PROTOCOL_VERSION:
            raise BundleFormatError(
                ERR_MANIFEST_UNSUPPORTED_VERSION, f"protocol_version={protocol_version}"
            )
        if len(raw_files) > limits.max_file_count:
            raise BundleFormatError(ERR_LIMIT_FILE_COUNT, str(len(raw_files)))

        records: list[FileRecord] = []
        for entry in raw_files:
            try:
                path = normalize_bundle_path(str(entry["path"]), limits)
                size = int(entry["size"])
                sha = str(entry["sha256"])
                executable = bool(entry.get("executable", False))
            except BundlePathError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise BundleFormatError(ERR_MANIFEST_MALFORMED, str(exc)) from exc
            if size < 0 or size > limits.max_file_bytes:
                raise BundleFormatError(ERR_LIMIT_FILE_BYTES, str(size))
            if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
                raise BundleFormatError(ERR_MANIFEST_MALFORMED, "sha256 must be 64 lowercase hex")
            records.append(FileRecord(path=path, size=size, sha256=sha, executable=executable))

        ordered = tuple(sort_records(records))
        _check_unique(ordered)

        total = sum(record.size for record in ordered)
        if total > limits.max_total_bytes:
            raise BundleFormatError(ERR_LIMIT_TOTAL_BYTES, str(total))
        if declared_count != len(ordered):
            raise BundleFormatError(ERR_MANIFEST_COUNT_MISMATCH, str(declared_count))
        if declared_bytes != total:
            raise BundleFormatError(ERR_MANIFEST_BYTES_MISMATCH, str(declared_bytes))

        actual_digest = compute_tree_digest(ordered)
        if declared_digest != actual_digest:
            # No values in the detail: a digest is not sensitive, but the mismatch says nothing
            # useful anyway and the code is what a caller acts on.
            raise BundleFormatError(ERR_MANIFEST_DIGEST_MISMATCH)

        omissions = tuple(
            Omission(path=str(item["path"]), reason=str(item["reason"]))
            for item in (raw.get("omissions") or [])
            if isinstance(item, Mapping) and "path" in item and "reason" in item
        )
        return cls(
            display_name=display_name,
            files=ordered,
            omissions=omissions,
            project_id=(str(raw["project_id"]) if raw.get("project_id") else None),
            source_id=(str(raw["source_id"]) if raw.get("source_id") else None),
            source_head=(str(raw["source_head"]) if raw.get("source_head") else None),
            protocol_version=protocol_version,
            policy_version=policy_version,
            deleted_count=int(raw.get("deleted_count") or 0),
            _tree_digest=actual_digest,
        )


def _check_unique(records: Sequence[FileRecord]) -> None:
    """Refuse duplicate paths and paths that collide after case/unicode folding."""
    seen: set[str] = set()
    folded: dict[str, str] = {}
    for record in records:
        if record.path in seen:
            raise BundlePathError(ERR_MANIFEST_DUPLICATE_PATH, record.path)
        seen.add(record.path)
        key = collision_key(record.path)
        if key in folded and folded[key] != record.path:
            raise BundlePathError(ERR_MANIFEST_CASE_COLLISION, record.path)
        folded[key] = record.path
