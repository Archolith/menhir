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
    "secrets/menhir/source-fence-token": b"f" * 48,
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
    return {
        "schema": 1,
        "release_id": "menhir-prod-0.2.0-1",
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
            "source-fence-token": "v1",
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
            "/srv/menhir/production/release/production.env": {
                "kind": "rendered",
                "sha256": "0" * 64,
                "rendered_key": "production_env_sha256",
            },
        },
        "source_fence_key_id": "source-fence-v1",
        "source_fence_public_key": "A" * 43,
        "source_fence_tls_ca_sha256": "0" * 64,
        "external_evidence_public_keys": {"worker-a": "A" * 43, "worker-b": "A" * 43},
    }


def _write_json(root: Path, name: str, obj) -> Path:
    path = root / name
    path.write_text(json.dumps(obj) + "\n")
    return path


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


def _backup_upload_receipt():
    return {
        "schema": 1,
        "kind": "backup-upload",
        "operation_job_id": "test-backup-job-1",
        "generation": "generation.Abc123",
        "manifest_sha256": "e" * 64,
        "release": _receipt_release(),
        "offhost": {
            "bucket": "menhir-backups",
            "production_backup": {
                "object_key": "menhir/generation.Abc123/archive.tar.gz.age",
                "version_id": "version-production-1",
                "object_sha256": "f" * 64,
                "object_size": 123,
                "server_side_encryption": "AES256",
                "lock_mode": "COMPLIANCE",
                "worm_retention_until": "2031-01-01T00:00:00Z",
                "version_readback_verified": True,
                "client_encryption": {
                    "algorithm": "age-x25519",
                    "recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
                    "plaintext_archive_sha256": "a" * 64,
                },
            },
            "sacrificial_probe": {
                "object_key": "menhir/worm-delete-denial-probes/generation.Abc123/probe",
                "version_id": "version-probe-1",
                "object_sha256": "b" * 64,
                "object_size": 42,
                "server_side_encryption": "AES256",
                "lock_mode": "COMPLIANCE",
                "worm_retention_until": "2031-01-01T00:00:00Z",
                "version_readback_verified": True,
                "locked_version_delete_denied": True,
                "version_persisted_after_delete_denial": True,
            },
        },
        "local_encrypted_archives": {
            "minimum_retained_generations": 2,
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


def test_backup_upload_receipt_valid(tmp_path):
    receipt = _backup_upload_receipt()
    path = _write_json(tmp_path, "receipt.json", receipt)
    _schema.validate_receipt(str(path), "backup-upload")


def test_release_schema_refuses_empty_prior_anchor_for_non_initial(tmp_path):
    release = _valid_release()
    release["rollback_anchors"]["initial_release"] = False
    release["rollback_anchors"]["prior_release_id"] = ""
    path = _write_json(tmp_path, "release.json", release)
    with pytest.raises(ValueError, match="non-initial release"):
        _schema.validate_release(str(path))


def test_backup_upload_receipt_requires_worm_denial(tmp_path):
    receipt = _backup_upload_receipt()
    receipt["offhost"]["sacrificial_probe"]["locked_version_delete_denied"] = False
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="locked_version_delete_denied"):
        _schema.validate_receipt(str(path), "backup-upload")


def test_backup_upload_receipt_requires_distinct_production_and_probe_objects(tmp_path):
    receipt = _backup_upload_receipt()
    production = receipt["offhost"]["production_backup"]
    probe = receipt["offhost"]["sacrificial_probe"]
    production["object_key"] = \
        "menhir/worm-delete-denial-probes/generation.Abc123/shared"
    probe["object_key"] = production["object_key"]
    probe["version_id"] = production["version_id"]
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="must differ"):
        _schema.validate_receipt(str(path), "backup-upload")


def test_backup_upload_receipt_requires_current_generation_in_local_evidence(tmp_path):
    receipt = _backup_upload_receipt()
    receipt["local_encrypted_archives"]["archives"][0]["generation"] = \
        "generation.Other789"
    receipt["local_encrypted_archives"]["archives"][0]["path"] = \
        "/srv/menhir/backups/encrypted/generation.Other789-current.tar.gz.age"
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="current generation"):
        _schema.validate_receipt(str(path), "backup-upload")


def test_backup_upload_receipt_requires_configured_distinct_generation_count(tmp_path):
    receipt = _backup_upload_receipt()
    receipt["local_encrypted_archives"]["archives"][1]["generation"] = \
        "generation.Abc123"
    receipt["local_encrypted_archives"]["archives"][1]["path"] = \
        "/srv/menhir/backups/encrypted/generation.Abc123-prior.tar.gz.age"
    receipt["local_encrypted_archives"]["retained_generation_count"] = 1
    path = _write_json(tmp_path, "receipt.json", receipt)
    with pytest.raises(ValueError, match="configured minimum"):
        _schema.validate_receipt(str(path), "backup-upload")


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
        "external_prerequisite_receipt": "2" * 64,
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
        _schema.validate_receipt(str(path), "backup-upload")


def test_external_prerequisite_requires_true_attestations(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "external-prerequisite",
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": "d" * 64,
        "checked_utc": _now(),
        "firewall": True,
        "proxied_dns": True,
        "full_strict": True,
        "hostname_aop": True,
        "external_scan": True,
        "console_recovery": True,
        "caddy_volume_permissions": False,
    }
    path = _write_json(tmp_path, "prerequisite.json", receipt)
    with pytest.raises(ValueError, match="caddy_volume_permissions"):
        _schema.validate_prerequisite(str(path))


def test_external_evidence_workers_aggregate_signed_distinct_networks(tmp_path):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    release = _valid_release()
    private_keys = {}
    public_keys = {}
    for worker in ("worker-a", "worker-b"):
        key = Ed25519PrivateKey.generate()
        private_keys[worker] = key
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        public_keys[worker] = base64.urlsafe_b64encode(public).rstrip(b"=").decode()
    release["external_evidence_public_keys"] = public_keys
    release_path = _write_json(tmp_path, "release.json", release)
    checks_path = _write_json(
        tmp_path,
        "checks.json",
        {
            "firewall": True,
            "proxied_dns": True,
            "full_strict": True,
            "hostname_aop": True,
            "external_scan": True,
            "console_recovery": True,
            "caddy_volume_permissions": True,
        },
    )
    observations = []
    worker_script = REPO_ROOT / "deploy" / "external-evidence-worker.py"
    for index, worker in enumerate(("worker-a", "worker-b"), start=1):
        key_path = tmp_path / f"{worker}.pem"
        key_path.write_bytes(
            private_keys[worker].private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                str(worker_script),
                str(release_path),
                str(key_path),
                worker,
                f"network-{index}",
                "route-v1",
                str(checks_path),
            ],
            cwd=REPO_ROOT / "deploy",
            capture_output=True,
            text=True,
            check=True,
        )
        observations.append(_write_json(tmp_path, f"{worker}.json", json.loads(result.stdout)))

    output = tmp_path / "external-prerequisite.json"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "deploy" / "external-evidence-aggregate.py"),
            str(release_path),
            "route-v1",
            str(output),
            *(str(path) for path in observations),
        ],
        cwd=REPO_ROOT / "deploy",
        check=True,
    )
    receipt = _schema.validate_prerequisite_binding(str(output), str(release_path))
    assert {item["network_id"] for item in receipt["observations"]} == {
        "network-1",
        "network-2",
    }
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600

    repeated = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "deploy" / "external-evidence-aggregate.py"),
            str(release_path),
            "route-v1",
            str(output),
            *(str(path) for path in observations),
        ],
        cwd=REPO_ROOT / "deploy",
        capture_output=True,
        text=True,
    )
    assert repeated.returncode != 0
    assert "already exists" in repeated.stderr


def test_source_fence_requires_both_proofs(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "source-writer-fence",
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": "d" * 64,
        "checked_utc": _now(),
        "expires_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "source_id": "local-menhir-chatgpt-chat",
        "source_writer_stopped": True,
        "source_mutation_probe_denied": False,
        "source_service_disabled": True,
        "source_firewall_persistent": True,
        "signing_key_id": "source-fence-v1",
        "signature": "A" * 86,
    }
    path = _write_json(tmp_path, "source-fence.json", receipt)
    with pytest.raises(ValueError, match="source_mutation_probe_denied"):
        _schema.validate_source_fence(str(path))


def test_source_fence_rejects_expired_receipt(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "source-writer-fence",
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": "d" * 64,
        "checked_utc": _now(),
        "expires_utc": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        "source_id": "local-menhir-chatgpt-chat",
        "source_writer_stopped": True,
        "source_mutation_probe_denied": True,
        "source_service_disabled": True,
        "source_firewall_persistent": True,
        "signing_key_id": "source-fence-v1",
        "signature": "A" * 86,
    }
    path = _write_json(tmp_path, "source-fence.json", receipt)
    with pytest.raises(ValueError, match="expires"):
        _schema.validate_source_fence(str(path))


def test_source_fence_rejects_window_longer_than_ten_minutes(tmp_path):
    receipt = {
        "schema": 1,
        "kind": "source-writer-fence",
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": "d" * 64,
        "checked_utc": _now(),
        "expires_utc": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        "source_id": "local-menhir-chatgpt-chat",
        "source_writer_stopped": True,
        "source_mutation_probe_denied": True,
        "source_service_disabled": True,
        "source_firewall_persistent": True,
        "signing_key_id": "source-fence-v1",
        "signature": "A" * 86,
    }
    path = _write_json(tmp_path, "source-fence.json", receipt)
    with pytest.raises(ValueError, match="no more than 10 minutes"):
        _schema.validate_source_fence(str(path))


def test_source_fence_ed25519_authenticates_release_bound_claims(tmp_path):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    release = _valid_release()
    release["source_fence_public_key"] = base64.urlsafe_b64encode(public).rstrip(b"=").decode()
    release_path = _write_json(tmp_path, "release.json", release)
    receipt = {
        "schema": 1,
        "kind": "source-writer-fence",
        "release_id": "menhir-prod-0.2.0-1",
        "release_manifest_sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
        "checked_utc": _now(),
        "expires_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "source_id": "local-menhir-chatgpt-chat",
        "source_writer_stopped": True,
        "source_mutation_probe_denied": True,
        "source_service_disabled": True,
        "source_firewall_persistent": True,
        "signing_key_id": "source-fence-v1",
        "signature": "A" * 86,
    }
    payload = _schema.source_fence_payload(receipt).encode()
    receipt["signature"] = base64.urlsafe_b64encode(key.sign(payload)).rstrip(b"=").decode()
    path = _write_json(tmp_path, "source-fence.json", receipt)
    _schema.verify_source_fence(str(path), str(release_path))

    receipt["release_id"] = "menhir-prod-0.2.0-2"
    receipt["signature"] = base64.urlsafe_b64encode(
        key.sign(_schema.source_fence_payload(receipt).encode())
    ).rstrip(b"=").decode()
    path = _write_json(tmp_path, "source-fence-wrong-release.json", receipt)
    with pytest.raises(ValueError, match="release_id"):
        _schema.verify_source_fence(str(path), str(release_path))

    receipt["release_id"] = release["release_id"]
    receipt["release_manifest_sha256"] = "0" * 64
    receipt["signature"] = base64.urlsafe_b64encode(
        key.sign(_schema.source_fence_payload(receipt).encode())
    ).rstrip(b"=").decode()
    path = _write_json(tmp_path, "source-fence-wrong-digest.json", receipt)
    with pytest.raises(ValueError, match="release digest"):
        _schema.verify_source_fence(str(path), str(release_path))

    receipt["release_manifest_sha256"] = hashlib.sha256(
        release_path.read_bytes()
    ).hexdigest()
    receipt["source_id"] = "tampered-source"
    path = _write_json(tmp_path, "source-fence-tampered.json", receipt)
    with pytest.raises(ValueError, match="signature"):
        _schema.verify_source_fence(str(path), str(release_path))


def test_promotion_binds_fence_receipt_to_live_source_identity():
    source = (Path(__file__).resolve().parents[1] / "deploy" / "promote.sh").read_text()
    assert '"${source_probe_base}/internal/source-fence"' in source
    assert 'X-Menhir-Fence-Challenge' in source
    assert 'Ed25519PublicKey.from_public_bytes(public).verify' in source
    assert 'verify-source-fence' in source
    assert '[ "$live_identity" = "$SOURCE_FENCE_ID" ]' in source
    assert 'curl -sS -o "$tmp" -w \'%{http_code}\' --max-time 15' in source
    assert '[ "$code" != "503" ]' in source


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
        'candidate_compose "$generation" run --rm --no-deps -T menhir '
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
