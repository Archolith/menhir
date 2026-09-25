"""Content hashing and chunk arithmetic for the remote snapshot wire contract.

Split out of :mod:`menhir.snapshot.protocol` (the wire-contract facade), which re-exports every
public name defined here; import these from the facade, not from this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from menhir.snapshot.protocol_records import FileRecord

__all__ = [
    "b64_encoded_len",
    "chunk_count",
    "compute_tree_digest",
    "sha256_hex",
    "sort_records",
]

#: Domain separator for `tree_digest`. Present so the digest of a file list can never collide with
#: some other sha256 in this system that happens to hash similar bytes.
_TREE_DIGEST_DOMAIN = b"menhir-snapshot-tree-v1\n"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
