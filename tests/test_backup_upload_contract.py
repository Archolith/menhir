"""Linux contract tests for the Menhir backup upload wrapper (Sol blockers 2-7).

These tests exercise deploy/menhir-backup-upload-contabo.sh on Linux with a
mocked ``aws`` CLI and mocked root/id/tar/stat tooling. Contract under test:

  * happy path fully succeeds (exit 0, finalized receipt, plaintext removed)
  * every failure path exits nonzero, retains the plaintext generation
    byte-for-byte unchanged, and writes no receipt
  * fixed root-owned config/profile: no environment credential overrides
  * provider AES256 + Object Lock COMPLIANCE only
  * at least two distinct regular non-symlink local encrypted generations
  * production backup identity is separate from the sacrificial delete probe
  * delete denial is attempted and proved only on the retained probe version
  * exact direct-child generation containment + name == manifest generation
  * symlinks/special entries inside the generation are refused

Execution contract: these tests require Linux (bash + GNU coreutils) and are
skipped elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="requires Linux bash/GNU coreutils execution contract",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
UPLOAD_SCRIPT = REPO_ROOT / "deploy" / "menhir-backup-upload-contabo.sh"
SCHEMA_PATH = REPO_ROOT / "deploy" / "lib" / "menhir_schema.py"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _generation_tree_hash(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(root))] = _sha256_bytes(path.read_bytes())
    return out


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
        "repo_remotes": {
            "menhir": "https://github.com/Archolith/menhir.git",
            "archolith_oauth": "https://github.com/Archolith/archolith_oauth.git",
            "yawn_deploy": "https://github.com/ctharvey/yawn.deploy.git",
            "yawn_vps": "https://github.com/ctharvey/yawn.vps.git",
        },
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
    authority = json.dumps(
        release, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    release["security_review"] = {
        "schema": 1,
        "kind": "menhir-production-security-review",
        "review_id": "security-review-1",
        "release_author": release["release_author"],
        "reviewer": "independent-security@example.com",
        "reviewed_utc": datetime.now(timezone.utc).isoformat(),
        "authority_sha256": hashlib.sha256(authority).hexdigest(),
        "verdict": "APPROVED",
        "unresolved_findings": {"critical": 0, "high": 0},
        "scope": [
            "authentication-and-oauth-authority",
            "authorization-and-client-tool-policy",
            "backup-restore-and-rollback",
            "host-privilege-and-command-wrappers",
            "network-and-ingress-boundaries",
            "runtime-hardening-and-observability",
            "secret-handling",
            "supply-chain-and-build-evidence",
        ],
        "report_sha256": "a" * 64,
        "review_artifact_sha256": "b" * 64,
    }
    return release


GEN_FILES = {
    "neo4j/neo4j.dump": (b"neo4j-dump", "authority"),
    "neo4j/system.dump": (b"system-dump", "authority"),
    "state/oauth/menhir_oauth_as.db": (b"oauth-db", "authority"),
    "state/telemetry/mcp_telemetry.db": (b"telemetry-db", "authority"),
    "secrets/neo4j/neo4j-auth": (b"neo4j/password", "secret"),
    "secrets/menhir/neo4j-password": (b"password", "secret"),
    "secrets/menhir/operator-key": (b"operator", "secret"),
    "secrets/menhir/source-fence-token": (b"f" * 48, "secret"),
    "secrets/menhir/openai-api-key": (b"provider", "secret"),
    "secrets/oauth/oauth_signing_key.json": (b"{}", "secret"),
    "secrets/oauth/retry-response-keyring.json": (b"{}", "secret"),
    "secrets/oauth/oauth-consent-secret": (b"consent", "secret"),
    "policy/client-policy.json": (b"{}", "config"),
    "config/docker-compose.production.yml": (b"services: {}\n", "config"),
    "config/Dockerfile": (b"FROM scratch\n", "config"),
    "config/production.env": (b"MENHIR_RELEASE_COMMIT=x\n", "config"),
    "config/release.json": (b"{}\n", "config"),
    "config/durable-state-inventory.json": (b"{}\n", "config"),
    "config/commit.txt": (b"a" * 40 + b"\n", "config"),
}


def _build_generation(root: Path) -> tuple[str, str]:
    """Build a minimal valid generation directory. Returns (generation_id, manifest hash)."""
    entries = {}
    for rel, (data, cls) in GEN_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries[rel] = {"sha256": _sha256_bytes(data), "class": cls}

    lines = [f"{entries[rel]['sha256']}  {rel}" for rel in sorted(entries)]
    sha256sums_content = ("\n".join(lines) + "\n").encode()
    (root / "SHA256SUMS").write_bytes(sha256sums_content)
    sha256sums_sha256 = _sha256_bytes(sha256sums_content)

    manifest = {
        "schema": 1,
        "generation": "generation.Test123",
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "build": {
            "repo_commit": "a" * 40,
            "menhir_image": "menhir@sha256:" + "1" * 64,
            "menhir_image_digest": "sha256:" + "1" * 64,
            "neo4j_image": "neo4j@sha256:" + "2" * 64,
            "neo4j_image_digest": "sha256:" + "2" * 64,
        },
        "release": {
            "release_id": "menhir-prod-0.2.0-1",
            "release_manifest_sha256": _sha256_bytes(
                json.dumps(_valid_release()).encode()
            ),
        },
        "restore_order": ["neo4j", "system", "oauth", "telemetry", "secrets", "policy"],
        "files": entries,
        "sha256sums_sha256": sha256sums_sha256,
    }
    manifest_path = root / "MANIFEST.json"
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    (root / "COMPLETE").write_text(manifest_sha256 + "\n")
    return manifest["generation"], manifest_sha256


def _make_mock_aws_script(mock_bin: Path, scenario: str) -> None:
    retention_ok = (
        datetime.now(timezone.utc) + timedelta(days=31)
    ).isoformat().replace("+00:00", "Z")
    retention_bad = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat().replace("+00:00", "Z")
    log = mock_bin / "aws-calls.log"
    script = mock_bin / "aws"
    script.write_text(f'''#!/usr/bin/env python3
import json, sys

scenario = {scenario!r}
args = sys.argv[1:]
args = args[args.index("s3api"):] if "s3api" in args else args
probe_data = b"menhir-object-lock-delete-denial-probe-v1\\n"
key = args[args.index("--key") + 1] if "--key" in args else ""
is_probe = "/worm-delete-denial-probes/" in key
expected_version = "v-probe-456" if is_probe else "v-backup-123"

with open({str(log)!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")

def respond(obj):
    print(json.dumps(obj))

if args[0:2] == ["s3api", "put-object"]:
    respond({{"VersionId": expected_version}})
elif args[0:2] == ["s3api", "put-object-retention"]:
    if not is_probe or args[args.index("--version-id") + 1] != "v-probe-456":
        print("retention applied to wrong object/version", file=sys.stderr)
        sys.exit(1)
    respond({{}})
elif args[0:2] == ["s3api", "head-object"]:
    if args[args.index("--version-id") + 1] != expected_version:
        print("head requested wrong version", file=sys.stderr)
        sys.exit(1)
    if is_probe:
        retention = "{retention_bad}" if scenario == "probe_bad_retention" else "{retention_ok}"
        respond({{"ContentLength": len(probe_data), "ServerSideEncryption": "AES256", "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": retention}})
    elif scenario == "bad_size":
        respond({{"ContentLength": 999, "ServerSideEncryption": "AES256", "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": "{retention_ok}"}})
    elif scenario == "bad_encryption":
        respond({{"ContentLength": 100, "ServerSideEncryption": "aws:kms", "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": "{retention_ok}"}})
    elif scenario == "bad_lock":
        respond({{"ContentLength": 100, "ServerSideEncryption": "AES256", "ObjectLockMode": "GOVERNANCE", "ObjectLockRetainUntilDate": "{retention_ok}"}})
    elif scenario == "bad_retention":
        respond({{"ContentLength": 100, "ServerSideEncryption": "AES256", "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": "{retention_bad}"}})
    else:
        respond({{"ContentLength": 100, "ServerSideEncryption": "AES256", "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": "{retention_ok}"}})
elif args[0:2] == ["s3api", "get-object"]:
    if args[args.index("--version-id") + 1] != expected_version:
        print("readback requested wrong version", file=sys.stderr)
        sys.exit(1)
    out_path = args[-1]
    if is_probe:
        data = b"wrong-probe-data" if scenario == "probe_readback_mismatch" else probe_data
    else:
        data = b"wrong-data" if scenario == "readback_mismatch" else b"x" * 100
    with open(out_path, "wb") as f:
        f.write(data)
    respond({{"ContentLength": len(data)}})
elif args[0:2] == ["s3api", "delete-object"]:
    if not is_probe:
        respond({{"VersionId": "v-backup-123", "DeleteMarker": False}})
        sys.exit(0)
    if scenario == "delete_not_denied":
        respond({{"VersionId": "v-probe-456", "DeleteMarker": False}})
        sys.exit(0)
    if scenario == "delete_indeterminate":
        print("Could not connect to the endpoint URL", file=sys.stderr)
        sys.exit(1)
    print("An error occurred (AccessDenied)", file=sys.stderr)
    sys.exit(1)
else:
    print("unknown aws command", file=sys.stderr)
    sys.exit(1)
''')
    script.chmod(0o755)


def _make_mock_tool(mock_bin: Path, name: str, body: str) -> None:
    script = mock_bin / name
    script.write_text("#!/usr/bin/env python3\n" + body)
    script.chmod(0o755)


class Harness:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.mock_bin = tmp_path / "mock_bin"
        self.generations_root = tmp_path / "generations"
        self.generations_root.mkdir(parents=True)
        self.status_dir = tmp_path / "status"
        self.receipt_path = tmp_path / "status" / "backup-upload-receipt.json"
        self.receipt_root = tmp_path / "status" / "backup-receipts"
        self.local_archive_root = tmp_path / "encrypted"
        self.aws_calls_log = self.mock_bin / "aws-calls.log"
        self.release_path = tmp_path / "release.json"
        self.release_path.write_text(json.dumps(_valid_release()) + "\n")
        self._write_profile_files()
        self._write_config()

    def _write_profile_files(self) -> None:
        creds = self.tmp / "aws-credentials"
        conf = self.tmp / "aws-config"
        creds.write_text("[menhir-backup]\naws_access_key_id=x\n")
        conf.write_text("[profile menhir-backup]\nregion=eu2\n")
        creds.chmod(0o600)
        conf.chmod(0o600)
        self.aws_credentials = creds
        self.aws_config = conf

    def _write_config(self) -> None:
        cfg = self.tmp / "backup-upload.conf"
        cfg.write_text("\n".join([
            "bucket=test-bucket",
            "archive_prefix=archive/",
            f"receipt_path={self.receipt_path}",
            f"receipt_root={self.receipt_root}",
            f"status_dir={self.status_dir}",
            f"generations_root={self.generations_root}",
            f"local_archive_root={self.local_archive_root}",
            "local_retention_generations=2",
            f"release_json={self.release_path}",
            "aws_profile=menhir-backup",
            "aws_region=eu2",
            f"aws_credentials={self.aws_credentials}",
            f"aws_config={self.aws_config}",
            "age_recipient=age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
            "",
        ]))
        cfg.chmod(0o600)
        self.config_path = cfg

    def build_generation(self) -> Path:
        gen_dir = self.generations_root / "generation.Test123"
        gen_dir.mkdir()
        _build_generation(gen_dir)
        return gen_dir

    def seed_local_archive(
        self, generation: str = "generation.Prior123", data: bytes = b"prior-archive"
    ) -> Path:
        self.local_archive_root.mkdir(parents=True, exist_ok=True)
        path = self.local_archive_root / (
            f"{generation}-20260801T000000Z-0123456789abcdef.tar.gz.age"
        )
        path.write_bytes(data)
        path.chmod(0o400)
        return path

    def run(self, gen_dir: Path | None = None, scenario: str = "happy") -> subprocess.CompletedProcess:
        if not self.mock_bin.exists():
            self.mock_bin.mkdir(parents=True)
            _make_mock_tool(self.mock_bin, "id", 'print("0")\n')
            # stat: owner is always root; mode is always 0600; size is real.
            _make_mock_tool(self.mock_bin, "stat", '''import os, sys
fmt = sys.argv[2]
path = sys.argv[3]
if "%s" in fmt:
    print(os.path.getsize(path))
elif "%u" in fmt:
    print("0")
elif "%a" in fmt:
    print("600")
else:
    sys.exit(1)
''')
            # tar: deterministic 100-byte archive so head/get sizes match.
            _make_mock_tool(self.mock_bin, "tar", '''import sys
args = sys.argv[1:]
flag = "czf" if "czf" in args else ("-czf" if "-czf" in args else None)
if flag is not None:
    archive = args[args.index(flag) + 1]
    with open(archive, "wb") as f:
        f.write(b"x" * 100)
''')
            # age: deterministic encrypted-object stand-in. The contract tests
            # assert invocation/config/receipt semantics; real age interoperability
            # belongs to the live clean-host restore gate.
            _make_mock_tool(self.mock_bin, "age", '''import pathlib, sys
args = sys.argv[1:]
out = args[args.index("--output") + 1]
pathlib.Path(out).write_bytes(b"x" * 100)
''')
        # Regenerate the aws mock per run so the scenario always applies.
        _make_mock_aws_script(self.mock_bin, scenario)

        env = os.environ.copy()
        for key in list(env):
            if key.startswith("AWS_"):
                del env[key]
        env["PATH"] = str(self.mock_bin) + os.pathsep + env.get("PATH", "")
        env["MENHIR_BACKUP_UPLOAD_CONFIG"] = str(self.config_path)
        env["MENHIR_OPERATION_JOB_ID"] = "test-backup-job-1"
        env["MENHIR_BACKUP_UPLOAD_ALLOW_NON_ROOT_TEST"] = "1"
        env["MENHIR_BACKUP_UPLOAD_TEST_BIN"] = str(REPO_ROOT / "deploy" / "lib")
        target = str(
            gen_dir if gen_dir is not None
            else self.generations_root / "generation.Test123"
        )
        return subprocess.run(
            ["bash", str(UPLOAD_SCRIPT), target],
            capture_output=True, text=True, env=env,
        )

    def aws_calls(self) -> list[list[str]]:
        if not self.aws_calls_log.exists():
            return []
        return [json.loads(line) for line in self.aws_calls_log.read_text().splitlines()]

    def assert_generation_unchanged(self, gen_dir: Path, before: dict[str, str]) -> None:
        assert gen_dir.is_dir(), "plaintext generation directory must be retained"
        assert _generation_tree_hash(gen_dir) == before, "plaintext generation changed"

    def assert_no_receipt(self) -> None:
        assert not self.receipt_path.exists(), "no receipt may exist after failure"


@pytest.fixture()
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# ---------------------------------------------------------------------------
# Happy path: final success must actually be possible
# ---------------------------------------------------------------------------

def test_upload_happy_path_fully_succeeds(harness: Harness):
    gen_dir = harness.build_generation()
    prior_archive = harness.seed_local_archive()

    result = harness.run(gen_dir, scenario="happy")

    assert result.returncode == 0, result.stderr
    assert "Backup upload complete" in result.stdout

    receipt = json.loads(harness.receipt_path.read_text())
    assert receipt["operation_job_id"] == "test-backup-job-1"
    assert receipt["plaintext_removed"] is True
    production = receipt["offhost"]["production_backup"]
    probe = receipt["offhost"]["sacrificial_probe"]
    assert production["server_side_encryption"] == "AES256"
    assert production["lock_mode"] == "COMPLIANCE"
    assert probe["server_side_encryption"] == "AES256"
    assert probe["lock_mode"] == "COMPLIANCE"
    assert probe["version_readback_verified"] is True
    assert probe["locked_version_delete_denied"] is True
    assert probe["version_persisted_after_delete_denial"] is True
    assert production["object_key"] != probe["object_key"]
    assert production["version_id"] != probe["version_id"]

    local = receipt["local_encrypted_archives"]
    assert local["minimum_retained_generations"] == 2
    assert local["retained_generation_count"] == 2
    archives = local["archives"]
    assert {item["generation"] for item in archives} == {
        "generation.Prior123", "generation.Test123",
    }
    assert all(Path(item["path"]).is_file() for item in archives)
    current = next(item for item in archives if item["generation"] == "generation.Test123")
    assert local["current_archive_path"] == current["path"]
    assert current["sha256"] == production["object_sha256"]
    assert current["size"] == production["object_size"]
    assert prior_archive.is_file(), "retained local archives must never be pruned"
    assert (harness.receipt_root / "generation.Test123.json").is_file()

    proc = subprocess.run(
        [sys.executable, str(SCHEMA_PATH), "validate-receipt",
         str(harness.receipt_path), "backup-upload"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        [sys.executable, str(SCHEMA_PATH), "validate-backup-promotion",
         str(harness.receipt_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr

    assert not gen_dir.exists()

    calls = harness.aws_calls()
    puts = [c for c in calls if c[0:2] == ["s3api", "put-object"]]
    put = next(c for c in puts if "/worm-delete-denial-probes/" not in c[c.index("--key") + 1])
    probe_put = next(c for c in puts if "/worm-delete-denial-probes/" in c[c.index("--key") + 1])
    assert put[put.index("--server-side-encryption") + 1] == "AES256"
    assert put[put.index("--object-lock-mode") + 1] == "COMPLIANCE"
    key = put[put.index("--key") + 1]
    assert key.startswith("archive/generation.Test123/")
    assert key.endswith(".tar.gz.age")
    metadata = put[put.index("--metadata") + 1]
    assert "generation=generation.Test123" in metadata
    assert "manifest-sha256=" in metadata
    assert "object-sha256=" in metadata
    assert "client-encryption=age-x25519" in metadata
    assert production["client_encryption"]["algorithm"] == "age-x25519"

    probe_key = probe_put[probe_put.index("--key") + 1]
    assert probe_key == probe["object_key"]
    retention = next(c for c in calls if c[0:2] == ["s3api", "put-object-retention"])
    assert retention[retention.index("--key") + 1] == probe_key
    assert retention[retention.index("--version-id") + 1] == probe["version_id"]
    assert "Mode=COMPLIANCE" in retention[retention.index("--retention") + 1]

    kinds = [tuple(c[0:2]) for c in calls]
    assert kinds.count(("s3api", "head-object")) == 3
    assert kinds.count(("s3api", "get-object")) == 3
    deletes = [c for c in calls if c[0:2] == ["s3api", "delete-object"]]
    assert len(deletes) == 1
    assert deletes[0][deletes[0].index("--key") + 1] == probe["object_key"]
    assert deletes[0][deletes[0].index("--version-id") + 1] == probe["version_id"]
    assert production["object_key"] not in deletes[0]
    assert production["version_id"] not in deletes[0]


def test_upload_requires_two_distinct_regular_local_generations(harness: Harness):
    gen_dir = harness.build_generation()
    before = _generation_tree_hash(gen_dir)
    harness.local_archive_root.mkdir(parents=True)
    symlink = harness.local_archive_root / (
        "generation.Prior123-20260801T000000Z-0123456789abcdef.tar.gz.age"
    )
    symlink.symlink_to(harness.tmp / "not-an-archive")

    result = harness.run(gen_dir, scenario="happy")

    assert result.returncode != 0
    assert "fewer than configured minimum distinct retained generations" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    assert symlink.is_symlink()
    harness.assert_no_receipt()
    assert harness.aws_calls() == []


# ---------------------------------------------------------------------------
# Failure paths: zero success, unchanged plaintext, no receipt
# ---------------------------------------------------------------------------

HEAD_SCENARIOS = [
    ("bad_size", "head size mismatch"),
    ("bad_encryption", "head encryption mismatch"),
    ("bad_lock", "head lock mode mismatch"),
    ("bad_retention", "head retention insufficient"),
]


@pytest.mark.parametrize("scenario,signature", HEAD_SCENARIOS)
def test_upload_head_failures_retain_plaintext(harness: Harness, scenario: str, signature: str):
    gen_dir = harness.build_generation()
    harness.seed_local_archive()
    before = _generation_tree_hash(gen_dir)

    result = harness.run(gen_dir, scenario=scenario)

    assert result.returncode != 0
    assert signature in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    harness.assert_no_receipt()


def test_upload_readback_mismatch_retains_plaintext(harness: Harness):
    gen_dir = harness.build_generation()
    harness.seed_local_archive()
    before = _generation_tree_hash(gen_dir)

    result = harness.run(gen_dir, scenario="readback_mismatch")

    assert result.returncode != 0
    assert "readback hash mismatch" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    harness.assert_no_receipt()


def test_upload_delete_not_denied_retains_plaintext(harness: Harness):
    gen_dir = harness.build_generation()
    harness.seed_local_archive()
    before = _generation_tree_hash(gen_dir)

    result = harness.run(gen_dir, scenario="delete_not_denied")

    assert result.returncode != 0
    assert "delete-object succeeded" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    harness.assert_no_receipt()


def test_upload_requires_explicit_probe_delete_denial(harness: Harness):
    gen_dir = harness.build_generation()
    harness.seed_local_archive()
    before = _generation_tree_hash(gen_dir)

    result = harness.run(gen_dir, scenario="delete_indeterminate")

    assert result.returncode != 0
    assert "did not return an explicit retention denial" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    harness.assert_no_receipt()


def test_upload_rejects_symlink_inside_generation(harness: Harness):
    gen_dir = harness.build_generation()
    before = _generation_tree_hash(gen_dir)
    (gen_dir / "neo4j" / "linked.dump").symlink_to(gen_dir / "neo4j" / "neo4j.dump")

    result = harness.run(gen_dir)

    assert result.returncode != 0
    assert "symlink or special entry" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    assert (gen_dir / "neo4j" / "linked.dump").is_symlink()
    harness.assert_no_receipt()


def test_upload_enforces_direct_child_containment(harness: Harness):
    nested_parent = harness.generations_root / "subdir"
    nested_parent.mkdir()
    gen_dir = nested_parent / "generation.Test123"
    gen_dir.mkdir()
    _build_generation(gen_dir)
    before = _generation_tree_hash(gen_dir)

    result = harness.run(gen_dir)

    assert result.returncode != 0
    assert "direct child of generations_root" in result.stderr
    harness.assert_generation_unchanged(gen_dir, before)
    harness.assert_no_receipt()


def test_upload_enforces_directory_name_matches_manifest(harness: Harness, tmp_path: Path):
    wrong = harness.generations_root / "generation.Wrong999"
    wrong.mkdir()
    _build_generation(wrong)  # manifest says generation.Test123
    before = _generation_tree_hash(wrong)

    result = harness.run(wrong)

    assert result.returncode != 0
    assert "must be generation.<alnum>" in result.stderr or \
        "!= manifest generation" in result.stderr
    harness.assert_generation_unchanged(wrong, before)
    harness.assert_no_receipt()


def test_schema_rejects_symlink_in_generation(harness: Harness, tmp_path: Path):
    gen_dir = harness.build_generation()
    (gen_dir / "extra").symlink_to(gen_dir / "COMPLETE")
    proc = subprocess.run(
        [sys.executable, str(SCHEMA_PATH), "validate-manifest",
         str(gen_dir / "MANIFEST.json"), str(gen_dir)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "symlink or special entry" in proc.stderr


def test_pending_receipt_is_valid_but_blocked_from_promotion(tmp_path: Path):
    receipt = {
        "schema": 1,
        "kind": "backup-upload",
        "operation_job_id": "test-backup-job-1",
        "generation": "generation.Abc",
        "manifest_sha256": "a" * 64,
        "release": {
            "release_id": "r",
            "release_manifest_sha256": "b" * 64,
            "menhir_image_digest": "sha256:" + "1" * 64,
            "neo4j_image_digest": "sha256:" + "2" * 64,
        },
        "offhost": {
            "bucket": "b",
            "production_backup": {
                "object_key": "archive/generation.Abc/backup.tar.gz.age",
                "version_id": "v-backup",
                "object_sha256": "c" * 64,
                "object_size": 100,
                "server_side_encryption": "AES256",
                "lock_mode": "COMPLIANCE",
                "worm_retention_until": datetime.now(timezone.utc).isoformat(),
                "version_readback_verified": True,
                "client_encryption": {
                    "algorithm": "age-x25519",
                    "recipient": "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
                    "plaintext_archive_sha256": "d" * 64,
                },
            },
            "sacrificial_probe": {
                "object_key": "archive/worm-delete-denial-probes/generation.Abc/probe",
                "version_id": "v-probe",
                "object_sha256": "e" * 64,
                "object_size": 42,
                "server_side_encryption": "AES256",
                "lock_mode": "COMPLIANCE",
                "worm_retention_until": datetime.now(timezone.utc).isoformat(),
                "version_readback_verified": True,
                "locked_version_delete_denied": True,
                "version_persisted_after_delete_denial": True,
            },
        },
        "local_encrypted_archives": {
            "minimum_retained_generations": 2,
            "retained_generation_count": 2,
            "current_archive_path": "/srv/menhir/backups/encrypted/generation.Abc-current.tar.gz.age",
            "archives": [
                {
                    "generation": "generation.Abc",
                    "path": "/srv/menhir/backups/encrypted/generation.Abc-current.tar.gz.age",
                    "sha256": "c" * 64,
                    "size": 100,
                },
                {
                    "generation": "generation.Prior",
                    "path": "/srv/menhir/backups/encrypted/generation.Prior-old.tar.gz.age",
                    "sha256": "f" * 64,
                    "size": 99,
                },
            ],
        },
        "plaintext_removed": False,
        "checked_utc": datetime.now(timezone.utc).isoformat(),
    }
    path = tmp_path / "pending-receipt.json"
    path.write_text(json.dumps(receipt) + "\n")
    validate_cmd = [sys.executable, str(SCHEMA_PATH)]
    ok = subprocess.run(
        validate_cmd + ["validate-receipt", str(path), "backup-upload"],
        capture_output=True, text=True,
    )
    assert ok.returncode == 0, ok.stderr
    blocked = subprocess.run(
        validate_cmd + ["validate-backup-promotion", str(path)],
        capture_output=True, text=True,
    )
    assert blocked.returncode != 0
    assert "plaintext removal" in blocked.stderr
