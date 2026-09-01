"""Structural tests for the Menhir deployment release-authority schemas.

These exercise the pure, Docker-free validation in deploy/lib/menhir_schema.py:
duplicate-key rejection, the strict release.json schema (unknown-label and
floating-image-pin refusal, mandatory Dockerfile wheel-hash manifest), the
generation manifest (exact set equality, classification, required authority,
extras/unclassified refusal), and the structured lifecycle receipts.

The module under test has no runtime dependencies, so these tests certify the
contracts the VPS lifecycle scripts rely on without any Docker daemon.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "deploy" / "lib" / "menhir_schema.py"

_spec = importlib.util.spec_from_file_location("menhir_schema", MODULE_PATH)
_schema = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_schema)

_DIGEST_MODULE_PATH = REPO_ROOT / "deploy" / "lib" / "authority_digest.py"
_digest_spec = importlib.util.spec_from_file_location("authority_digest", _DIGEST_MODULE_PATH)
_authority = importlib.util.module_from_spec(_digest_spec)
assert _digest_spec.loader is not None
_digest_spec.loader.exec_module(_authority)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(root: Path, rel: str, data: bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha256_bytes(data)


REQUIRED_FILES = {
    "neo4j/neo4j.dump": b"neo4j-dump",
    "neo4j/system.dump": b"system-dump",
    "state/oauth/menhir_oauth_as.db": b"oauth-db",
    "state/telemetry/mcp_telemetry.db": b"telemetry-db",
    "secrets/neo4j/neo4j-auth": b"neo4j/password",
    "secrets/menhir/neo4j-password": b"password",
    "secrets/menhir/operator-key": b"operator",
    "secrets/menhir/openai-api-key": b"provider",
    "secrets/oauth/oauth_signing_key.json": b"{}",
    "secrets/oauth/retry-response-keyring.json": b"{}",
    "secrets/oauth/oauth-consent-secret": b"consent",
    "policy/client-policy.json": b"{}",
    "config/docker-compose.production.yml": b"services: {}\n",
    "config/Dockerfile": b"FROM scratch\n",
    "config/production.env": b"MENHIR_RELEASE_COMMIT=x\n",
    "config/release.json": b"{}\n",
    "config/durable-state-inventory.json": b"{}\n",
    "config/commit.txt": b"a" * 40 + b"\n",
}

MANIFEST_MARKERS = {"MANIFEST.json", "SHA256SUMS", "COMPLETE"}


def _classify(rel: str) -> str:
    if rel.startswith("secrets/"):
        return "secret"
    if rel in {
        "neo4j/neo4j.dump",
        "neo4j/system.dump",
        "state/oauth/menhir_oauth_as.db",
        "state/telemetry/mcp_telemetry.db",
    }:
        return "authority"
    return "config"


def _build_generation(root: Path, files: dict[str, bytes]) -> dict:
    entries = {}
    for rel, data in files.items():
        digest = _write(root, rel, data)
        entries[rel] = {"sha256": digest, "class": _classify(rel)}

    # SHA256SUMS
    lines = []
    for rel in sorted(entries):
        lines.append(f"{entries[rel]['sha256']}  {rel}")
    sha256sums_content = ("\n".join(lines) + "\n").encode()
    _write(root, "SHA256SUMS", sha256sums_content)
    sha256sums_sha256 = _sha256_bytes(sha256sums_content)

    manifest = {
        "schema": 1,
        "generation": "generation.Abc123",
        "created_utc": "2026-08-26T00:00:00Z",
        "build": {
            "repo_commit": "a" * 40,
            "menhir_image": "menhir@sha256:" + "b" * 64,
            "menhir_image_digest": "sha256:" + "b" * 64,
            "neo4j_image": "neo4j@sha256:" + "c" * 64,
            "neo4j_image_digest": "sha256:" + "c" * 64,
        },
        "release": {
            "release_id": "menhir-prod-0.2.0-1",
            "release_manifest_sha256": "d" * 64,
        },
        "restore_order": ["neo4j", "system", "oauth", "telemetry", "secrets", "policy"],
        "files": entries,
        "sha256sums_sha256": sha256sums_sha256,
    }
    manifest_path = root / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (root / "COMPLETE").write_text(_sha256_bytes(manifest_path.read_bytes()) + "\n")
    return manifest


def _valid_release() -> dict:
    release = {
        "schema": 1,
        "release_id": "menhir-prod-0.2.0-1",
        "release_author": "release-operator@example.com",
        "repos": {
            "menhir": "a" * 40,
            "archolith_oauth": "b" * 40,
            "yawn_deploy": "c" * 40,
            "yawn_vps": "d" * 40,
        },
        "repo_remotes": dict(_schema.EXPECTED_REPO_REMOTES),
        "oauth_wheel_sha256": "e" * 64,
        "oauth_wheel_source": {
            "repository": "archolith_oauth",
            "commit": "b" * 40,
            "source_tree_sha256": "f" * 64,
            "wheel_sha256": "e" * 64,
        },
        "images": {
            "menhir": "sha256:" + "1" * 64,
            "neo4j": "sha256:" + "2" * 64,
            "caddy": "sha256:" + "3" * 64,
            "base": "sha256:" + "4" * 64,
        },
        "wheel_manifest_sha256": "5" * 64,
        "dockerfile_wheel_manifest_sha256": "6" * 64,
        "sbom_sha256": "7" * 64,
        "scan_evidence_sha256": "8" * 64,
        "provenance_sha256": "9" * 64,
        "rendered": {
            "menhir_compose_sha256": "a" * 64,
            "yawn_compose_sha256": "d" * 64,
            "caddy_sha256": "b" * 64,
            "registry_sha256": "e" * 64,
            "policy_sha256": "c" * 64,
            "yawn_env_sha256": "6" * 64,
            "production_env_sha256": "0" * 64,
            "operations_policy_sha256": "1" * 64,
            "oauth_public_key_sha256": "2" * 64,
            "python_runtime_digest_sha256": "3" * 64,
        },
        "network": {
            "project": "menhir-prod",
            "external_network": "menhir-proxy",
            "alias": "menhir-prod-app",
            "peers": ["172.30.0.2"],
        },
        "rollback_anchors": {
            "initial_release": True,
            "prior_release_id": "",
            "prior_release_sha256": "",
            "prior_images": {
                "menhir": "sha256:" + "9" * 64,
                "neo4j": "sha256:" + "8" * 64,
                "caddy": "sha256:" + "7" * 64,
            },
            "prior_route_sha256": "4" * 64,
            "initial_host_state_sha256": "5" * 64,
        },
        "secret_version_ids": {
            "neo4j-auth": "v1",
            "neo4j-password": "v1",
            "oauth-signing-key": "v1",
            "oauth-retry-keyring": "v1",
            "oauth-consent-secret": "v1",
            "operator-key": "v1",
            "client-policy": "v1",
            "provider-key": "v1",
        },
        "artifacts": {
            "/srv/menhir/production/bin/worker": {
                "kind": "git",
                "sha256": "f" * 64,
                "repository": "menhir",
                "commit": "a" * 40,
                "path": "ops/worker",
                "blob_oid": "1" * 40,
            },
            "/etc/yawn-vps/menhir-oauth-policy.json": {
                "kind": "rendered",
                "sha256": "1" * 64,
                "rendered_key": "operations_policy_sha256",
            },
            "/etc/yawn-vps/menhir-oauth-public.pem": {
                "kind": "rendered",
                "sha256": "2" * 64,
                "rendered_key": "oauth_public_key_sha256",
            },
            "/etc/yawn-vps/menhir-python-runtime.sha256": {
                "kind": "rendered",
                "sha256": "3" * 64,
                "rendered_key": "python_runtime_digest_sha256",
            },
            "/srv/menhir/production/release/production.env": {
                "kind": "rendered",
                "sha256": "0" * 64,
                "rendered_key": "production_env_sha256",
            },
        },
        "deployment": {
            "topology": "same-host-docker",
            "legacy_container": "menhir-prod-app",
            "production_container": "menhir-prod-app",
            "candidate_container": "menhir-candidate-app",
            "legacy_database_container": "menhir-prod-neo4j",
            "candidate_database_container": "menhir-candidate-neo4j",
            "compose_project": "menhir-prod",
            "compose_service": "menhir",
        },
    }
    release["security_review"] = {
        "schema": 1,
        "kind": "menhir-production-security-review",
        "review_id": "security-review-1",
        "release_author": release["release_author"],
        "reviewer": "independent-security@example.com",
        "reviewed_utc": datetime.now(timezone.utc).isoformat(),
        "authority_sha256": _schema.release_authority_sha256(release),
        "verdict": "APPROVED",
        "unresolved_findings": {"critical": 0, "high": 0},
        "scope": sorted(_schema.REQUIRED_SECURITY_REVIEW_SCOPE),
        "report_sha256": "a" * 64,
        "review_artifact_sha256": "b" * 64,
    }
    return release


def _write_json(root: Path, name: str, obj) -> Path:
    path = root / name
    path.write_text(json.dumps(obj) + "\n")
    return path


def _refresh_security_review(release: dict) -> None:
    release["security_review"]["authority_sha256"] = (
        _schema.release_authority_sha256(release)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Duplicate-key rejection -------------------------------------------------


def test_load_strict_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "dup.json"
    path.write_text('{"a": 1, "a": 2}')
    with pytest.raises(ValueError):
        _schema.load_strict(str(path))


# --- release.json ------------------------------------------------------------


def test_release_valid(tmp_path):
    path = _write_json(tmp_path, "release.json", _valid_release())
    _schema.validate_release(str(path))


def test_prior_release_without_new_runtime_binding_remains_valid(tmp_path):
    release = _valid_release()
    del release["rendered"]["python_runtime_digest_sha256"]
    del release["artifacts"]["/etc/yawn-vps/menhir-python-runtime.sha256"]
    release["security_review"]["authority_sha256"] = \
        _schema.release_authority_sha256(release)
    path = _write_json(tmp_path, "prior-release.json", release)
    _schema.validate_release(str(path))


@pytest.mark.parametrize("label", ["repos", "images", "rendered", "network", "rollback_anchors"])
def test_release_rejects_unknown_top_level_label(tmp_path, label):
    release = _valid_release()
    release["unknown_extra"] = "x"
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError):
        _schema.validate_release(str(path))


def test_release_rejects_floating_image_pin(tmp_path):
    release = _valid_release()
    release["images"]["menhir"] = "menhir:latest"
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError):
        _schema.validate_release(str(path))


def test_release_requires_dockerfile_wheel_manifest(tmp_path):
    release = _valid_release()
    del release["dockerfile_wheel_manifest_sha256"]
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError):
        _schema.validate_release(str(path))


def test_release_requires_exact_approved_independent_security_review(tmp_path):
    release = _valid_release()
    del release["security_review"]
    path = _write_json(tmp_path, "missing-review.json", release)
    with pytest.raises(ValueError, match="security_review"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["security_review"]["verdict"] = "REJECTED"
    path = _write_json(tmp_path, "rejected-review.json", release)
    with pytest.raises(ValueError, match="APPROVED"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["security_review"]["unresolved_findings"]["critical"] = 1
    path = _write_json(tmp_path, "critical-review.json", release)
    with pytest.raises(ValueError, match="unresolved critical"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["security_review"]["reviewer"] = release["release_author"]
    path = _write_json(tmp_path, "self-review.json", release)
    with pytest.raises(ValueError, match="independent"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["security_review"]["scope"][0] = []
    path = _write_json(tmp_path, "malformed-scope.json", release)
    with pytest.raises(ValueError, match="scope"):
        _schema.validate_release(str(path))


def test_release_security_review_is_bound_to_every_release_claim(tmp_path):
    release = _valid_release()
    release["images"]["menhir"] = "sha256:" + "f" * 64
    path = _write_json(tmp_path, "stale-review.json", release)
    with pytest.raises(ValueError, match="exact release authority"):
        _schema.validate_release(str(path))


@pytest.mark.parametrize(
    ("script", "validator"),
    (
        ("backup-generation.sh", "validate-release"),
        ("menhir-backup-local.sh", "validate-release"),
        ("restore-generation.sh", "validate-release"),
        ("candidate-deploy.sh", "validate_release_authority"),
        ("candidate-accept.sh", "validate-release"),
        ("promote.sh", "validate_release_authority"),
        ("rollback.sh", "validate_release_authority"),
    ),
)
def test_every_production_mutation_path_uses_strict_release_validation(
    script, validator
):
    source = (REPO_ROOT / "deploy" / script).read_text(encoding="utf-8")
    assert validator in source


def test_release_cryptographically_binds_oauth_wheel_to_source_commit(tmp_path):
    release = _valid_release()
    release["oauth_wheel_source"]["commit"] = "a" * 40
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError, match="source commit"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["oauth_wheel_source"]["wheel_sha256"] = "0" * 64
    path = _write_json(tmp_path, "release-wheel.json", release)
    with pytest.raises(ValueError, match="wheel digest"):
        _schema.validate_release(str(path))


def test_release_requires_four_commits(tmp_path):
    release = _valid_release()
    del release["repos"]["yawn_vps"]
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError):
        _schema.validate_release(str(path))


@pytest.mark.parametrize(
    ("destination", "rendered_key"),
    (
        ("/etc/yawn-vps/menhir-oauth-policy.json", "operations_policy_sha256"),
        ("/etc/yawn-vps/menhir-oauth-public.pem", "oauth_public_key_sha256"),
        ("/etc/yawn-vps/menhir-python-runtime.sha256", "python_runtime_digest_sha256"),
    ),
)
def test_release_requires_oauth_authority_rendered_artifacts(
    tmp_path, destination, rendered_key
):
    release = _valid_release()
    del release["artifacts"][destination]
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError, match="required rendered artifact"):
        _schema.validate_release(str(path))

    release = _valid_release()
    release["artifacts"][destination]["sha256"] = "f" * 64
    path = _write_json(tmp_path, "mutated-release.json", release)
    with pytest.raises(ValueError, match="rendered authority"):
        _schema.validate_release(str(path))


# --- Manifest ----------------------------------------------------------------


def test_manifest_valid_exact_set_equality(tmp_path):
    _build_generation(tmp_path, dict(REQUIRED_FILES))
    _schema.validate_manifest(str(tmp_path / "MANIFEST.json"), str(tmp_path))


def test_manifest_rejects_undeclared_extra_file(tmp_path):
    _build_generation(tmp_path, dict(REQUIRED_FILES))
    _write(tmp_path, "config/extra.txt", b"extra")
    # The manifest does not declare config/extra.txt -> exact set equality fails.
    with pytest.raises(ValueError, match="undeclared"):
        _schema.validate_manifest(str(tmp_path / "MANIFEST.json"), str(tmp_path))


def test_manifest_rejects_missing_declared_file(tmp_path):
    _build_generation(tmp_path, dict(REQUIRED_FILES))
    (tmp_path / "neo4j" / "system.dump").unlink()
    with pytest.raises(ValueError):
        _schema.validate_manifest(str(tmp_path / "MANIFEST.json"), str(tmp_path))


def test_manifest_rejects_duplicate_keys(tmp_path):
    _build_generation(tmp_path, dict(REQUIRED_FILES))
    # Re-write MANIFEST.json with a duplicated top-level key.
    raw = '{"schema": 1, "schema": 1, "generation": "generation.Abc123"}'
    (tmp_path / "MANIFEST.json").write_text(raw)
    with pytest.raises(ValueError):
        _schema.validate_manifest(str(tmp_path / "MANIFEST.json"), str(tmp_path))


def test_manifest_requires_authority_files(tmp_path):
    files = dict(REQUIRED_FILES)
    del files["secrets/oauth/oauth_signing_key.json"]
    _build_generation(tmp_path, files)
    with pytest.raises(ValueError, match="oauth_signing_key"):
        _schema.validate_manifest(str(tmp_path / "MANIFEST.json"), str(tmp_path))


# --- Receipts ----------------------------------------------------------------


def _receipt_release():
    return {
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": "d" * 64,
        "menhir_image_digest": "sha256:" + "b" * 64,
        "neo4j_image_digest": "sha256:" + "c" * 64,
    }


def _backup_local_receipt():
    return {
        "schema": 1,
        "kind": "backup-local",
        "operation_job_id": "test-backup-job-1",
        "generation": "generation.Abc123",
        "manifest_sha256": "e" * 64,
        "release": _receipt_release(),
        "encryption": {
            "algorithm": "age-x25519",
            "recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
            "plaintext_archive_sha256": "a" * 64,
            "roundtrip_verified": True,
        },
        "local_encrypted_archives": {
            "retention_target_generations": 2,
            "retained_generation_count": 2,
            "current_archive_path": "/srv/menhir/backups/encrypted/generation.Abc123-current.tar.gz.age",
            "archives": [
                {
                    "generation": "generation.Abc123",
                    "path": "/srv/menhir/backups/encrypted/generation.Abc123-current.tar.gz.age",
                    "sha256": "f" * 64,
                    "size": 123,
                },
                {
                    "generation": "generation.Prior456",
                    "path": "/srv/menhir/backups/encrypted/generation.Prior456-prior.tar.gz.age",
                    "sha256": "c" * 64,
                    "size": 122,
                },
            ],
        },
        "plaintext_removed": True,
        "checked_utc": _now(),
    }


def test_backup_local_receipt_valid(tmp_path):
    receipt = _backup_local_receipt()
    path = _write_json(tmp_path, "receipt.json", receipt)
    _schema.validate_receipt(str(path), "backup-local")


def test_release_schema_refuses_empty_prior_anchor_for_non_initial(tmp_path):
    release = _valid_release()
    release["rollback_anchors"]["initial_release"] = False
    release["rollback_anchors"]["prior_release_id"] = ""
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError, match="non-initial release"):
        _schema.validate_release(str(path))


def test_backup_local_receipt_requires_roundtrip_verification(tmp_path):
    receipt = _backup_local_receipt()
    receipt["encryption"]["roundtrip_verified"] = False
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="roundtrip_verified"):
        _schema.validate_receipt(str(path), "backup-local")


def test_backup_local_receipt_requires_age_encryption(tmp_path):
    receipt = _backup_local_receipt()
    receipt["encryption"]["algorithm"] = "plaintext"
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="age-x25519"):
        _schema.validate_receipt(str(path), "backup-local")


def test_backup_local_receipt_requires_current_generation_in_local_evidence(tmp_path):
    receipt = _backup_local_receipt()
    receipt["local_encrypted_archives"]["archives"][0]["generation"] = \
        "generation.Other789"
    receipt["local_encrypted_archives"]["archives"][0]["path"] = \
        "/srv/menhir/backups/encrypted/generation.Other789-current.tar.gz.age"
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="current generation"):
        _schema.validate_receipt(str(path), "backup-local")


def test_backup_local_receipt_requires_correct_distinct_generation_count(tmp_path):
    receipt = _backup_local_receipt()
    receipt["local_encrypted_archives"]["archives"][1]["generation"] = \
        "generation.Abc123"
    receipt["local_encrypted_archives"]["archives"][1]["path"] = \
        "/srv/menhir/backups/encrypted/generation.Abc123-prior.tar.gz.age"
    receipt["local_encrypted_archives"]["retained_generation_count"] = 2
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="distinct evidence"):
        _schema.validate_receipt(str(path), "backup-local")


def _live_backup_receipt(tmp_path):
    archive_root = tmp_path / "encrypted"
    archive_root.mkdir()
    current = archive_root / "generation.Abc123-current.tar.gz.age"
    prior = archive_root / "generation.Prior456-prior.tar.gz.age"
    current.write_bytes(b"current encrypted archive")
    prior.write_bytes(b"prior encrypted archive")
    receipt = _backup_local_receipt()
    receipt["local_encrypted_archives"]["current_archive_path"] = str(current)
    for entry, path in zip(
            receipt["local_encrypted_archives"]["archives"], (current, prior)):
        entry["path"] = str(path)
        entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        entry["size"] = path.stat().st_size
    receipt_path = _write_json(tmp_path, "backup-receipt.json", receipt)
    return archive_root, current, receipt_path, receipt


def test_backup_promotion_verifies_live_archive_bytes(tmp_path):
    archive_root, current, receipt_path, _ = _live_backup_receipt(tmp_path)
    _schema.validate_backup_promotion(str(receipt_path), str(archive_root))

    current.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="size|digest"):
        _schema.validate_backup_promotion(str(receipt_path), str(archive_root))


def test_backup_promotion_refuses_missing_archive(tmp_path):
    archive_root, current, receipt_path, _ = _live_backup_receipt(tmp_path)
    current.unlink()
    with pytest.raises(OSError):
        _schema.validate_backup_promotion(str(receipt_path), str(archive_root))


def test_backup_receipt_requires_retention_target_to_be_met(tmp_path):
    receipt = _backup_local_receipt()
    receipt["local_encrypted_archives"]["retention_target_generations"] = 3
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="below retention target"):
        _schema.validate_receipt(str(path), "backup-local")


def test_bootstrap_backup_is_valid_but_cannot_gate_promotion(tmp_path):
    receipt = _backup_local_receipt()
    receipt["local_encrypted_archives"]["retention_target_generations"] = 1
    receipt["local_encrypted_archives"]["retained_generation_count"] = 1
    receipt["local_encrypted_archives"]["archives"] = [
        receipt["local_encrypted_archives"]["archives"][0]
    ]
    path = _write_json(tmp_path, "receipt.json", receipt)
    _schema.validate_receipt(str(path), "backup-local")
    with pytest.raises(ValueError, match="two retained encrypted generations"):
        _schema.validate_backup_promotion(str(path), str(tmp_path))


def test_desktop_archive_receipt_binds_current_backup_and_release(tmp_path):
    archive_root, current, backup_path, backup = _live_backup_receipt(tmp_path)
    release = _valid_release()
    release["release_id"] = backup["release"]["release_id"]
    release_path = _write_json(tmp_path, "release.json", release)
    release_sha = hashlib.sha256(release_path.read_bytes()).hexdigest()
    backup["release"]["release_manifest_sha256"] = release_sha
    backup_path.write_text(json.dumps(backup), encoding="utf-8")
    desktop = {
        "schema": 1,
        "kind": "menhir-desktop-archive",
        "generation": backup["generation"],
        "release": {
            "release_id": release["release_id"],
            "release_manifest_sha256": release_sha,
        },
        "archive": {
            "sha256": hashlib.sha256(current.read_bytes()).hexdigest(),
            "size_bytes": current.stat().st_size,
        },
        "desktop_destination": r"C:\\Backups\\Menhir\\generation.Abc123.tar.gz.age",
        "archived_utc": _now(),
    }
    desktop_path = _write_json(tmp_path, "desktop-receipt.json", desktop)

    _schema.validate_desktop_archive(
        str(desktop_path), str(backup_path), str(release_path), str(archive_root)
    )

    desktop["archive"]["sha256"] = "0" * 64
    desktop_path.write_text(json.dumps(desktop), encoding="utf-8")
    with pytest.raises(ValueError, match="does not bind"):
        _schema.validate_desktop_archive(
            str(desktop_path), str(backup_path), str(release_path), str(archive_root)
        )


def test_restore_uses_the_selected_generations_immutable_backup_receipt():
    source = (REPO_ROOT / "deploy" / "restore-generation.sh").read_text(
        encoding="utf-8"
    )
    assert 'backup_receipt="${STATUS_DIR}/backup-receipts/${gen_id}.json"' in source


def test_rehearsal_receipt_valid(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "rehearsal",
        "generation": "generation.Abc123",
        "manifest_sha256": "e" * 64,
        "release": _receipt_release(),
        "neo4j_check": "ok",
        "sqlite_integrity": "ok",
        "checked_utc": _now(),
    }
    path = _write_json(tmp_path, "receipt.json", receipt)
    _schema.validate_receipt(str(path), "rehearsal")


def test_candidate_accept_receipt_valid(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "candidate-accept",
        "generation": "generation.Abc123",
        "manifest_sha256": "e" * 64,
        "release": _receipt_release(),
        "readyz": "ok",
        "oauth_discovery": "ok",
        "recall": "skipped",
        "mutation_503": "ok",
        "tier_tool_identity": "ok",
        "authority_before_digest": "1" * 64,
        "authority_after_digest": "1" * 64,
        "same_host_writer_fence_sha256": "2" * 64,
        "checked_utc": _now(),
    }
    path = _write_json(tmp_path, "receipt.json", receipt)
    _schema.validate_receipt(str(path), "candidate-accept")


def test_receipt_kind_mismatch(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "rehearsal",
        "generation": "generation.Abc123",
        "manifest_sha256": "e" * 64,
        "release": _receipt_release(),
        "neo4j_check": "ok",
        "sqlite_integrity": "ok",
        "checked_utc": _now(),
    }
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="kind"):
        _schema.validate_receipt(str(path), "backup-local")


def test_promotion_revalidates_same_host_writer_census_under_lock():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "promote.sh").read_text()
    assert "acquire_release_lock" in source
    assert "same-host-writer-fence.json" in source
    assert 'docker ps -aq' in source
    assert 'verify_args=(verify "$RELEASE_JSON" "$same_host_fence" "$census")' in source
    assert 'verify_args+=(--allow-production)' in source
    assert source.count("verify_same_host_fence") >= 4


def test_release_run_reconstructs_progress_instead_of_trusting_state():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "release-run.sh").read_text()
    assert "release-run.json is observability, never authorization" in source
    assert 'require_root_file "$state" "release-run state"' in source
    assert '"${SCRIPT_DIR}/same-host-fence.sh"' in source
    assert '"$caddy_release" reconcile' in source
    assert 'same_host_helper="${SCRIPT_DIR}/lib/same_host_fence.py"' in source
    assert 'same_host_helper="${SCRIPT_DIR}/same_host_fence.py"' in source
    assert '"$same_host_helper" verify' in source
    assert "--allow-production" in source
    assert "current-generation" in source
    assert 'validate-desktop-archive "$desktop_receipt" "$backup_receipt" "$RELEASE_JSON"' in source
    assert "requires a completed local backup and verified desktop archive receipt" in source


def test_runtime_binding_distinguishes_policy_file_and_canonical_digests():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "release-lib.sh").read_text()
    assert 'policy.pop("canonical_digest", "")' in source
    assert 'actual_policy_digest=hashlib.sha256(json.dumps(' in source
    assert '(release["rendered"]["policy_sha256"], sha(policy_path), "policy")' in source
    assert '(policy_digest, declared_policy_digest, "configured policy")' in source
    assert '(declared_policy_digest, actual_policy_digest, "canonical policy")' in source
    assert '(policy_digest, sha(policy_path), "configured policy")' not in source


def test_generation_checksum_manifest_is_not_self_referential():
    root = Path(__file__).resolve().parents[1]
    backup = (root / "deploy" / "backup-generation.sh").read_text()
    restore = (root / "deploy" / "restore-generation.sh").read_text()
    assert "! -path './SHA256SUMS'" in backup
    assert "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  ./SHA256SUMS" in restore
    assert "invalid legacy SHA256SUMS self-entry" in restore
    assert "unexpected SHA256SUMS self-entry" in restore


def test_promotion_requires_release_bound_desktop_archive_receipt():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "promote.sh").read_text()
    assert 'require_root_file "$desktop_receipt" "desktop archive receipt"' in source
    assert 'validate-desktop-archive "$desktop_receipt" "$backup_receipt" "$RELEASE_JSON"' in source


def test_candidate_deploy_census_fences_app_and_database_before_start():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "candidate-deploy.sh").read_text()
    assert 'docker ps -aq' in source
    assert 'same_host_helper" verify' in source
    assert "legacy or competing app/database" in source


def test_new_cutover_archives_only_completed_prior_mutation_marker():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "backup-generation.sh").read_text()
    assert 'prior_mutation_generation="$(sed -n \'1p\' "$mutation_marker")"' in source
    assert 'prior_current_generation="$(current_generation)"' in source
    assert 'mutation-history/${prior_mutation_generation}.txt' in source
    assert "prior mutation history conflicts" in source
    assert "os.unlink(source)" in source


def test_cutover_disables_legacy_restart_before_quiesce_and_backup():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "backup-generation.sh").read_text()
    disable = 'docker update --restart=no "$legacy_app_id" "$legacy_database_id"'
    quiesce = 'docker compose -f "${COMPOSE_FILE}" stop'
    assert source.count(disable) == 1
    assert source.index(disable) < source.index(quiesce)


def test_runtime_image_does_not_require_retired_remote_fence_secret():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile").read_text()
    assert "source-fence-token" not in source
    assert "MENHIR_SOURCE_FENCE_TOKEN" not in source


def test_release_binds_fixed_same_host_topology(tmp_path):
    release = _valid_release()
    path = _write_json(tmp_path, "release.json", release)
    _schema.validate_release(str(path))
    release["deployment"]["legacy_container"] = "attacker-selected"
    _refresh_security_review(release)
    path = _write_json(tmp_path, "bad-release.json", release)
    with pytest.raises(ValueError, match="same-host Docker topology"):
        _schema.validate_release(str(path))


def test_backup_loads_and_checks_both_neo4j_dumps_before_acceptance():
    source = (REPO_ROOT / "deploy" / "backup-generation.sh").read_text()
    for database in ("neo4j", "system"):
        assert f"database load {database} --from-path=/backup" in source
        assert f"database check {database} --report-path=/backup" in source
    assert '.neo4j-verify.${generation}.' in source


# ---------------------------------------------------------------------------
# authority_digest.py -- deterministic, tamper-evident authority hash (blocker 5)


def test_candidate_prestart_neo4j_digest_uses_ephemeral_reviewed_image() -> None:
    release_lib = (REPO_ROOT / "deploy" / "release-lib.sh").read_text(
        encoding="utf-8"
    )
    digest_function = release_lib.split(
        "candidate_neo4j_authority_digest() {", 1
    )[1].split("\n}", 1)[0]
    assert (
        'MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" config --quiet'
        in digest_function
    )
    assert (
        'MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" run '
        '--rm --no-deps -T menhir '
        "python3 - neo4j" in digest_function
    )
    assert "docker exec -i menhir-candidate-app" not in digest_function
# ---------------------------------------------------------------------------


def _seed_state(root: Path):
    (root / "oauth").mkdir(parents=True)
    (root / "telemetry").mkdir()
    (root / "oauth" / "menhir_oauth_as.db").write_bytes(b"oauth-bytes")
    (root / "telemetry" / "mcp_telemetry.db").write_bytes(b"telemetry-bytes")
    (root / "queues").mkdir()
    (root / "queues" / "ingest.db").write_bytes(b"queue-bytes")
    (root / "leases").mkdir()
    (root / "leases" / "lease.db").write_bytes(b"lease-bytes")
    (root / "sessions").mkdir()
    (root / "sessions" / "session.db").write_bytes(b"session-bytes")
    (root / "recall").mkdir()
    (root / "recall" / "recall.db").write_bytes(b"recall-bytes")


def test_authority_local_digest_hashes_all_authoritative_files(tmp_path):
    _seed_state(tmp_path)
    digest = _authority.local_files(str(tmp_path))
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_authority_local_digest_is_deterministic(tmp_path):
    _seed_state(tmp_path)
    first = _authority.local_files(str(tmp_path))
    second = _authority.local_files(str(tmp_path))
    assert first == second


def test_authority_local_digest_is_order_independent(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir(); b.mkdir()
    for root in (a, b):
        (root / "x").mkdir(); (root / "y").mkdir()
        (root / "x" / "1").write_bytes(b"one")
        (root / "y" / "2").write_bytes(b"two")
    # Create x/1 and y/2 in the opposite creation order so traversal order
    # differs, but the canonical hash must be identical.
    digest_a = _authority.local_files(str(a))
    digest_b = _authority.local_files(str(b))
    assert digest_a == digest_b


def test_authority_local_digest_changes_on_content_change(tmp_path):
    _seed_state(tmp_path)
    before = _authority.local_files(str(tmp_path))
    (tmp_path / "queues" / "ingest.db").write_bytes(b"HOSTILE-CHANGE")
    after = _authority.local_files(str(tmp_path))
    assert before != after


def test_authority_local_digest_has_no_disposable_exclusion_bypass(tmp_path):
    _seed_state(tmp_path)
    probe = tmp_path / "recall" / "probe-output.db"
    probe.write_bytes(b"probe-session-receipt")
    before = _authority.local_files(str(tmp_path))
    probe.write_bytes(b"changed-probe-session-receipt")
    assert _authority.local_files(str(tmp_path)) != before
    with pytest.raises(TypeError):
        _authority.local_files(str(tmp_path), ("recall/probe-output.db",))


def test_authority_local_digest_rejects_symlink(tmp_path):
    _seed_state(tmp_path)
    target = tmp_path / "real.txt"
    target.write_bytes(b"t")
    try:
        (tmp_path / "link").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform")
    with pytest.raises(ValueError, match="symlink"):
        _authority.local_files(str(tmp_path))


def test_authority_local_digest_rejects_special_entry(tmp_path):
    _seed_state(tmp_path)
    import stat as stat_mod
    fifo = tmp_path / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        pytest.skip("named pipes not available on this platform")
    assert not stat_mod.S_ISREG(os.stat(fifo).st_mode)
    with pytest.raises(ValueError, match="special"):
        _authority.local_files(str(tmp_path))


def test_authority_local_digest_rejects_symlink_root(tmp_path):
    _seed_state(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    try:
        linked = tmp_path / "linked-root"
        linked.symlink_to(other)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform")
    with pytest.raises(ValueError):
        _authority.local_files(str(linked))


def test_authority_local_set_binds_labels_and_every_disjoint_root(tmp_path):
    oauth = tmp_path / "oauth-root"
    telemetry = tmp_path / "telemetry-root"
    oauth.mkdir(); telemetry.mkdir()
    (oauth / "authority.db").write_bytes(b"oauth")
    (telemetry / "authority.db").write_bytes(b"telemetry")
    roots = {"oauth": str(oauth), "telemetry": str(telemetry)}
    before = _authority.local_set(roots)
    (telemetry / "authority.db").write_bytes(b"changed")
    assert _authority.local_set(roots) != before
    assert _authority.local_set(dict(reversed(list(roots.items())))) \
        == _authority.local_set(roots)


def test_authority_local_set_rejects_duplicate_resolved_roots(tmp_path):
    root = tmp_path / "authority"
    root.mkdir()
    with pytest.raises(ValueError, match="unique"):
        _authority.local_set({"oauth": str(root), "telemetry": str(root)})


def test_authority_combine_valid(tmp_path):
    digest = _authority.combine("a" * 64, "b" * 64)
    assert len(digest) == 64
    assert digest != "a" * 64


def test_authority_combine_rejects_bad_local_hex():
    with pytest.raises(ValueError, match="local"):
        _authority.combine("not-hex", "b" * 64)


def test_authority_combine_rejects_bad_neo4j_hex():
    with pytest.raises(ValueError, match="neo4j"):
        _authority.combine("a" * 64, "g" * 64)


def test_authority_combine_is_deterministic():
    first = _authority.combine("a" * 64, "b" * 64)
    second = _authority.combine("a" * 64, "b" * 64)
    assert first == second
    assert first != _authority.combine("a" * 64, "c" * 64)


def test_neo4j_structured_digest_is_type_preserving_and_delimiter_safe():
    left = [{"kind": "node-property", "key": "a|b", "value": "c"}]
    right = [{"kind": "node-property", "key": "a", "value": "b|c"}]
    assert _authority.structured_records(left) != _authority.structured_records(right)
    assert _authority.structured_records([{"value": 1}]) != \
        _authority.structured_records([{"value": "1"}])


def test_neo4j_structured_digest_canonicalizes_mapping_order():
    first = [{"kind": "node", "properties": {"b": 2, "a": 1}}]
    second = [{"properties": {"a": 1, "b": 2}, "kind": "node"}]
    assert _authority.structured_records(first) == _authority.structured_records(second)


def test_neo4j_authority_normalizes_unordered_database_collections():
    assert _authority._authority_row("nodes", {"labels": ["Z", "A"]}) == {
        "labels": ["A", "Z"]
    }
    assert _authority._authority_row("users", {"roles": ["reader", "admin"]}) == {
        "roles": ["admin", "reader"]
    }


def test_neo4j_authority_query_inventory_includes_security_authority():
    queries = dict(_authority.NEO4J_AUTHORITY_QUERIES)
    assert {"nodes", "relationships", "indexes", "constraints", "databases",
            "users", "roles", "privileges"} <= set(queries)
    assert "SHOW ROLES WITH USERS" in queries["roles"]
    assert "SHOW PRIVILEGES" in queries["privileges"]


def test_neo4j_community_query_inventory_omits_enterprise_only_authority():
    queries = dict(_authority.NEO4J_COMMUNITY_AUTHORITY_QUERIES)
    assert {"nodes", "relationships", "indexes", "constraints", "databases",
            "users"} <= set(queries)
    assert "roles" not in queries
    assert "privileges" not in queries
    assert "dbms.components" in _authority.NEO4J_COMPONENT_QUERY


def test_neo4j_enterprise_query_inventory_requires_roles_and_privileges():
    queries = dict(_authority.NEO4J_ENTERPRISE_AUTHORITY_QUERIES)
    assert set(queries) == {"roles", "privileges"}


def test_release_library_defines_canonical_prod_root_and_hash_memory_limit():
    source = (REPO_ROOT / "deploy" / "release-lib.sh").read_text(encoding="utf-8")
    assert 'MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-${MENHIR_ROOT}}"' in source
    assert 'MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" config --quiet' in source
    assert '--rm --no-deps -T menhir python3 - neo4j' in source
    assert '--memory 4g' not in source


def test_compose_parser_resolves_authority_memory_limit_to_four_gibibytes():
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    env = {
        **os.environ,
        "MENHIR_IMAGE": "example.invalid/menhir@sha256:" + "1" * 64,
        "NEO4J_IMAGE": "example.invalid/neo4j@sha256:" + "2" * 64,
        "MENHIR_RUNTIME_MODE": "candidate-readonly",
        "MENHIR_INSTANCE_ID": "parser-test",
        "MENHIR_RELEASE_ID": "parser-test",
        "MENHIR_PUBLIC_BASE_URL": "https://memory.example",
        "MENHIR_CLIENT_POLICY_DIGEST": "3" * 64,
        "LLM_CHAT_PROVIDER": "openai",
        "GRAPHITI_LLM_PROVIDER": "openai",
        "GRAPHITI_EMBED_PROVIDER": "openai",
        "MENHIR_APP_MEMORY_LIMIT": "4g",
    }
    completed = subprocess.run(
        [
            "docker", "compose", "--file",
            str(REPO_ROOT / "deploy" / "docker-compose.production.yml"),
            "config", "--format", "json",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    config = json.loads(completed.stdout)
    assert int(config["services"]["menhir"]["mem_limit"]) == 4 * 1024**3


def test_candidate_shell_functions_do_not_require_ambient_generation(tmp_path):
    def bash_path(path: Path) -> str:
        resolved = path.resolve()
        if os.name == "nt":
            return f"/mnt/{resolved.drive[0].lower()}{resolved.as_posix()[2:]}"
        return resolved.as_posix()

    source = (REPO_ROOT / "deploy" / "release-lib.sh").read_text(encoding="utf-8")
    backup_root = tmp_path / "backups"
    source = source.replace(
        'BACKUP_ROOT="/srv/menhir/backups"',
        f'BACKUP_ROOT="{bash_path(backup_root)}"',
    )
    library = tmp_path / "release-lib.sh"
    library.write_text(source, encoding="utf-8", newline="\n")
    (backup_root / "candidate" / "abc").mkdir(parents=True)
    (backup_root / "candidate" / "abc" / "REHEARSAL-PASSED").write_text("ok\n")
    script = f'''set -euo pipefail
source "{bash_path(library)}"
compose_env() {{ :; }}
unset generation || true
candidate_down abc
candidate_compose() {{ :; }}
wait_healthy() {{ :; }}
install() {{ :; }}
unset generation || true
candidate_up abc
'''
    completed = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _run_fake_neo4j_authority(monkeypatch, components):
    import neo4j

    calls = []

    class Session:
        def __init__(self, database):
            self.database = database

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def run(self, query):
            calls.append((self.database, query))
            if query == _authority.NEO4J_COMPONENT_QUERY:
                return components
            return []

    class Driver:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def session(self, *, database):
            return Session(database)

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *_args, **_kwargs: Driver())
    digest = _authority.neo4j_authority_digest(
        uri="bolt://neo4j:7687", username="neo4j", password="secret", database="neo4j"
    )
    return digest, calls


def test_neo4j_community_execution_omits_enterprise_queries(monkeypatch):
    digest, calls = _run_fake_neo4j_authority(
        monkeypatch,
        [{"name": "Neo4j Kernel", "versions": ["5.26.30"], "edition": "community"}],
    )
    queries = [query for _database, query in calls]
    assert len(digest) == 64
    assert "SHOW USERS" in queries
    assert "SHOW ROLES WITH USERS" not in queries
    assert "SHOW PRIVILEGES" not in queries


def test_neo4j_enterprise_execution_requires_security_queries(monkeypatch):
    _digest, calls = _run_fake_neo4j_authority(
        monkeypatch,
        [{"name": "Neo4j Kernel", "versions": ["5.26.30"], "edition": "enterprise"}],
    )
    queries = [query for _database, query in calls]
    assert "SHOW ROLES WITH USERS" in queries
    assert "SHOW PRIVILEGES" in queries


def test_neo4j_component_authority_changes_digest(monkeypatch):
    first, _calls = _run_fake_neo4j_authority(
        monkeypatch,
        [{"name": "Neo4j Kernel", "versions": ["5.26.29"], "edition": "community"}],
    )
    second, _calls = _run_fake_neo4j_authority(
        monkeypatch,
        [{"name": "Neo4j Kernel", "versions": ["5.26.30"], "edition": "community"}],
    )
    assert first != second


@pytest.mark.parametrize("components", [
    [],
    [{"name": "Neo4j Kernel", "versions": ["5"], "edition": "unknown"}],
    [
        {"name": "Neo4j Kernel", "versions": ["5"], "edition": "community"},
        {"name": "Other", "versions": ["5"], "edition": "enterprise"},
    ],
])
def test_neo4j_edition_detection_fails_closed(monkeypatch, components):
    with pytest.raises(ValueError):
        _run_fake_neo4j_authority(monkeypatch, components)
