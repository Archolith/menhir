"""Shared strict-JSON primitives and field validators for the Menhir schemas.

Extracted from ``menhir_schema``: the duplicate-key-rejecting JSON loader, the
``_require_*`` field validators, timestamp freshness checks, digest helpers,
the generation-directory walkers, and the shared regex contracts. These are
internal implementation details; import them through ``menhir_schema``, which
re-exports the public surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1

_GENERATION_RE = re.compile(r"^generation\.[A-Za-z0-9]+$")
_OPERATION_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Safe, monotonic release identity shape. Every release_id MUST match so a
# caller-supplied label cannot smuggle path/traversal/metacharacter content
# into the immutable authority record and so rollback ordering is unambiguous.
_RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")

# Source and external evidence use Ed25519. Only raw public keys are release
# inputs; source/worker private keys never exist on the target VPS.
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate object key: %r" % key)
        out[key] = value
    return out


def load_strict(path: str) -> dict:
    """Load a JSON document, rejecting duplicate object keys."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_reject_duplicates)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_str(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError("%s must be a non-empty string" % label)
    return value


def _parse_utc(value, label):
    value = _require_str(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("%s must be an ISO-8601 timestamp" % label) from exc
    if parsed.tzinfo is None:
        raise ValueError("%s must include a timezone" % label)
    return parsed.astimezone(timezone.utc)


def _require_fresh(value, label, max_age_seconds):
    parsed = _parse_utc(value, label)
    now = datetime.now(timezone.utc)
    if parsed > now + timedelta(seconds=60):
        raise ValueError("%s is in the future" % label)
    if parsed < now - timedelta(seconds=max_age_seconds):
        raise ValueError("%s is stale" % label)
    return parsed


def _require_sha256(value, label):
    value = _require_str(value, label)
    if not _SHA256_RE.match(value):
        raise ValueError("%s must be a 64-char lowercase sha256" % label)
    return value


def release_authority_sha256(release: dict) -> str:
    """Digest every release claim except the security review itself."""
    if not isinstance(release, dict):
        raise ValueError("release authority must be a JSON object")
    authority = dict(release)
    authority.pop("security_review", None)
    payload = json.dumps(
        authority, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _require_digest(value, label):
    value = _require_str(value, label)
    if not _DIGEST_RE.match(value):
        raise ValueError("%s must be digest-pinned (sha256:<64hex>)" % label)
    return value


def _require_release_id(value, label):
    value = _require_str(value, label)
    if not _RELEASE_ID_RE.match(value):
        raise ValueError(
            "%s violates the safe release_id contract "
            "(menhir-prod-<major>.<minor>.<patch>-<seq>)" % label
        )
    return value


def _require_key_id(value, label):
    value = _require_str(value, label)
    if len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ValueError("%s must be a safe bounded key id" % label)
    return value


def _require_exact_keys(mapping: dict, allowed: frozenset, label: str) -> None:
    if not isinstance(mapping, dict):
        raise ValueError("%s must be a JSON object" % label)
    actual = set(mapping)
    extra = actual - allowed
    missing = allowed - actual
    if extra or missing:
        details = []
        if missing:
            details.append("missing label(s): %s" % ", ".join(sorted(missing)))
        if extra:
            details.append("unknown label(s): %s" % ", ".join(sorted(extra)))
        raise ValueError("%s has invalid labels; %s" % (label, "; ".join(details)))


def _reject_symlinks_and_special(root: str) -> None:
    """Refuse symlinks and special entries anywhere inside the tree."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not (os.path.isfile(full) or os.path.isdir(full)):
                raise ValueError(
                    "generation contains symlink or special entry: %s"
                    % os.path.relpath(full, root).replace(os.sep, "/"))


def _walk_regular_files(root: str):
    _reject_symlinks_and_special(root)
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and not os.path.islink(full):
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                result.append(rel)
    return result


def _validate_release_binding(document: dict, label: str) -> None:
    _require_str(document.get("release_id"), "%s.release_id" % label)
    _require_sha256(document.get("release_manifest_sha256"),
                    "%s.release_manifest_sha256" % label)


def _decode_b64url(value: object, label: str, expected_length: int) -> bytes:
    import base64
    if not isinstance(value, str) or not value or "=" in value or not _B64URL_RE.fullmatch(value):
        raise ValueError("%s must be canonical unpadded base64url" % label)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("%s is invalid base64url" % label) from exc
    canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(raw) != expected_length or canonical != value:
        raise ValueError("%s has invalid length or encoding" % label)
    return raw


def _decode_ed25519_public_key(value: object, label: str) -> bytes:
    return _decode_b64url(value, label, 32)
