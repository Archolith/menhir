"""MANIFEST.json validation for Menhir backup generations.

Extracted from ``menhir_schema``: the per-file durability classification
constants and ``validate_manifest``, which enforces exact set equality between
the manifest and the generation directory, per-file classification, content
hashes, and the required authority file list. Import through ``menhir_schema``.
"""

from __future__ import annotations

import os

from menhir_schema_support import (
    SCHEMA_VERSION,
    _COMMIT_RE,
    _GENERATION_RE,
    _parse_utc,
    _require_digest,
    _require_exact_keys,
    _require_sha256,
    _require_str,
    _sha256_file,
    _walk_regular_files,
    load_strict,
)

# Per-file durability classification. Every file in a generation must carry
# exactly one of these; anything unclassified is rejected.
FILE_CLASSES = frozenset({"authority", "secret", "config", "disposable"})

# Marker files that are part of a generation but are NOT themselves enumerated
# in the manifest `files` map (they bind the manifest rather than the data).
_GEN_MARKERS = frozenset({"MANIFEST.json", "SHA256SUMS", "COMPLETE"})

_MANIFEST_TOP_KEYS = frozenset(
    {"schema", "generation", "created_utc", "build", "release", "restore_order",
     "files", "sha256sums_sha256"}
)
_BUILD_KEYS = frozenset(
    {"repo_commit", "menhir_image", "menhir_image_digest",
     "neo4j_image", "neo4j_image_digest"}
)
_RELEASE_KEYS = frozenset({"release_id", "release_manifest_sha256"})

# Required authority files that every complete generation must enumerate.
_REQUIRED_AUTHORITY = frozenset({
    "neo4j/neo4j.dump",
    "neo4j/system.dump",
    "state/oauth/menhir_oauth_as.db",
    "state/telemetry/mcp_telemetry.db",
    "secrets/neo4j/neo4j-auth",
    "secrets/menhir/neo4j-password",
    "secrets/menhir/operator-key",
    "secrets/oauth/oauth_signing_key.json",
    "secrets/oauth/retry-response-keyring.json",
    "secrets/oauth/oauth-consent-secret",
    "policy/client-policy.json",
    "config/docker-compose.production.yml",
    "config/Dockerfile",
    "config/production.env",
    "config/release.json",
    "config/durable-state-inventory.json",
    "config/commit.txt",
})


def validate_manifest(manifest_path: str, root: str) -> dict:
    """Validate a generation MANIFEST.json against the generation directory."""
    manifest = load_strict(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    _require_exact_keys(manifest, _MANIFEST_TOP_KEYS, "manifest")
    if manifest.get("schema") != SCHEMA_VERSION:
        raise ValueError("manifest schema must be %d" % SCHEMA_VERSION)

    generation = _require_str(manifest.get("generation"), "manifest.generation")
    if not _GENERATION_RE.match(generation):
        raise ValueError("manifest.generation is invalid: %r" % generation)
    _parse_utc(manifest.get("created_utc"), "manifest.created_utc")

    build = manifest.get("build")
    _require_exact_keys(build, _BUILD_KEYS, "manifest.build")
    if not _COMMIT_RE.match(_require_str(build.get("repo_commit"), "build.repo_commit")):
        raise ValueError("build.repo_commit must be a 40-char lowercase hex commit")
    _require_digest(build.get("menhir_image_digest"), "build.menhir_image_digest")
    _require_digest(build.get("neo4j_image_digest"), "build.neo4j_image_digest")

    release = manifest.get("release")
    if release is not None:
        _require_exact_keys(release, _RELEASE_KEYS, "manifest.release")
        _require_str(release.get("release_id"), "release.release_id")
        _require_sha256(release.get("release_manifest_sha256"),
                        "release.release_manifest_sha256")

    restore_order = manifest.get("restore_order")
    if not isinstance(restore_order, list) or not restore_order or \
            any(not isinstance(x, str) or not x for x in restore_order):
        raise ValueError("manifest.restore_order must be a non-empty list of strings")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest.files must be a non-empty object")

    # Exact set equality against the generation directory contents.
    declared = set(files)
    actual = set(p for p in _walk_regular_files(root) if p not in _GEN_MARKERS)
    if declared != actual:
        extras = sorted(actual - declared)
        missing = sorted(declared - actual)
        detail = []
        if extras:
            detail.append("undeclared file(s): %s" % ", ".join(extras))
        if missing:
            detail.append("missing file(s): %s" % ", ".join(missing))
        raise ValueError("manifest.files is not exactly equal to the generation "
                         "contents; " + "; ".join(detail))

    # Per-file classification + content hash.
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            raise ValueError("manifest.files[%s] must be an object" % rel)
        _require_exact_keys(entry, frozenset({"sha256", "class"}), "files[%s]" % rel)
        cls = _require_str(entry.get("class"), "files[%s].class" % rel)
        if cls not in FILE_CLASSES:
            raise ValueError("files[%s] has unknown class %r" % (rel, cls))
        expected = _require_sha256(entry.get("sha256"), "files[%s].sha256" % rel)
        actual_hash = _sha256_file(os.path.join(root, rel))
        if actual_hash != expected:
            raise ValueError("files[%s] sha256 mismatch" % rel)

    # Required authority must be present and classified authority/secret/config.
    for rel in _REQUIRED_AUTHORITY:
        if rel not in files:
            raise ValueError("required authority file missing from manifest: %s" % rel)
        if files[rel]["class"] not in ("authority", "secret", "config"):
            raise ValueError("authority file %s must be authority/secret/config, "
                             "not %s" % (rel, files[rel]["class"]))

    if not any(path in files for path in (
            "secrets/menhir/openai-api-key", "secrets/menhir/gemini-api-key",
            "secrets/menhir/local-llm-api-key")):
        raise ValueError("generation must include the selected provider credential authority")

    _require_sha256(manifest.get("sha256sums_sha256"), "manifest.sha256sums_sha256")
    # The hash list must itself be bound by the manifest.
    sha256sums_actual = _sha256_file(os.path.join(root, "SHA256SUMS"))
    if sha256sums_actual != manifest["sha256sums_sha256"]:
        raise ValueError("manifest.sha256sums_sha256 does not bind SHA256SUMS")
    return manifest
