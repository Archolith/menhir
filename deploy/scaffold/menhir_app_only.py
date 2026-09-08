#!/usr/bin/env python3
"""Classify and execute bounded Menhir application-image-only releases."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - pure classifier tests run on Windows
    fcntl = None  # type: ignore[assignment]


ROOT = Path("/srv/menhir/production")
STATUS = Path("/var/lib/menhir-production")
UPLOAD_ROOT = Path("/home/thron/.menhir-app-only-upload")
COMPOSE = ROOT / "deploy/docker-compose.production.yml"
LIVE_RELEASE = ROOT / "release/release.json"
LIVE_ENV = ROOT / "release/production.env"
LIVE_POLICY = ROOT / "policy/client-policy.json"
SCHEMA = ROOT / "bin/menhir_schema.py"
SCAFFOLD = Path("/srv/menhir/scaffold/bin/menhir_scaffold.py")
ADMISSION_LOCK = Path("/run/lock/menhir-production-admission.lock")
MUTATION_LOCK = Path("/run/lock/menhir-production.lock")
ACTIVE = STATUS / "app-only-active.json"
LAST = STATUS / "app-only-last.json"
SECURITY_CONFIG_ACTIVE = STATUS / "security-config-active.json"
MAINTENANCE_ACTIVE = STATUS / "release-run.json"
PROBE_CLIENT_ID = "menhir-deploy-probe"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}")
BUNDLE_ID = re.compile(r"[a-f0-9]{32}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
ALLOWED_ENV_CHANGES = {"MENHIR_IMAGE", "MENHIR_RELEASE_COMMIT", "MENHIR_RELEASE_ID"}
APP_ONLY_SOURCE_PATTERNS = tuple(re.compile(value) for value in (
    r"^src/menhir/explorer/static/[^/]+$",
    r"^src/menhir/explorer/templates/[^/]+$",
))
ALLOWED_RELEASE_SCALARS = (
    ("release_id",),
    ("repos", "menhir"),
    ("images", "menhir"),
    ("provenance_sha256",),
    ("sbom_sha256",),
    ("scan_evidence_sha256",),
    ("wheel_manifest_sha256",),
    ("dockerfile_wheel_manifest_sha256",),
    ("rendered", "production_env_sha256"),
)
STAGING_RECEIPT_NAME = "staging-receipt.json"
APPROVAL_NAME = "promotion-approval.json"
STAGING_KEYS = {
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "deployment_class", "ingress_mode", "images",
    "runner_sha256", "started_utc", "completed_utc", "test_identities",
    "checks", "production_preflight",
}
APPROVAL_KEYS = {
    "schema", "kind", "release_id", "release_sha256", "bundle_sha256",
    "staging_receipt_sha256", "approved_by", "approved_utc",
    "promotion_wrapper_sha256", "operator_wrapper_sha256", "root_runner_sha256",
}
PREFLIGHT_KEYS = {
    "schema", "kind", "result", "observed_utc", "deployment_class",
    "candidate_release_id", "ingress_mode", "checks", "canonical_sha256",
}
STAGING_CHECKS = {
    "artifact_identity", "production_memory_limits", "production_network_shape",
    "oauth_policy_shape", "ingress_request_handling", "isolated_disposable_data",
    "non_production_credentials", "production_authority_absent", "oauth_discovery",
    "oauth_authorization_code_pkce", "mcp_initialize", "mcp_tools_list", "mcp_recall",
    "synthetic_write_allowed", "denied_operation_refused", "restart_persistence",
    "automatic_rollback",
}


class AppOnlyError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def is_app_only_source_path(path: str) -> bool:
    """Return whether a source path is proven safe for the no-backup lane."""
    return any(pattern.fullmatch(path) for pattern in APP_ONLY_SOURCE_PATTERNS)


def strict_load(path: Path) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AppOnlyError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppOnlyError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AppOnlyError(f"JSON root must be an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def composite_sha256(parts: tuple[tuple[str, Path], ...]) -> str:
    """Bind multiple imported executables into one reviewable authority digest."""
    digest = hashlib.sha256()
    for label, path in parts:
        digest.update(f"{label}\0{sha256(path)}\n".encode("ascii"))
    return digest.hexdigest()


def parse_utc(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise AppOnlyError(f"{label} must be an explicit UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise AppOnlyError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise AppOnlyError(f"{label} must be UTC")
    return parsed.astimezone(dt.timezone.utc)


def require_root_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise AppOnlyError(f"{label} must be a regular non-symlink file")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise AppOnlyError(f"{label} must be root-owned and not group/other writable")


def require_upload(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 1000:
        raise AppOnlyError(f"{label} must be a regular non-symlink file owned by thron")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError(f"{label} must have mode 0600 or stricter")


def run(command: list[str], timeout: int, *, input_bytes: bytes | None = None) -> str:
    try:
        result = subprocess.run(
            command, input=input_bytes, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppOnlyError(f"command failed: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-1000:].strip()
        raise AppOnlyError(f"command exited {result.returncode}: {' '.join(command)}: {stderr}")
    return result.stdout.decode("utf-8", "replace").strip()


def atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, 0, 0)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("ascii"),
        0o400,
    )


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AppOnlyError(f"cannot read production environment: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#") or "=" not in line:
            raise AppOnlyError(f"production environment line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in result:
            raise AppOnlyError(f"production environment key is invalid or duplicated: {key}")
        if not value or any(char in value for char in "\r\n\x00"):
            raise AppOnlyError(f"production environment value is invalid: {key}")
        result[key] = value
    return result


def set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    target: Any = value
    for key in path[:-1]:
        if not isinstance(target, dict) or key not in target:
            raise AppOnlyError("release is missing required field: " + "/".join(path))
        target = target[key]
    if not isinstance(target, dict) or path[-1] not in target:
        raise AppOnlyError("release is missing required field: " + "/".join(path))
    target[path[-1]] = replacement


def json_differences(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "/"]
    if isinstance(left, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}/{key}"
            if key not in left or key not in right:
                differences.append(path)
            else:
                differences.extend(json_differences(left[key], right[key], path))
        return differences
    if isinstance(left, list):
        return [] if left == right else [prefix or "/"]
    return [] if left == right else [prefix or "/"]


def classify_release(
    live: dict[str, Any], candidate: dict[str, Any], live_sha: str,
    live_env: dict[str, str], candidate_env: dict[str, str], candidate_env_sha: str,
) -> dict[str, Any]:
    live_id = live.get("release_id")
    candidate_id = candidate.get("release_id")
    if not isinstance(live_id, str) or not isinstance(candidate_id, str) or not ID.fullmatch(candidate_id):
        raise AppOnlyError("release identity is invalid")
    if candidate_id == live_id:
        raise AppOnlyError("changed app image must use a new immutable release_id")
    live_image = live.get("images", {}).get("menhir")
    candidate_image = candidate.get("images", {}).get("menhir")
    if not isinstance(candidate_image, str) or not DIGEST.fullmatch(candidate_image):
        raise AppOnlyError("candidate Menhir image digest is invalid")
    if candidate_image == live_image:
        raise AppOnlyError("app-only release does not change the Menhir image")

    rollback = candidate.get("rollback_anchors")
    expected_rollback = {
        "prior_release_id": live_id,
        "prior_release_sha256": live_sha,
        "prior_images": {
            "menhir": live_image,
            "neo4j": live.get("images", {}).get("neo4j"),
            "caddy": live.get("images", {}).get("caddy"),
        },
    }
    if not isinstance(rollback, dict) or any(
        rollback.get(key) != value for key, value in expected_rollback.items()
    ):
        raise AppOnlyError("candidate rollback anchors do not bind the exact live release")

    if set(live_env) != set(candidate_env):
        raise AppOnlyError("candidate production environment adds or removes keys")
    changed_env = sorted(key for key in live_env if live_env[key] != candidate_env[key])
    if not changed_env or not set(changed_env).issubset(ALLOWED_ENV_CHANGES):
        raise AppOnlyError("protected production environment differs: " + ", ".join(changed_env))
    expected_env = {
        "MENHIR_RELEASE_ID": candidate_id,
        "MENHIR_RELEASE_COMMIT": candidate.get("repos", {}).get("menhir"),
    }
    for key, value in expected_env.items():
        if candidate_env.get(key) != value:
            raise AppOnlyError(f"candidate environment {key} is not release-bound")
    image_ref = candidate_env.get("MENHIR_IMAGE", "")
    if not image_ref.endswith("@" + candidate_image):
        raise AppOnlyError("candidate MENHIR_IMAGE is not bound to the release digest")
    live_image_ref = live_env.get("MENHIR_IMAGE", "")
    if image_ref.rsplit("@", 1)[0] != live_image_ref.rsplit("@", 1)[0]:
        raise AppOnlyError("candidate changes the Menhir image repository")
    if candidate_env.get("NEO4J_IMAGE") != live_env.get("NEO4J_IMAGE"):
        raise AppOnlyError("candidate changes the Neo4j image reference")
    if candidate.get("rendered", {}).get("production_env_sha256") != candidate_env_sha:
        raise AppOnlyError("candidate production.env digest is not release-bound")

    normalized = copy.deepcopy(candidate)
    for path in ALLOWED_RELEASE_SCALARS:
        target: Any = live
        for key in path:
            target = target[key]
        set_path(normalized, path, target)
    normalized["security_review"] = copy.deepcopy(live.get("security_review"))
    normalized["rollback_anchors"] = copy.deepcopy(live.get("rollback_anchors"))

    live_artifacts = live.get("artifacts")
    candidate_artifacts = normalized.get("artifacts")
    if not isinstance(live_artifacts, dict) or not isinstance(candidate_artifacts, dict):
        raise AppOnlyError("release artifact authority is missing")
    env_artifact = "/srv/menhir/production/release/production.env"
    if env_artifact not in candidate_artifacts or env_artifact not in live_artifacts:
        raise AppOnlyError("production.env artifact authority is missing")
    candidate_artifacts[env_artifact] = copy.deepcopy(live_artifacts[env_artifact])
    for path, row in candidate_artifacts.items():
        prior = live_artifacts.get(path)
        if not isinstance(row, dict) or not isinstance(prior, dict):
            continue
        if row.get("repository") == "menhir" and prior.get("repository") == "menhir":
            without_commit = {key: value for key, value in row.items() if key != "commit"}
            prior_without_commit = {key: value for key, value in prior.items() if key != "commit"}
            if without_commit == prior_without_commit and "commit" in row and "commit" in prior:
                row["commit"] = prior["commit"]

    differences = json_differences(live, normalized)
    if differences:
        raise AppOnlyError(
            "protected release surfaces differ: " + ", ".join(differences[:12])
        )
    return {
        "classification": "app-only",
        "live_release_id": live_id,
        "candidate_release_id": candidate_id,
        "prior_image": live_image,
        "candidate_image": candidate_image,
        "changed_environment_keys": changed_env,
    }


def validate_source_manifest(
    manifest: dict[str, Any], release_sha: str, env_sha: str, candidate_id: str,
) -> None:
    if set(manifest) != {"schema", "kind", "release_id", "release_sha256", "files"}:
        raise AppOnlyError("source install-bundle manifest keys mismatch")
    if manifest.get("schema") != 1 or manifest.get("kind") != "menhir-release-install-bundle":
        raise AppOnlyError("source install-bundle manifest schema mismatch")
    if manifest.get("release_id") != candidate_id or manifest.get("release_sha256") != release_sha:
        raise AppOnlyError("source install-bundle release binding mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise AppOnlyError("source install-bundle files are missing")
    required = {
        "/srv/menhir/production/release/release.json": release_sha,
        "/srv/menhir/production/release/production.env": env_sha,
    }
    for path, digest in required.items():
        row = files.get(path)
        if not isinstance(row, dict) or row.get("sha256") != digest:
            raise AppOnlyError(f"source install-bundle does not bind {path}")


def recompute_source_classification(
    history_bundle: Path, live_commit: str, candidate_commit: str,
) -> tuple[list[str], list[str]]:
    """Recompute the protected-path decision from commit-addressed Git objects."""
    if history_bundle.stat().st_size > 128 * 1024 * 1024:
        raise AppOnlyError("source history bundle is unexpectedly large")
    with tempfile.TemporaryDirectory(prefix="menhir-app-only-git-") as temporary:
        repository = Path(temporary) / "repository.git"
        run(["git", "clone", "--quiet", "--bare", str(history_bundle), str(repository)], 30)
        observed_live = run(
            ["git", "-C", str(repository), "rev-parse", "refs/heads/live^{commit}"], 10,
        )
        observed_candidate = run(
            ["git", "-C", str(repository), "rev-parse", "refs/heads/candidate^{commit}"], 10,
        )
        if observed_live != live_commit or observed_candidate != candidate_commit:
            raise AppOnlyError("source history bundle is not bound to the release commits")
        output = run([
            "git", "-C", str(repository), "diff", "--name-only",
            "--diff-filter=ACDMRTUXB", observed_live, observed_candidate,
        ], 30)
    changed = sorted(set(output.splitlines()))
    if not changed or any(
        not path or path.startswith("/") or ".." in path.split("/") for path in changed
    ):
        raise AppOnlyError("trusted source diff is empty or invalid")
    forbidden = sorted(path for path in changed if not is_app_only_source_path(path))
    return changed, forbidden


def validate_promotion_authority(
    bundle: Path,
    release: dict[str, Any],
    *,
    deployment_class: str,
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_bundle_sha256: str,
    expected_staging_receipt_sha256: str,
    expected_approval_sha256: str,
    expected_ingress_container_id: str,
    promotion_attempt_id: str,
    approved_utc: str,
    promotion_started_utc: str,
) -> dict[str, Any]:
    """Independently prove the desktop approval at the privileged boundary."""
    for value, label in (
        (expected_runner_sha256, "root runner"),
        (expected_release_sha256, "release"),
        (expected_bundle_sha256, "bundle"),
        (expected_staging_receipt_sha256, "staging receipt"),
        (expected_approval_sha256, "approval"),
        (expected_ingress_container_id, "Cloudflared container"),
    ):
        if not isinstance(value, str) or HEX64.fullmatch(value) is None:
            raise AppOnlyError(f"expected {label} SHA-256 is malformed")
    if not isinstance(promotion_attempt_id, str) or BUNDLE_ID.fullmatch(promotion_attempt_id) is None:
        raise AppOnlyError("promotion attempt identity is malformed")

    staging_path = bundle / STAGING_RECEIPT_NAME
    approval_path = bundle / APPROVAL_NAME
    if sha256(staging_path) != expected_staging_receipt_sha256:
        raise AppOnlyError("staging receipt differs from the approved authority")
    if sha256(approval_path) != expected_approval_sha256:
        raise AppOnlyError("approval artifact differs from the approved authority")
    staging = strict_load(staging_path)
    approval = strict_load(approval_path)
    if set(staging) != STAGING_KEYS or staging.get("schema") != 1 \
            or staging.get("kind") != "menhir-personal-staging" \
            or staging.get("result") != "passed":
        raise AppOnlyError("staging receipt schema is invalid")
    if set(approval) != APPROVAL_KEYS or approval.get("schema") != 1 \
            or approval.get("kind") != "menhir-personal-promotion-approval":
        raise AppOnlyError("approval artifact schema is invalid")

    release_id = release.get("release_id")
    release_path = bundle / "release.json"
    if not release_path.exists():
        release_path = bundle / "candidate-release.json"
    if release.get("deployment_class") != deployment_class \
            or release.get("ingress_mode") != "cloudflared" \
            or sha256(release_path) != expected_release_sha256:
        raise AppOnlyError("release does not match the privileged promotion authority")
    expected_staging = {
        "release_id": release_id,
        "release_sha256": expected_release_sha256,
        "bundle_sha256": expected_bundle_sha256,
        "deployment_class": deployment_class,
        "ingress_mode": "cloudflared",
        "images": {
            "menhir": release.get("images", {}).get("menhir"),
            "neo4j": release.get("images", {}).get("neo4j"),
        },
    }
    for key, expected in expected_staging.items():
        if staging.get(key) != expected:
            raise AppOnlyError(f"staging receipt {key} is not release-bound")
    checks = staging.get("checks")
    if not isinstance(checks, dict) or set(checks) != STAGING_CHECKS \
            or any(value is not True for value in checks.values()):
        raise AppOnlyError("staging receipt does not prove every required check")
    preflight = staging.get("production_preflight")
    if not isinstance(preflight, dict) or set(preflight) != PREFLIGHT_KEYS:
        raise AppOnlyError("production preflight schema is invalid")
    unsealed = copy.deepcopy(preflight)
    declared_seal = unsealed.pop("canonical_sha256", None)
    calculated_seal = hashlib.sha256(json.dumps(
        unsealed, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if declared_seal != calculated_seal:
        raise AppOnlyError("production preflight seal is invalid")
    if preflight.get("schema") != 1 \
            or preflight.get("kind") != "menhir-production-readiness-preflight" \
            or preflight.get("result") != "passed" \
            or preflight.get("deployment_class") != deployment_class \
            or preflight.get("candidate_release_id") != release_id \
            or preflight.get("ingress_mode") != "cloudflared":
        raise AppOnlyError("production preflight is not release-bound")
    preflight_checks = preflight.get("checks")
    identities = (
        preflight_checks.get("network_roles", {}).get("ingress", {}).get("identities")
        if isinstance(preflight_checks, dict) else None
    )
    if not isinstance(identities, list) or len(identities) != 1 \
            or not isinstance(identities[0], dict) \
            or identities[0].get("container_id") != expected_ingress_container_id:
        raise AppOnlyError("production preflight does not bind the expected Cloudflared identity")

    expected_approval = {
        "release_id": release_id,
        "release_sha256": expected_release_sha256,
        "bundle_sha256": expected_bundle_sha256,
        "staging_receipt_sha256": expected_staging_receipt_sha256,
        "root_runner_sha256": expected_runner_sha256,
        "approved_utc": approved_utc,
    }
    for key, expected in expected_approval.items():
        if approval.get(key) != expected:
            raise AppOnlyError(f"approval artifact {key} is not promotion-bound")
    for key in ("promotion_wrapper_sha256", "operator_wrapper_sha256"):
        if not isinstance(approval.get(key), str) or HEX64.fullmatch(approval[key]) is None:
            raise AppOnlyError(f"approval artifact {key} is invalid")
    if not isinstance(approval.get("approved_by"), str) \
            or re.fullmatch(r"[A-Za-z0-9._@+-]{1,128}", approval["approved_by"]) is None:
        raise AppOnlyError("approval identity is invalid")

    staging_started = parse_utc(staging.get("started_utc"), "staging start")
    staging_completed = parse_utc(staging.get("completed_utc"), "staging completion")
    preflight_observed = parse_utc(preflight.get("observed_utc"), "preflight observation")
    approved = parse_utc(approved_utc, "approval time")
    promotion_started = parse_utc(promotion_started_utc, "promotion start")
    if staging_completed < staging_started or preflight_observed > staging_started \
            or approved < staging_completed \
            or promotion_started < approved \
            or promotion_started > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1):
        raise AppOnlyError("promotion authority chronology is invalid")
    staging_runner = staging.get("runner_sha256")
    if not isinstance(staging_runner, str) or HEX64.fullmatch(staging_runner) is None:
        raise AppOnlyError("staging runner authority is invalid")
    return {
        "bundle_sha256": expected_bundle_sha256,
        "staging_receipt_sha256": expected_staging_receipt_sha256,
        "staging_runner_sha256": staging_runner,
        "staging_started_utc": staging["started_utc"],
        "staging_completed_utc": staging["completed_utc"],
        "approval_sha256": expected_approval_sha256,
        "approved_by": approval["approved_by"],
        "approved_utc": approval["approved_utc"],
        "promotion_wrapper_sha256": approval["promotion_wrapper_sha256"],
        "operator_wrapper_sha256": approval["operator_wrapper_sha256"],
        "promotion_attempt_id": promotion_attempt_id,
        "promotion_started_utc": promotion_started_utc,
    }


def load_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> dict[str, Any]:
    if not BUNDLE_ID.fullmatch(bundle_id):
        raise AppOnlyError("bundle id must be 32 lowercase hexadecimal characters")
    bundle = UPLOAD_ROOT / f"app-{bundle_id}"
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise AppOnlyError("uploaded app-only bundle is missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError("uploaded app-only bundle must be a private directory owned by thron")
    base_names = (
        "app-only-manifest.json", "release.json", "production.env",
        "source-manifest.json", "classification-evidence.json", "source-history.bundle",
    )
    names = base_names + ((STAGING_RECEIPT_NAME, APPROVAL_NAME) if require_authority else ())
    for name in names:
        require_upload(bundle / name, name)
    manifest = strict_load(bundle / "app-only-manifest.json")
    if set(manifest) != {"schema", "kind", "source_bundle_sha256", "files"} \
            or manifest.get("schema") != 1 or manifest.get("kind") != "menhir-app-only-bundle":
        raise AppOnlyError("app-only bundle manifest schema mismatch")
    expected_files = set(names) - {"app-only-manifest.json"}
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected_files:
        raise AppOnlyError("app-only bundle file set mismatch")
    for name in expected_files:
        if files[name] != sha256(bundle / name):
            raise AppOnlyError(f"app-only bundle digest mismatch: {name}")
    if manifest["source_bundle_sha256"] != sha256(bundle / "source-manifest.json"):
        raise AppOnlyError("app-only source manifest digest mismatch")
    if destination is not None:
        destination.mkdir(parents=True, mode=0o700)
        os.chown(destination, 0, 0)
        for name in names:
            atomic_bytes(destination / name, (bundle / name).read_bytes(), 0o400)
        bundle = destination
    release = strict_load(bundle / "release.json")
    env = parse_env(bundle / "production.env")
    source = strict_load(bundle / "source-manifest.json")
    evidence = strict_load(bundle / "classification-evidence.json")
    release_sha = sha256(bundle / "release.json")
    env_sha = sha256(bundle / "production.env")
    validate_source_manifest(source, release_sha, env_sha, str(release.get("release_id", "")))
    if set(evidence) != {
        "schema", "kind", "live_commit", "candidate_commit", "changed_paths",
        "forbidden_matches", "generated_utc",
    } or evidence.get("schema") != 1 or evidence.get("kind") != "menhir-app-only-classification":
        raise AppOnlyError("app-only classification evidence schema mismatch")
    changed_paths = evidence.get("changed_paths")
    if not isinstance(changed_paths, list) or not changed_paths \
            or changed_paths != sorted(set(changed_paths)) \
            or any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in changed_paths):
        raise AppOnlyError("app-only changed-path evidence is invalid")
    if evidence.get("forbidden_matches") != []:
        raise AppOnlyError("source diff contains protected app-only paths")
    if evidence.get("live_commit") != strict_load(LIVE_RELEASE).get("repos", {}).get("menhir") \
            or evidence.get("candidate_commit") != release.get("repos", {}).get("menhir"):
        raise AppOnlyError("classification evidence is not bound to the live and candidate commits")
    trusted_changed, trusted_forbidden = recompute_source_classification(
        bundle / "source-history.bundle", evidence["live_commit"], evidence["candidate_commit"],
    )
    if changed_paths != trusted_changed or evidence.get("forbidden_matches") != trusted_forbidden:
        raise AppOnlyError("caller classification differs from the trusted source diff")
    if trusted_forbidden:
        raise AppOnlyError("source diff contains protected app-only paths")
    return {
        "path": bundle, "release": release, "env": env,
        "release_sha": release_sha, "env_sha": env_sha, "evidence": evidence,
    }


def validate_release_file(path: Path) -> None:
    require_root_file(SCHEMA, "release schema validator")
    run(["python3", str(SCHEMA), "validate-release", str(path)], 30)


def classify_bundle(
    bundle_id: str, destination: Path | None = None, *, require_authority: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    bundle = load_bundle(bundle_id, destination, require_authority=require_authority)
    validate_release_file(bundle["path"] / "release.json")
    live = strict_load(LIVE_RELEASE)
    result = classify_release(
        live, bundle["release"], sha256(LIVE_RELEASE), parse_env(LIVE_ENV),
        bundle["env"], bundle["env_sha"],
    )
    result["candidate_release_sha256"] = bundle["release_sha"]
    return bundle, result


def inspect_container(name: str) -> dict[str, Any]:
    value = json.loads(run(["docker", "inspect", name], 20))
    if not isinstance(value, list) or len(value) != 1:
        raise AppOnlyError(f"Docker inspection is ambiguous: {name}")
    return value[0]


def inspect_cloudflared() -> dict[str, Any]:
    """Return the sole running Cloudflared peer on the production network."""

    network = json.loads(run(["docker", "network", "inspect", "menhir-proxy"], 20))
    if not isinstance(network, list) or len(network) != 1:
        raise AppOnlyError("production network inspection is ambiguous")
    peers: list[dict[str, Any]] = []
    for row in (network[0].get("Containers") or {}).values():
        container_id = row.get("Name")
        if not isinstance(container_id, str) or not container_id:
            continue
        inspected = inspect_container(container_id)
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.service") == "cloudflared" \
                and inspected.get("State", {}).get("Running") is True:
            peers.append(inspected)
    if len(peers) != 1:
        raise AppOnlyError("expected exactly one running Cloudflared ingress peer")
    return peers[0]


def wait_app(image_digest: str, release_id: str, database_id: str, deadline_seconds: int) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            app = inspect_container("menhir-prod-app")
            database = inspect_container("menhir-prod-neo4j")
            labels = app.get("Config", {}).get("Labels", {}) or {}
            environment = app.get("Config", {}).get("Env", []) or []
            if all((
                database.get("Id") == database_id,
                database.get("State", {}).get("Health", {}).get("Status") == "healthy",
                app.get("State", {}).get("Health", {}).get("Status") == "healthy",
                str(app.get("Config", {}).get("Image", "")).endswith("@" + image_digest),
                labels.get("com.docker.compose.project") == "menhir-prod",
                labels.get("com.docker.compose.service") == "menhir",
                f"MENHIR_RELEASE_ID={release_id}" in environment,
            )):
                return
        except AppOnlyError:
            pass
        time.sleep(2)
    raise AppOnlyError("replacement app did not become exact and healthy within 120 seconds")


def replace_app(env_path: Path, image_digest: str, release_id: str, database_id: str) -> None:
    run([
        "docker", "compose", "--project-name", "menhir-prod", "--env-file", str(env_path),
        "--file", str(COMPOSE), "up", "-d", "--no-deps", "--force-recreate", "menhir",
    ], 120)
    wait_app(image_digest, release_id, database_id, 120)


def request_json(url: str, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Menhir-AppOnly/1", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except Exception as exc:
        raise AppOnlyError(f"HTTP acceptance failed: {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise AppOnlyError(f"HTTP acceptance returned non-object JSON: {url}")
    return value


def mcp_post(base: str, token: str, payload: dict[str, Any], session: str = "") -> tuple[dict[str, Any], str]:
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Menhir-AppOnly/1",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        base.rstrip("/") + "/mcp-http",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            next_session = response.headers.get("Mcp-Session-Id", session)
    except urllib.error.HTTPError as exc:
        raise AppOnlyError(f"MCP acceptance returned HTTP {exc.code}") from exc
    if "text/event-stream" in content_type:
        events = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not events:
            raise AppOnlyError("MCP acceptance SSE response had no data")
        body = events[-1]
    try:
        value = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise AppOnlyError("MCP acceptance returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("error"):
        raise AppOnlyError(f"MCP acceptance returned an error: {value}")
    return value, next_session


def require_probe_policy() -> None:
    """Prove the short-lived acceptance identity has only the reviewed read surface."""

    require_root_file(LIVE_POLICY, "production client policy")
    policy = strict_load(LIVE_POLICY)
    clients = policy.get("clients")
    probe = clients.get(PROBE_CLIENT_ID) if isinstance(clients, dict) else None
    expected = {
        "label": PROBE_CLIENT_ID,
        "scopes": ["menhir:read"],
        "maximum_tier": "readonly",
        "namespace": "",
        "allowed_tools": ["recall_memories"],
    }
    if not isinstance(probe, dict) or any(probe.get(key) != value for key, value in expected.items()):
        raise AppOnlyError("production policy lacks the exact read-only menhir-deploy-probe identity")
    denied = probe.get("denied_tools")
    if not isinstance(denied, list) or not denied or "recall_memories" in denied:
        raise AppOnlyError("menhir-deploy-probe deny boundary is invalid")


def mint_probe_token() -> str:
    """Mint one 60-second policy-bound JWT in memory inside the running app."""

    require_probe_policy()
    script = r'''import json
import os
import secrets
import time
from menhir.api import jose_provider

client_id = "menhir-deploy-probe"
now = int(time.time())
with open(os.environ["MENHIR_OAUTH_SIGNING_KEY_PATH"], encoding="utf-8") as handle:
    key = jose_provider.load_key(json.load(handle))
public = jose_provider.serialize_key(key, private=False)
claims = {
    "iss": os.environ["MENHIR_OAUTH_ISSUER"],
    "sub": "service:menhir-deploy-probe",
    "aud": os.environ["MENHIR_OAUTH_RESOURCE"],
    "client_id": client_id,
    "client_name": client_id,
    "scope": "menhir:read",
    "tier": "readonly",
    "iat": now,
    "exp": now + 60,
    "jti": secrets.token_urlsafe(18),
}
print(jose_provider.sign_jwt(
    {"alg": "RS256", "kid": public["kid"], "typ": "JWT"}, claims, key,
))
'''
    token = run(
        ["docker", "exec", "-i", "menhir-prod-app", "python", "-"],
        15,
        input_bytes=script.encode("ascii"),
    )
    if token.count(".") != 2 or any(char.isspace() for char in token):
        raise AppOnlyError("deploy-probe token mint returned malformed output")
    return token


def accept_production(base: str, release_id: str, image_digest: str, database_id: str) -> None:
    ready = request_json(base.rstrip("/") + "/readyz")
    if ready.get("status") != "ready" or ready.get("mode") != "production" \
            or ready.get("mutation_fence") is not False:
        raise AppOnlyError("public readiness is not writable production mode")
    request_json(base.rstrip("/") + "/.well-known/jwks.json")
    request_json(base.rstrip("/") + "/livez")
    app = inspect_container("menhir-prod-app")
    if app.get("State", {}).get("Health", {}).get("Status") != "healthy" \
            or not str(app.get("Config", {}).get("Image", "")).endswith("@" + image_digest):
        raise AppOnlyError("accepted runtime is not the candidate image")
    if inspect_container("menhir-prod-neo4j").get("Id") != database_id:
        raise AppOnlyError("Neo4j changed during app-only replacement")
    token = mint_probe_token()
    response, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "menhir-app-only-accept", "version": "1"}},
    })
    if "result" not in response:
        raise AppOnlyError("MCP initialize lacked a result")
    _, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, session)
    response, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, session)
    tools = {row.get("name") for row in response.get("result", {}).get("tools", [])}
    if "recall_memories" not in tools:
        raise AppOnlyError("acceptance identity cannot see recall_memories")
    response, _ = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recall_memories", "arguments": {
            "query": "Menhir app-only production acceptance", "limit": 1}},
    }, session)
    if response.get("result", {}).get("isError") is True:
        raise AppOnlyError("read-only recall acceptance returned a tool error")


def write_stage(transaction: dict[str, Any], stage: str, **extra: Any) -> None:
    transaction.update(extra)
    transaction["stage"] = stage
    transaction["updated_utc"] = now_iso()
    atomic_json(ACTIVE, transaction)


def docker_pull(image_ref: str, credential: Path, root_config: Path) -> None:
    require_upload(credential, "Docker credential")
    if credential.stat().st_size > 65536:
        raise AppOnlyError("Docker credential file is unexpectedly large")
    config = strict_load(credential)
    if set(config) != {"auths"} or not isinstance(config.get("auths"), dict) \
            or "ghcr.io" not in config["auths"]:
        raise AppOnlyError("Docker credential must contain only a ghcr.io auth map")
    root_config.mkdir(parents=True, mode=0o700)
    os.chown(root_config, 0, 0)
    atomic_bytes(root_config / "config.json", credential.read_bytes(), 0o600)
    try:
        run(["docker", "--config", str(root_config), "pull", image_ref], 60)
    finally:
        try:
            (root_config / "config.json").unlink()
            root_config.rmdir()
        except OSError:
            pass


def finalize_transaction(transaction: dict[str, Any]) -> None:
    atomic_json(LAST, transaction)
    try:
        ACTIVE.unlink()
    except FileNotFoundError:
        pass


def restore_authority(transaction: dict[str, Any], candidate: bool) -> None:
    tx = Path(transaction["transaction_root"])
    release_name = "candidate-release.json" if candidate else "prior-release.json"
    env_name = "candidate-production.env" if candidate else "prior-production.env"
    require_root_file(tx / release_name, release_name)
    require_root_file(tx / env_name, env_name)
    atomic_bytes(LIVE_ENV, (tx / env_name).read_bytes(), 0o600)
    atomic_bytes(LIVE_RELEASE, (tx / release_name).read_bytes(), 0o400)


def rollback(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    restore_authority(transaction, False)
    if transaction.get("stage") in {"replacing", "running_candidate"}:
        replace_app(
            tx / "prior-production.env", transaction["prior_image"],
            transaction["prior_release_id"], transaction["database_container_id"],
        )
    write_stage(transaction, "rolled_back", completed_utc=now_iso())
    finalize_transaction(transaction)


def rollforward(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    replace_app(
        tx / "candidate-production.env", transaction["candidate_image"],
        transaction["candidate_release_id"], transaction["database_container_id"],
    )
    restore_authority(transaction, True)
    accepted_app = inspect_container("menhir-prod-app")
    accepted_database = inspect_container("menhir-prod-neo4j")
    accepted_ingress = inspect_cloudflared()
    if accepted_database.get("Id") != transaction["database_container_id"]:
        raise AppOnlyError("Neo4j changed during app-only recovery")
    if accepted_ingress.get("Id") != transaction["ingress_container_id"]:
        raise AppOnlyError("Cloudflared changed during app-only recovery")
    write_stage(
        transaction, "complete", completed_utc=now_iso(), recovered=True,
        result="passed", candidate_app_container_id=accepted_app.get("Id"),
        database_container_id_after=accepted_database.get("Id"),
        ingress_container_id_after=accepted_ingress.get("Id"),
    )
    run([str(SCAFFOLD), "verify", "--app-only"], 30)
    finalize_transaction(transaction)


class DeploymentLocks:
    """Hold cross-lane admission before the legacy mutation lock."""

    def __init__(self, handles: list[Any]) -> None:
        self._handles = handles

    def close(self) -> None:
        for handle in reversed(self._handles):
            handle.close()


def acquire_lock() -> DeploymentLocks:
    if fcntl is None:
        raise AppOnlyError("POSIX file locking is unavailable")
    handles: list[Any] = []
    try:
        for path in (ADMISSION_LOCK, MUTATION_LOCK):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise
            handles.append(handle)
    except OSError as exc:
        for handle in reversed(handles):
            handle.close()
        raise AppOnlyError("deployment admission is held") from exc
    return DeploymentLocks(handles)


def require_no_incomplete_transactions() -> None:
    """Reject a new mutation while any deployment lane has unfinished state."""
    for path, kind in (
        (ACTIVE, "app-only"),
        (SECURITY_CONFIG_ACTIVE, "security-config"),
        (MAINTENANCE_ACTIVE, "maintenance"),
    ):
        if not path.exists():
            continue
        require_root_file(path, f"active {kind} transaction")
        stage = strict_load(path).get("stage")
        if kind != "maintenance" or stage != "complete":
            raise AppOnlyError(
                f"an incomplete {kind} transaction exists; recover it before deploying"
            )


def require_runner_sha256(expected: object, path: Path = Path(__file__)) -> str:
    if not isinstance(expected, str) or HEX64.fullmatch(expected) is None:
        raise AppOnlyError("expected root runner SHA-256 is malformed")
    actual = sha256(path)
    if actual != expected:
        raise AppOnlyError("root runner differs from the owner-approved authority")
    return actual


def discard_unstarted_transaction(
    path: Path, lane: str, *, status_root: Path | None = None,
) -> None:
    expected_parent = ((status_root or STATUS) / lane).resolve()
    resolved = path.resolve()
    if resolved.parent != expected_parent or not resolved.name:
        raise AppOnlyError("refusing unsafe unstarted transaction cleanup")
    if resolved.exists():
        shutil.rmtree(resolved)


def _transaction_root(value: dict[str, Any], lane: str) -> Path:
    root_value = value.get("transaction_root")
    if not isinstance(root_value, str):
        raise AppOnlyError("transaction receipt has no transaction root")
    root = Path(root_value).resolve()
    if root.parent != (STATUS / lane).resolve() or not root.name:
        raise AppOnlyError("transaction receipt has an unsafe transaction root")
    return root


def validate_transaction_authority(
    transaction: dict[str, Any],
    *,
    lane: str,
    expected_kind: str,
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_bundle_sha256: str,
    expected_staging_receipt_sha256: str,
    expected_approval_sha256: str,
    expected_ingress_container_id: str,
    promotion_attempt_id: str,
    approved_utc: str,
    promotion_started_utc: str,
) -> dict[str, Any]:
    if transaction.get("schema") != 1 or transaction.get("kind") != expected_kind \
            or transaction.get("stage") != "complete" or transaction.get("result") != "passed":
        raise AppOnlyError("completed transaction receipt schema is invalid")
    root = _transaction_root(transaction, lane)
    for name in ("candidate-release.json", "candidate-production.env", STAGING_RECEIPT_NAME, APPROVAL_NAME):
        require_root_file(root / name, f"transaction {name}")
    release = strict_load(root / "candidate-release.json")
    authority = validate_promotion_authority(
        root,
        release,
        deployment_class=lane,
        expected_runner_sha256=expected_runner_sha256,
        expected_release_sha256=expected_release_sha256,
        expected_bundle_sha256=expected_bundle_sha256,
        expected_staging_receipt_sha256=expected_staging_receipt_sha256,
        expected_approval_sha256=expected_approval_sha256,
        expected_ingress_container_id=expected_ingress_container_id,
        promotion_attempt_id=promotion_attempt_id,
        approved_utc=approved_utc,
        promotion_started_utc=promotion_started_utc,
    )
    expected = {
        "runner_sha256": expected_runner_sha256,
        "candidate_release_id": release.get("release_id"),
        "candidate_release_sha256": expected_release_sha256,
        "ingress_container_id_after": expected_ingress_container_id,
        **authority,
    }
    for key, value in expected.items():
        if transaction.get(key) != value:
            raise AppOnlyError(f"completed transaction has another {key}")
    started = parse_utc(transaction.get("started_utc"), "transaction start")
    completed = parse_utc(transaction.get("completed_utc"), "transaction completion")
    if started < parse_utc(approved_utc, "approval time") \
            or started < parse_utc(promotion_started_utc, "promotion start") \
            or completed < started:
        raise AppOnlyError("completed transaction chronology is invalid")
    return transaction


def validate_active_transaction_authority(
    transaction: dict[str, Any], *, lane: str, expected_kind: str,
    expected_runner_sha256: str,
) -> None:
    if transaction.get("schema") != 1 or transaction.get("kind") != expected_kind:
        raise AppOnlyError("active transaction receipt schema is invalid")
    root = _transaction_root(transaction, lane)
    for name in ("candidate-release.json", STAGING_RECEIPT_NAME, APPROVAL_NAME):
        require_root_file(root / name, f"transaction {name}")
    authority = validate_promotion_authority(
        root,
        strict_load(root / "candidate-release.json"),
        deployment_class=lane,
        expected_runner_sha256=expected_runner_sha256,
        expected_release_sha256=str(transaction.get("candidate_release_sha256", "")),
        expected_bundle_sha256=str(transaction.get("bundle_sha256", "")),
        expected_staging_receipt_sha256=str(transaction.get("staging_receipt_sha256", "")),
        expected_approval_sha256=str(transaction.get("approval_sha256", "")),
        expected_ingress_container_id=str(transaction.get("ingress_container_id", "")),
        promotion_attempt_id=str(transaction.get("promotion_attempt_id", "")),
        approved_utc=str(transaction.get("approved_utc", "")),
        promotion_started_utc=str(transaction.get("promotion_started_utc", "")),
    )
    for key, value in authority.items():
        if transaction.get(key) != value:
            raise AppOnlyError(f"active transaction has another {key}")
    started = parse_utc(transaction.get("started_utc"), "transaction start")
    if started < parse_utc(transaction.get("approved_utc"), "approval time") \
            or started < parse_utc(transaction.get("promotion_started_utc"), "promotion start"):
        raise AppOnlyError("active transaction predates its promotion authority")


def assert_app_only_live_equality(transaction: dict[str, Any]) -> None:
    """Refuse stale receipt adoption unless every promoted live identity still matches."""
    root = _transaction_root(transaction, "app-only")
    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    if sha256(LIVE_RELEASE) != transaction.get("candidate_release_sha256") \
            or sha256(LIVE_RELEASE) != sha256(root / "candidate-release.json") \
            or sha256(LIVE_ENV) != sha256(root / "candidate-production.env"):
        raise AppOnlyError("completed app-only receipt no longer matches live release/environment")
    release = strict_load(LIVE_RELEASE)
    environment = parse_env(LIVE_ENV)
    if release.get("release_id") != transaction.get("candidate_release_id") \
            or release.get("images", {}).get("menhir") != transaction.get("candidate_image") \
            or environment.get("MENHIR_RELEASE_ID") != transaction.get("candidate_release_id") \
            or not environment.get("MENHIR_IMAGE", "").endswith("@" + str(transaction.get("candidate_image"))):
        raise AppOnlyError("completed app-only receipt no longer matches live authority")
    running_app = inspect_container("menhir-prod-app")
    running_database = inspect_container("menhir-prod-neo4j")
    running_ingress = inspect_cloudflared()
    running_image = str(running_app.get("Config", {}).get("Image", ""))
    if running_app.get("Id") != transaction.get("candidate_app_container_id") \
            or not running_image.endswith("@" + str(transaction.get("candidate_image"))) \
            or running_database.get("Id") != transaction.get("database_container_id_after") \
            or running_ingress.get("Id") != transaction.get("ingress_container_id_after"):
        raise AppOnlyError("completed app-only receipt no longer matches live containers")


def deploy(
    bundle_id: str,
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_ingress_container_id: str,
    expected_bundle_sha256: str = "",
    expected_staging_receipt_sha256: str = "",
    expected_approval_sha256: str = "",
    promotion_attempt_id: str = "",
    approved_utc: str = "",
    promotion_started_utc: str = "",
) -> dict[str, Any]:
    runner_sha256 = require_runner_sha256(expected_runner_sha256)
    if HEX64.fullmatch(expected_release_sha256) is None:
        raise AppOnlyError("expected release SHA-256 is malformed")
    if HEX64.fullmatch(expected_ingress_container_id) is None:
        raise AppOnlyError("expected Cloudflared container ID is malformed")
    lock = acquire_lock()
    transaction: dict[str, Any] | None = None
    try:
        require_no_incomplete_transactions()
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        uploaded = load_bundle(bundle_id)
        authority = validate_promotion_authority(
            uploaded["path"], uploaded["release"], deployment_class="app-only",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        tx_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + bundle_id
        tx = STATUS / "app-only" / tx_id
        bundle, classification = classify_bundle(bundle_id, tx)
        validate_promotion_authority(
            bundle["path"], bundle["release"], deployment_class="app-only",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        if classification["candidate_release_sha256"] != expected_release_sha256:
            discard_unstarted_transaction(tx, "app-only")
            raise AppOnlyError("uploaded release differs from the owner-approved authority")
        require_root_file(LIVE_RELEASE, "live release")
        require_root_file(LIVE_ENV, "live production environment")
        atomic_bytes(tx / "prior-release.json", LIVE_RELEASE.read_bytes(), 0o400)
        atomic_bytes(tx / "prior-production.env", LIVE_ENV.read_bytes(), 0o400)
        candidate_env = tx / "production.env"
        candidate_release = tx / "release.json"
        os.replace(candidate_env, tx / "candidate-production.env")
        os.replace(candidate_release, tx / "candidate-release.json")
        app = inspect_container("menhir-prod-app")
        database = inspect_container("menhir-prod-neo4j")
        ingress = inspect_cloudflared()
        if ingress.get("Id") != expected_ingress_container_id:
            raise AppOnlyError("Cloudflared changed since the approved staging preflight")
        transaction = {
            "schema": 1,
            "kind": "menhir-app-only-transaction",
            "runner_sha256": runner_sha256,
            "transaction_id": tx_id,
            "transaction_root": str(tx),
            "bundle_id": bundle_id,
            "prior_release_id": classification["live_release_id"],
            "candidate_release_id": classification["candidate_release_id"],
            "prior_release_sha256": sha256(tx / "prior-release.json"),
            "candidate_release_sha256": classification["candidate_release_sha256"],
            "prior_image": classification["prior_image"],
            "candidate_image": classification["candidate_image"],
            "prior_app_container_id": app.get("Id"),
            "database_container_id": database.get("Id"),
            "ingress_container_id": ingress.get("Id"),
            **authority,
            "started_utc": now_iso(),
        }
        write_stage(transaction, "classified")
        upload = UPLOAD_ROOT / f"app-{bundle_id}"
        image_ref = bundle["env"]["MENHIR_IMAGE"]
        docker_pull(image_ref, upload / "docker-config.json", tx / "docker-config")
        write_stage(transaction, "pulled")
        write_stage(transaction, "replacing")
        replace_app(
            tx / "candidate-production.env", classification["candidate_image"],
            classification["candidate_release_id"], str(database.get("Id")),
        )
        write_stage(transaction, "running_candidate")
        accept_production(
            bundle["env"]["MENHIR_PUBLIC_BASE_URL"], classification["candidate_release_id"],
            classification["candidate_image"], str(database.get("Id")),
        )
        accepted_app = inspect_container("menhir-prod-app")
        accepted_database = inspect_container("menhir-prod-neo4j")
        accepted_ingress = inspect_cloudflared()
        if accepted_database.get("Id") != transaction["database_container_id"]:
            raise AppOnlyError("Neo4j changed during app-only transaction")
        if accepted_ingress.get("Id") != transaction["ingress_container_id"]:
            raise AppOnlyError("Cloudflared changed during app-only transaction")
        write_stage(
            transaction, "accepted", accepted_utc=now_iso(),
            candidate_app_container_id=accepted_app.get("Id"),
            database_container_id_after=accepted_database.get("Id"),
            ingress_container_id_after=accepted_ingress.get("Id"),
        )
        restore_authority(transaction, True)
        write_stage(transaction, "complete", completed_utc=now_iso(), result="passed")
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        finalize_transaction(transaction)
        return transaction
    except Exception as exc:
        if transaction is not None and transaction.get("stage") not in {"accepted", "complete"}:
            try:
                rollback(transaction)
            except Exception as rollback_exc:
                raise AppOnlyError(f"deployment failed ({exc}); automatic rollback failed ({rollback_exc})") from rollback_exc
        elif transaction is not None and transaction.get("stage") == "accepted":
            try:
                rollforward(transaction)
            except Exception as recovery_exc:
                raise AppOnlyError(f"accepted deployment could not commit or recover: {recovery_exc}") from recovery_exc
        if isinstance(exc, AppOnlyError):
            raise
        raise AppOnlyError(str(exc)) from exc
    finally:
        lock.close()


def adopt(
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_ingress_container_id: str,
    expected_bundle_sha256: str,
    expected_staging_receipt_sha256: str,
    expected_approval_sha256: str,
    promotion_attempt_id: str,
    approved_utc: str,
    promotion_started_utc: str,
) -> dict[str, Any]:
    runner_sha256 = require_runner_sha256(expected_runner_sha256)
    lock = acquire_lock()
    try:
        require_no_incomplete_transactions()
        transaction = last_receipt()
        validate_transaction_authority(
            transaction,
            lane="app-only",
            expected_kind="menhir-app-only-transaction",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        assert_app_only_live_equality(transaction)
        return transaction
    finally:
        lock.close()


def recover() -> dict[str, Any]:
    if not ACTIVE.exists():
        raise AppOnlyError("there is no incomplete app-only transaction")
    lock = acquire_lock()
    try:
        require_root_file(ACTIVE, "active app-only transaction")
        transaction = strict_load(ACTIVE)
        if transaction.get("kind") != "menhir-app-only-transaction":
            raise AppOnlyError("active app-only transaction schema mismatch")
        runner_sha256 = require_runner_sha256(transaction.get("runner_sha256"))
        validate_active_transaction_authority(
            transaction,
            lane="app-only",
            expected_kind="menhir-app-only-transaction",
            expected_runner_sha256=runner_sha256,
        )
        if transaction.get("stage") == "complete":
            finalize_transaction(transaction)
        elif transaction.get("stage") == "accepted":
            rollforward(transaction)
        else:
            rollback(transaction)
        return transaction
    finally:
        lock.close()


def live_info() -> dict[str, Any]:
    require_root_file(LIVE_RELEASE, "live release")
    value = strict_load(LIVE_RELEASE)
    return {
        "release_id": value.get("release_id"),
        "menhir_commit": value.get("repos", {}).get("menhir"),
        "menhir_image": value.get("images", {}).get("menhir"),
        "release_sha256": sha256(LIVE_RELEASE),
    }


def check_live() -> dict[str, Any]:
    lock = acquire_lock()
    try:
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        return accept_current()
    finally:
        lock.close()


def last_receipt() -> dict[str, Any]:
    require_root_file(LAST, "last app-only transaction receipt")
    value = strict_load(LAST)
    if value.get("schema") != 1 or value.get("kind") != "menhir-app-only-transaction" \
            or value.get("stage") != "complete" or value.get("result") != "passed":
        raise AppOnlyError("last app-only transaction receipt is not complete")
    if value.get("database_container_id") != value.get("database_container_id_after"):
        raise AppOnlyError("last app-only receipt does not prove unchanged Neo4j")
    if value.get("ingress_container_id") != value.get("ingress_container_id_after"):
        raise AppOnlyError("last app-only receipt does not prove unchanged Cloudflared")
    return value


def accept_current() -> dict[str, Any]:
    """Accept the current release while a maintenance stage journal is active."""

    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    release = strict_load(LIVE_RELEASE)
    environment = parse_env(LIVE_ENV)
    database_id = str(inspect_container("menhir-prod-neo4j").get("Id"))
    accept_production(
        environment["MENHIR_PUBLIC_BASE_URL"], release["release_id"],
        release["images"]["menhir"], database_id,
    )
    return {
        "status": "accepted",
        "release_id": release["release_id"],
        "menhir_image": release["images"]["menhir"],
        "checked_utc": now_iso(),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("bundle_id")
    deploy_command = commands.add_parser("deploy")
    deploy_command.add_argument("bundle_id")
    deploy_command.add_argument("expected_runner_sha256")
    deploy_command.add_argument("expected_release_sha256")
    deploy_command.add_argument("expected_ingress_container_id")
    for name in (
        "expected_bundle_sha256", "expected_staging_receipt_sha256",
        "expected_approval_sha256", "promotion_attempt_id", "approved_utc",
        "promotion_started_utc",
    ):
        deploy_command.add_argument(name)
    adopt_command = commands.add_parser("adopt")
    for name in (
        "expected_runner_sha256", "expected_release_sha256",
        "expected_ingress_container_id", "expected_bundle_sha256",
        "expected_staging_receipt_sha256", "expected_approval_sha256",
        "promotion_attempt_id", "approved_utc", "promotion_started_utc",
    ):
        adopt_command.add_argument(name)
    commands.add_parser("recover")
    commands.add_parser("live")
    commands.add_parser("check")
    commands.add_parser("accept-current")
    commands.add_parser("receipt")
    return result


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("REFUSED: app-only authority must run as root", file=sys.stderr)
        return 1
    args = parser().parse_args(argv)
    try:
        if args.command == "classify":
            _, value = classify_bundle(args.bundle_id, require_authority=False)
        elif args.command == "deploy":
            value = deploy(
                args.bundle_id,
                args.expected_runner_sha256,
                args.expected_release_sha256,
                args.expected_ingress_container_id,
                args.expected_bundle_sha256,
                args.expected_staging_receipt_sha256,
                args.expected_approval_sha256,
                args.promotion_attempt_id,
                args.approved_utc,
                args.promotion_started_utc,
            )
        elif args.command == "adopt":
            value = adopt(
                args.expected_runner_sha256,
                args.expected_release_sha256,
                args.expected_ingress_container_id,
                args.expected_bundle_sha256,
                args.expected_staging_receipt_sha256,
                args.expected_approval_sha256,
                args.promotion_attempt_id,
                args.approved_utc,
                args.promotion_started_utc,
            )
        elif args.command == "recover":
            value = recover()
        elif args.command == "live":
            value = live_info()
        elif args.command == "check":
            value = check_live()
        elif args.command == "receipt":
            value = last_receipt()
        else:
            value = accept_current()
    except AppOnlyError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
