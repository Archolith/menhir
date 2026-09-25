"""Structured lifecycle-receipt validation for Menhir backup operations.

Extracted from ``menhir_schema``: the ``backup-local`` / ``rehearsal`` /
``candidate-accept`` receipt validators, the receipt-to-release binding, the
encrypted-archive promotion check, and the desktop-copy archive binding.
Import through ``menhir_schema``.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import stat

from menhir_schema_release import validate_release
from menhir_schema_support import (
    SCHEMA_VERSION,
    _GENERATION_RE,
    _OPERATION_JOB_ID_RE,
    _parse_utc,
    _require_digest,
    _require_exact_keys,
    _require_fresh,
    _require_sha256,
    _require_str,
    _sha256_file,
    load_strict,
)

_RECEIPT_KINDS = frozenset({"backup-local", "rehearsal", "candidate-accept"})

_RECEIPT_RELEASE_KEYS = frozenset(
    {"release_id", "release_manifest_sha256", "menhir_image_digest",
     "neo4j_image_digest"}
)

_LOCAL_ENCRYPTION_KEYS = frozenset({
    "algorithm", "recipient", "plaintext_archive_sha256", "roundtrip_verified",
})

_LOCAL_ARCHIVES_KEYS = frozenset({
    "retention_target_generations", "retained_generation_count",
    "current_archive_path", "archives",
})

_LOCAL_ARCHIVE_ENTRY_KEYS = frozenset({
    "generation", "path", "sha256", "size",
})

_DESKTOP_ARCHIVE_KEYS = frozenset({
    "schema", "kind", "generation", "release", "archive",
    "desktop_destination", "archived_utc",
})
_DESKTOP_RELEASE_KEYS = frozenset({"release_id", "release_manifest_sha256"})
_DESKTOP_ARCHIVE_ENTRY_KEYS = frozenset({"sha256", "size_bytes"})


def _validate_receipt_release(release, label):
    _require_exact_keys(release, _RECEIPT_RELEASE_KEYS, label)
    _require_str(release.get("release_id"), "%s.release_id" % label)
    _require_sha256(release.get("release_manifest_sha256"),
                    "%s.release_manifest_sha256" % label)
    _require_digest(release.get("menhir_image_digest"),
                    "%s.menhir_image_digest" % label)
    _require_digest(release.get("neo4j_image_digest"),
                    "%s.neo4j_image_digest" % label)


def validate_receipt(path: str, kind: str) -> dict:
    """Validate a structured lifecycle receipt of the given kind."""
    if kind not in _RECEIPT_KINDS:
        raise ValueError("unknown receipt kind: %r" % kind)
    receipt = load_strict(path)
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a JSON object")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != kind:
        raise ValueError("receipt kind %r != expected %r" % (receipt.get("kind"), kind))
    generation = _require_str(receipt.get("generation"), "receipt.generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("receipt.generation is invalid")
    _require_sha256(receipt.get("manifest_sha256"), "receipt.manifest_sha256")
    _validate_receipt_release(receipt.get("release"), "receipt.release")

    if kind == "backup-local":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "operation_job_id", "generation",
            "manifest_sha256", "release",
            "encryption", "local_encrypted_archives", "plaintext_removed", "checked_utc",
        }), "backup-local receipt")
        operation_job_id = _require_str(
            receipt.get("operation_job_id"), "backup-local.operation_job_id"
        )
        if not _OPERATION_JOB_ID_RE.match(operation_job_id):
            raise ValueError("backup-local.operation_job_id is invalid")
        encryption = receipt.get("encryption")
        _require_exact_keys(encryption, _LOCAL_ENCRYPTION_KEYS, "encryption")
        if encryption.get("algorithm") != "age-x25519":
            raise ValueError("encryption.algorithm must be age-x25519")
        recipient = _require_str(encryption.get("recipient"), "encryption.recipient")
        if not recipient.startswith("age1"):
            raise ValueError("encryption.recipient must be an age recipient")
        _require_sha256(encryption.get("plaintext_archive_sha256"),
                        "encryption.plaintext_archive_sha256")
        if encryption.get("roundtrip_verified") is not True:
            raise ValueError("encryption.roundtrip_verified must be true")

        local_archives = receipt.get("local_encrypted_archives")
        _require_exact_keys(local_archives, _LOCAL_ARCHIVES_KEYS,
                            "local_encrypted_archives")
        retention_target = local_archives.get("retention_target_generations")
        if not isinstance(retention_target, int) or isinstance(retention_target, bool) \
                or retention_target < 1:
            raise ValueError("local encrypted retention target must be positive")
        archives = local_archives.get("archives")
        if not isinstance(archives, list) or not archives:
            raise ValueError("local_encrypted_archives.archives must be a non-empty list")
        archive_generations = set()
        archive_paths = set()
        current_matches = []
        current_archive_path = _require_str(
            local_archives.get("current_archive_path"),
            "local_encrypted_archives.current_archive_path",
        )
        if not (posixpath.isabs(current_archive_path) or ntpath.isabs(current_archive_path)):
            raise ValueError("local_encrypted_archives.current_archive_path must be absolute")
        for index, archive in enumerate(archives):
            label = "local_encrypted_archives.archives[%d]" % index
            _require_exact_keys(archive, _LOCAL_ARCHIVE_ENTRY_KEYS, label)
            archive_generation = _require_str(archive.get("generation"),
                                              "%s.generation" % label)
            if not _GENERATION_RE.match(archive_generation):
                raise ValueError("%s.generation is invalid" % label)
            local_path = _require_str(archive.get("path"), "%s.path" % label)
            if not (posixpath.isabs(local_path) or ntpath.isabs(local_path)):
                raise ValueError("%s.path must be absolute" % label)
            if not os.path.basename(local_path).startswith(archive_generation + "-") or \
                    not local_path.endswith(".tar.gz.age"):
                raise ValueError("%s.path must bind its generation archive name" % label)
            if local_path in archive_paths:
                raise ValueError("local encrypted archive paths must be unique")
            archive_paths.add(local_path)
            archive_generations.add(archive_generation)
            _require_sha256(archive.get("sha256"), "%s.sha256" % label)
            if not isinstance(archive.get("size"), int) or \
                    isinstance(archive.get("size"), bool) or archive["size"] <= 0:
                raise ValueError("%s.size must be positive" % label)
            if archive_generation == generation and \
                    local_path == current_archive_path:
                current_matches.append(archive)
        retained_count = local_archives.get("retained_generation_count")
        if not isinstance(retained_count, int) or isinstance(retained_count, bool) or \
                retained_count != len(archive_generations):
            raise ValueError("local retained generation count must match distinct evidence")
        if retained_count < retention_target:
            raise ValueError("local retained generation count is below retention target")
        if not current_matches:
            raise ValueError(
                "local encrypted archive evidence must include the current generation"
            )
        # Pending receipts (written before plaintext removal) carry False;
        # only a finalized receipt with True may gate promotion/retirement.
        if not isinstance(receipt.get("plaintext_removed"), bool):
            raise ValueError("plaintext_removed must be a boolean")
        _parse_utc(receipt.get("checked_utc"), "backup-local.checked_utc")

    elif kind == "rehearsal":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "generation", "manifest_sha256", "release",
            "neo4j_check", "sqlite_integrity", "checked_utc",
        }), "rehearsal receipt")
        if receipt.get("neo4j_check") != "ok":
            raise ValueError("rehearsal.neo4j_check must be 'ok'")
        if receipt.get("sqlite_integrity") != "ok":
            raise ValueError("rehearsal.sqlite_integrity must be 'ok'")
        _require_fresh(receipt.get("checked_utc"), "rehearsal.checked_utc", 86400)

    elif kind == "candidate-accept":
        _require_exact_keys(receipt, frozenset({
            "schema", "kind", "generation", "manifest_sha256", "release",
            "readyz", "oauth_discovery", "recall", "mutation_503",
            "tier_tool_identity", "authority_before_digest",
            "authority_after_digest", "same_host_writer_fence_sha256",
            "checked_utc",
        }), "candidate-accept receipt")
        for key in ("readyz", "oauth_discovery", "mutation_503",
                    "tier_tool_identity"):
            if receipt.get(key) != "ok":
                raise ValueError("candidate-accept.%s must be 'ok'" % key)
        if receipt.get("recall") not in ("ok", "skipped"):
            raise ValueError("candidate-accept.recall must be 'ok' or 'skipped'")
        _require_sha256(receipt.get("authority_before_digest"),
                        "authority_before_digest")
        _require_sha256(receipt.get("authority_after_digest"),
                        "authority_after_digest")
        _require_sha256(receipt.get("same_host_writer_fence_sha256"),
                        "same_host_writer_fence_sha256")
        _require_fresh(receipt.get("checked_utc"), "candidate-accept.checked_utc", 900)

    return receipt


def validate_receipt_binding(path: str, kind: str, release_path: str,
                             generation: str, manifest_sha256: str,
                             menhir_digest: str, neo4j_digest: str) -> dict:
    """Validate a receipt and bind it to one exact release and generation."""
    receipt = validate_receipt(path, kind)
    release = validate_release(release_path)
    if receipt.get("generation") != generation:
        raise ValueError("receipt.generation does not match expected value")
    if receipt.get("manifest_sha256") != _require_sha256(
            manifest_sha256, "expected manifest sha256"):
        raise ValueError("receipt.manifest_sha256 does not match expected value")
    binding = receipt["release"]
    checks = {
        "release_id": release["release_id"],
        "release_manifest_sha256": _sha256_file(release_path),
        "menhir_image_digest": _require_digest(menhir_digest, "expected Menhir digest"),
        "neo4j_image_digest": _require_digest(neo4j_digest, "expected Neo4j digest"),
    }
    for key, value in checks.items():
        if binding.get(key) != value:
            raise ValueError("receipt.release.%s does not match release authority" % key)
    if release["images"]["menhir"] != menhir_digest:
        raise ValueError("expected Menhir digest differs from release authority")
    if release["images"]["neo4j"] != neo4j_digest:
        raise ValueError("expected Neo4j digest differs from release authority")
    return receipt


def validate_backup_promotion(
        path: str, archive_root: str = "/srv/menhir/backups/encrypted") -> dict:
    """Validate promotion evidence against the encrypted files that exist now."""
    receipt = validate_receipt(path, "backup-local")
    if receipt.get("plaintext_removed") is not True:
        raise ValueError("plaintext removal must be confirmed before promotion")
    local_archives = receipt["local_encrypted_archives"]
    if local_archives["retention_target_generations"] < 2:
        raise ValueError("promotion requires two retained encrypted generations")
    _require_fresh(receipt["checked_utc"], "backup-local.checked_utc", 3600)
    archive_root = os.path.abspath(archive_root)
    root_stat = os.lstat(archive_root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("encrypted archive root must be a non-symlink directory")
    archives = local_archives["archives"]
    for index, archive in enumerate(archives):
        archive_path = os.path.abspath(archive["path"])
        if os.path.dirname(archive_path) != archive_root:
            raise ValueError("encrypted archive is outside the fixed archive root")
        info = os.lstat(archive_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError("encrypted archive must be a regular non-symlink file")
        if info.st_size != archive["size"]:
            raise ValueError("encrypted archive size does not match receipt")
        if _sha256_file(archive_path) != archive["sha256"]:
            raise ValueError("encrypted archive digest does not match receipt")
    return receipt


def validate_desktop_archive(
        path: str, backup_path: str, release_path: str,
        archive_root: str = "/srv/menhir/backups/encrypted") -> dict:
    """Bind a fresh desktop-copy receipt to the live backup and release authority."""
    receipt = load_strict(path)
    _require_exact_keys(receipt, _DESKTOP_ARCHIVE_KEYS, "desktop archive receipt")
    if receipt.get("schema") != SCHEMA_VERSION:
        raise ValueError("desktop archive receipt schema must be %d" % SCHEMA_VERSION)
    if receipt.get("kind") != "menhir-desktop-archive":
        raise ValueError("desktop archive receipt kind is invalid")
    generation = _require_str(receipt.get("generation"), "desktop archive generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("desktop archive generation is invalid")
    release_binding = receipt.get("release")
    _require_exact_keys(release_binding, _DESKTOP_RELEASE_KEYS, "desktop archive release")
    _require_str(release_binding.get("release_id"), "desktop archive release_id")
    _require_sha256(release_binding.get("release_manifest_sha256"),
                    "desktop archive release manifest")
    archive = receipt.get("archive")
    _require_exact_keys(archive, _DESKTOP_ARCHIVE_ENTRY_KEYS, "desktop archive")
    _require_sha256(archive.get("sha256"), "desktop archive sha256")
    size = archive.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("desktop archive size_bytes must be positive")
    destination = _require_str(
        receipt.get("desktop_destination"), "desktop archive destination"
    )
    if not ntpath.isabs(destination):
        raise ValueError("desktop archive destination must be an absolute Windows path")
    _require_fresh(receipt.get("archived_utc"), "desktop archive archived_utc", 3600)

    backup = validate_backup_promotion(backup_path, archive_root)
    release = validate_release(release_path)
    if backup["release"]["release_id"] != release["release_id"] or \
            backup["release"]["release_manifest_sha256"] != _sha256_file(release_path):
        raise ValueError("backup receipt does not bind the release authority")
    current_path = backup["local_encrypted_archives"]["current_archive_path"]
    current = next(
        item for item in backup["local_encrypted_archives"]["archives"]
        if item["path"] == current_path and item["generation"] == backup["generation"]
    )
    expected = {
        "generation": backup["generation"],
        "release_id": release["release_id"],
        "release_manifest_sha256": _sha256_file(release_path),
        "sha256": current["sha256"],
        "size_bytes": current["size"],
    }
    actual = {
        "generation": generation,
        "release_id": release_binding["release_id"],
        "release_manifest_sha256": release_binding["release_manifest_sha256"],
        "sha256": archive["sha256"],
        "size_bytes": size,
    }
    if actual != expected:
        raise ValueError("desktop archive receipt does not bind the current backup and release")
    return receipt
