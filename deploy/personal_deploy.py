#!/usr/bin/env python3
"""Coordinate Menhir's personal stage, approval, and promotion workflow.

This coordinator consumes a finalized product-release workspace but never builds
or republishes it.  A separate staging runner must prove the exact immutable
bundle in disposable infrastructure and emit the strict receipt validated here.
Only one explicit, receipt-bound approval can unlock the production runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


STATE_NAME = "personal-deploy.json"
STAGING_RECEIPT_NAME = "staging-receipt.json"
APPROVAL_NAME = "promotion-approval.json"
PROMOTION_RECEIPT_NAME = "promotion-receipt.json"
ROOT_TRANSACTION_RECEIPT_NAME = "root-transaction-receipt.json"
RELEASE_STATE_NAME = "release-flow.json"
RELEASE_NAME = "release.json"
BUNDLE_NAME = "install-bundle"
DEFAULT_PROMOTION_WRAPPER = Path(__file__).resolve().with_name("personal_promote.ps1")
POWERSHELL = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"

KIND = "menhir-personal-deployment"
SCHEMA = 1
PHASES = ("selected", "staged", "approved", "promoted")
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DEPLOYMENT_CLASSES = frozenset({"app-only", "security-config", "maintenance"})
MAX_STAGING_AGE = timedelta(hours=24)

STATE_KEYS = frozenset({
    "schema", "kind", "phase", "workspace", "release_workspace",
    "release_id", "release_sha256", "bundle_sha256", "deployment_class",
    "ingress_mode", "menhir_image", "neo4j_image", "staging_receipt_sha256",
    "approval_sha256", "promotion_receipt_sha256",
})
STAGING_CHECKS = frozenset({
    "artifact_identity",
    "production_memory_limits",
    "production_network_shape",
    "oauth_policy_shape",
    "ingress_request_handling",
    "isolated_disposable_data",
    "non_production_credentials",
    "production_authority_absent",
    "oauth_discovery",
    "oauth_authorization_code_pkce",
    "mcp_initialize",
    "mcp_tools_list",
    "mcp_recall",
    "synthetic_write_allowed",
    "denied_operation_refused",
    "restart_persistence",
    "automatic_rollback",
})
STAGING_KEYS = frozenset({
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "deployment_class", "ingress_mode", "images", "runner_sha256",
    "started_utc", "completed_utc", "test_identities", "checks",
    "production_preflight",
})
PREFLIGHT_KEYS = frozenset({
    "schema", "kind", "result", "observed_utc", "deployment_class",
    "candidate_release_id", "ingress_mode", "checks", "canonical_sha256",
})
PREFLIGHT_CHECK_KEYS = frozenset({
    "live_services", "network_roles", "release_journal", "headroom",
    "maintenance_route",
})
APPROVAL_KEYS = frozenset({
    "schema", "kind", "release_id", "release_sha256", "bundle_sha256",
    "staging_receipt_sha256", "approved_by", "approved_utc",
})
PROMOTION_KEYS = frozenset({
    "schema", "kind", "result", "release_id", "release_sha256",
    "bundle_sha256", "staging_receipt_sha256", "approval_sha256",
    "deployment_class", "ingress_mode", "started_utc", "completed_utc",
    "elapsed_seconds", "promotion_wrapper_sha256", "operator_wrapper_sha256",
    "transaction_kind", "transaction_receipt_sha256", "transaction",
})


class PersonalDeployError(ValueError):
    """A personal-deployment input, artifact, or transition is invalid."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PersonalDeployError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise PersonalDeployError(f"{label} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PersonalDeployError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PersonalDeployError(f"{label} must be a regular non-symlink file")
    return path


def _directory(path: Path, label: str, *, empty: bool = False) -> Path:
    if not path.is_absolute():
        raise PersonalDeployError(f"{label} must be an absolute path")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PersonalDeployError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise PersonalDeployError(f"{label} must be a non-symlink directory")
    if empty and any(path.iterdir()):
        raise PersonalDeployError(f"{label} must be empty")
    return path.resolve()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    path = _regular_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PersonalDeployError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise PersonalDeployError(f"{label} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    root = _directory(root, "install bundle")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PersonalDeployError(f"install bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise PersonalDeployError(f"install bundle contains special entry: {relative}")
        digest.update(f"{relative}\0{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PersonalDeployError(f"{label} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PersonalDeployError(f"{label} is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise PersonalDeployError(f"{label} must be UTC")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise PersonalDeployError(f"{label} schema is invalid")


def _release_binding(release_workspace: Path) -> dict[str, Any]:
    release_workspace = _directory(release_workspace, "product release workspace")
    release_state = _load_json(
        release_workspace / RELEASE_STATE_NAME,
        "product release state",
    )
    if release_state.get("kind") != "menhir-release-flow" \
            or release_state.get("phase") not in {"bundled", "published", "deployed"}:
        raise PersonalDeployError("product release is not finalized")
    release_id = release_state.get("release_id")
    release_sha = release_state.get("release_sha256")
    bundle_sha = release_state.get("bundle_sha256")
    deployment_class = release_state.get("deployment_class")
    ingress_mode = release_state.get("ingress_mode")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
        raise PersonalDeployError("product release ID is invalid")
    if not isinstance(release_sha, str) or SHA256_RE.fullmatch(release_sha) is None \
            or not isinstance(bundle_sha, str) or SHA256_RE.fullmatch(bundle_sha) is None:
        raise PersonalDeployError("product release digests are invalid")
    if deployment_class not in DEPLOYMENT_CLASSES:
        raise PersonalDeployError("product deployment class is invalid")
    if ingress_mode != "cloudflared":
        raise PersonalDeployError("product ingress mode is invalid")
    release_path = _regular_file(release_workspace / RELEASE_NAME, "release authority")
    bundle = _directory(release_workspace / BUNDLE_NAME, "install bundle")
    if _sha256(release_path) != release_sha or _tree_sha256(bundle) != bundle_sha:
        raise PersonalDeployError("finalized product release artifacts changed")
    release = _load_json(release_path, "release authority")
    if release.get("release_id") != release_id:
        raise PersonalDeployError("release authority identity mismatch")
    if release.get("deployment_class") != deployment_class:
        raise PersonalDeployError("product state/release deployment class mismatch")
    if release.get("ingress_mode") != ingress_mode:
        raise PersonalDeployError("product state/release ingress mode mismatch")
    for state_key, release_key, filename in (
        ("notes_json_sha256", "notes_json_sha256", "release-notes.json"),
        ("notes_markdown_sha256", "notes_markdown_sha256", "release-notes.md"),
    ):
        state_digest = release_state.get(state_key)
        release_digest = release.get(release_key)
        if not isinstance(state_digest, str) or SHA256_RE.fullmatch(state_digest) is None \
                or release_digest != state_digest:
            raise PersonalDeployError(f"product {filename} authority mismatch")
        if _sha256(_regular_file(release_workspace / filename, filename)) != state_digest:
            raise PersonalDeployError(f"finalized product {filename} changed")
    images = release.get("images")
    if not isinstance(images, dict):
        raise PersonalDeployError("release image authority is invalid")
    for name in ("menhir", "neo4j"):
        if not isinstance(images.get(name), str) or IMAGE_RE.fullmatch(images[name]) is None:
            raise PersonalDeployError(f"release {name} image digest is invalid")
    return {
        "release_workspace": str(release_workspace),
        "release_id": release_id,
        "release_sha256": release_sha,
        "bundle_sha256": bundle_sha,
        "deployment_class": deployment_class,
        "ingress_mode": ingress_mode,
        "menhir_image": images["menhir"],
        "neo4j_image": images["neo4j"],
    }


def _state_path(workspace: Path) -> Path:
    return workspace / STATE_NAME


def _load_state(workspace: Path) -> dict[str, Any]:
    workspace = _directory(workspace, "personal deployment workspace")
    state = _load_json(_state_path(workspace), "personal deployment state")
    _exact_keys(state, STATE_KEYS, "personal deployment state")
    if state.get("schema") != SCHEMA or state.get("kind") != KIND:
        raise PersonalDeployError("personal deployment state kind/schema is invalid")
    if state.get("phase") not in PHASES or state.get("workspace") != str(workspace):
        raise PersonalDeployError("personal deployment state identity is invalid")
    binding = _release_binding(Path(str(state.get("release_workspace", ""))))
    for key, expected in binding.items():
        if state.get(key) != expected:
            raise PersonalDeployError(f"personal deployment release binding changed: {key}")
    for key in ("staging_receipt_sha256", "approval_sha256", "promotion_receipt_sha256"):
        value = state.get(key)
        if value is not None and (not isinstance(value, str) or SHA256_RE.fullmatch(value) is None):
            raise PersonalDeployError(f"personal deployment digest is invalid: {key}")
    return state


def select_flow(release_workspace: Path, workspace: Path) -> dict[str, Any]:
    workspace = _directory(workspace, "personal deployment workspace", empty=True)
    binding = _release_binding(release_workspace)
    state = {
        "schema": SCHEMA,
        "kind": KIND,
        "phase": "selected",
        "workspace": str(workspace),
        **binding,
        "staging_receipt_sha256": None,
        "approval_sha256": None,
        "promotion_receipt_sha256": None,
    }
    _atomic_json(_state_path(workspace), state)
    return state


def _runner(path: Path, label: str) -> Path:
    path = _regular_file(path, label)
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise PersonalDeployError(f"{label} is not executable")
    return path


def _runner_sha256(path: Path) -> str:
    """Bind the desktop staging entry point and its uploaded VPS payload."""
    if path.name.lower() != "personal_stage.ps1":
        return _sha256(path)
    companion = _regular_file(path.with_name("personal_stage_vps.py"), "VPS staging runner")
    digest = hashlib.sha256()
    for component in (path, companion):
        digest.update(f"{component.name}\0{_sha256(component)}\n".encode("utf-8"))
    return digest.hexdigest()


def _stage_command(workspace: Path, state: dict[str, Any], runner: Path, receipt: Path) -> list[str]:
    if runner.suffix.lower() == ".ps1":
        return [
            POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
            "-Bundle", str(Path(state["release_workspace"]) / BUNDLE_NAME),
            "-ExpectedBundleSha256", state["bundle_sha256"],
            "-ExpectedReleaseId", state["release_id"],
            "-ExpectedReleaseSha256", state["release_sha256"],
            "-DeploymentClass", state["deployment_class"],
            "-Receipt", str(receipt),
            "-Workspace", str(workspace),
        ]
    if runner.name.lower() == "personal_stage_vps.py":
        raise PersonalDeployError(
            "the VPS staging runner cannot run directly; use personal_stage.ps1 "
            "to bind and transfer the image archive"
        )
    prefix: list[str]
    if runner.suffix.lower() == ".py":
        prefix = [sys.executable, str(runner)]
    else:
        prefix = [str(runner)]
    return [
        *prefix,
        "--bundle", str(Path(state["release_workspace"]) / BUNDLE_NAME),
        "--expected-bundle-sha256", state["bundle_sha256"],
        "--expected-release-id", state["release_id"],
        "--expected-release-sha256", state["release_sha256"],
        "--deployment-class", state["deployment_class"],
        "--receipt", str(receipt),
        "--workspace", str(workspace),
    ]


def _validate_staging_receipt(path: Path, state: dict[str, Any], runner_sha: str) -> dict[str, Any]:
    receipt = _load_json(path, "staging receipt")
    _exact_keys(receipt, STAGING_KEYS, "staging receipt")
    expected = {
        "schema": 1,
        "kind": "menhir-personal-staging",
        "result": "passed",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "deployment_class": state["deployment_class"],
        "ingress_mode": state["ingress_mode"],
        "runner_sha256": runner_sha,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise PersonalDeployError(f"staging receipt {key} mismatch")
    if receipt.get("images") != {
        "menhir": state["menhir_image"],
        "neo4j": state["neo4j_image"],
    }:
        raise PersonalDeployError("staging receipt image identity mismatch")
    if receipt.get("test_identities") != {
        "oauth_client_id": "menhir-staging-probe",
        "subject": "menhir-admin",
        "namespace": "menhir-staging",
    }:
        raise PersonalDeployError("staging receipt test identities are invalid")
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or set(checks) != STAGING_CHECKS \
            or any(value is not True for value in checks.values()):
        raise PersonalDeployError("staging receipt does not prove every required check")
    preflight = receipt.get("production_preflight")
    if not isinstance(preflight, dict):
        raise PersonalDeployError("staging receipt has no production readiness preflight")
    _exact_keys(preflight, PREFLIGHT_KEYS, "production readiness preflight")
    expected_preflight = {
        "schema": 1,
        "kind": "menhir-production-readiness-preflight",
        "result": "passed",
        "deployment_class": state["deployment_class"],
        "candidate_release_id": state["release_id"],
        "ingress_mode": state["ingress_mode"],
    }
    for key, value in expected_preflight.items():
        if preflight.get(key) != value:
            raise PersonalDeployError(f"production readiness preflight {key} mismatch")
    digest = preflight.get("canonical_sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise PersonalDeployError("production readiness preflight digest is invalid")
    unsealed = dict(preflight)
    unsealed.pop("canonical_sha256")
    canonical = hashlib.sha256(json.dumps(
        unsealed, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if digest != canonical:
        raise PersonalDeployError("production readiness preflight digest mismatch")
    preflight_checks = preflight.get("checks")
    if not isinstance(preflight_checks, dict) or set(preflight_checks) != PREFLIGHT_CHECK_KEYS:
        raise PersonalDeployError("production readiness preflight check set is invalid")
    headroom = preflight_checks.get("headroom")
    if not isinstance(headroom, dict) or set(headroom) != {
        "disk_free_bytes", "disk_required_bytes",
        "memory_available_bytes", "memory_required_bytes",
    } or any(not isinstance(value, int) or isinstance(value, bool) for value in headroom.values()) \
            or headroom["disk_free_bytes"] < headroom["disk_required_bytes"] \
            or headroom["memory_available_bytes"] < headroom["memory_required_bytes"]:
        raise PersonalDeployError("production readiness preflight headroom is invalid")
    route = preflight_checks.get("maintenance_route")
    route_required = state["deployment_class"] == "maintenance"
    if not isinstance(route, dict) or route.get("applicable") is not route_required:
        raise PersonalDeployError("production readiness preflight route scope is invalid")
    started = _utc(receipt.get("started_utc"), "staging started_utc")
    observed = _utc(preflight.get("observed_utc"), "production preflight observed_utc")
    completed = _utc(receipt.get("completed_utc"), "staging completed_utc")
    current = datetime.now(timezone.utc)
    if observed > started or completed < started or completed > current + timedelta(minutes=1) \
            or current - completed > MAX_STAGING_AGE:
        raise PersonalDeployError("staging receipt is stale or has invalid timing")
    return receipt


def stage_flow(
    workspace: Path,
    runner_path: Path,
    *,
    execute: bool,
    command_runner: Callable[[list[str]], None] | None = None,
) -> dict[str, Any] | list[str]:
    workspace = _directory(workspace, "personal deployment workspace")
    state = _load_state(workspace)
    runner = _runner(runner_path, "staging runner")
    runner_sha = _runner_sha256(runner)
    receipt = workspace / STAGING_RECEIPT_NAME
    command = _stage_command(workspace, state, runner, receipt)
    if state["phase"] in {"staged", "approved", "promoted"}:
        if not receipt.exists() or _sha256(receipt) != state["staging_receipt_sha256"]:
            raise PersonalDeployError("recorded staging receipt changed")
        _validate_staging_receipt(receipt, state, runner_sha)
        return state
    if state["phase"] != "selected":
        raise PersonalDeployError("only a selected release can be staged")
    if not execute:
        return command
    if receipt.exists() or receipt.is_symlink():
        raise PersonalDeployError("staging receipt path must not already exist")
    (command_runner or (lambda value: subprocess.run(value, check=True)))(command)
    _validate_staging_receipt(receipt, state, runner_sha)
    state["phase"] = "staged"
    state["staging_receipt_sha256"] = _sha256(receipt)
    _atomic_json(_state_path(workspace), state)
    return state


def rehearse_flow(
    release_workspace: Path,
    workspace: Path,
    runner_path: Path,
    *,
    execute: bool,
    command_runner: Callable[[list[str]], None] | None = None,
) -> dict[str, Any] | list[str]:
    """Select and stage one release, resuming an existing matching selection."""
    workspace = _directory(workspace, "personal deployment workspace")
    if not any(workspace.iterdir()):
        select_flow(release_workspace, workspace)
    else:
        state = _load_state(workspace)
        binding = _release_binding(release_workspace)
        for key, expected in binding.items():
            if state.get(key) != expected:
                raise PersonalDeployError(f"rehearsal release binding mismatch: {key}")
    return stage_flow(
        workspace,
        runner_path,
        execute=execute,
        command_runner=command_runner,
    )


def _verify_staging(workspace: Path, state: dict[str, Any]) -> str:
    receipt = _regular_file(workspace / STAGING_RECEIPT_NAME, "staging receipt")
    digest = _sha256(receipt)
    if digest != state.get("staging_receipt_sha256"):
        raise PersonalDeployError("staging receipt changed after validation")
    # The runner digest was validated when the receipt was accepted. Recheck all
    # release/timing/check fields without requiring that executable to remain.
    value = _load_json(receipt, "staging receipt")
    runner_sha = value.get("runner_sha256")
    if not isinstance(runner_sha, str) or SHA256_RE.fullmatch(runner_sha) is None:
        raise PersonalDeployError("staging runner digest is invalid")
    _validate_staging_receipt(receipt, state, runner_sha)
    return digest


def approve_flow(
    workspace: Path,
    confirmation: str | None = None,
    staging_confirmation: str | None = None,
    approved_by: str | None = None,
) -> dict[str, Any]:
    workspace = _directory(workspace, "personal deployment workspace")
    state = _load_state(workspace)
    if confirmation is not None and confirmation != state["release_id"]:
        raise PersonalDeployError("approval release confirmation must exactly match the release ID")
    staging_sha = _verify_staging(workspace, state)
    if staging_confirmation is not None and staging_confirmation != staging_sha:
        raise PersonalDeployError("approval staging confirmation must exactly match the receipt digest")
    if not isinstance(approved_by, str) or not re.fullmatch(r"[A-Za-z0-9._@+-]{1,128}", approved_by):
        raise PersonalDeployError("approval identity is invalid")
    approval_path = workspace / APPROVAL_NAME
    if state["phase"] in {"approved", "promoted"}:
        if _sha256(_regular_file(approval_path, "promotion approval")) != state["approval_sha256"]:
            raise PersonalDeployError("recorded promotion approval changed")
        return state
    if state["phase"] != "staged":
        raise PersonalDeployError("only a successfully staged release can be approved")
    if approval_path.exists() or approval_path.is_symlink():
        raise PersonalDeployError("promotion approval path must not already exist")
    approval = {
        "schema": 1,
        "kind": "menhir-personal-promotion-approval",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "staging_receipt_sha256": staging_sha,
        "approved_by": approved_by,
        "approved_utc": _now(),
    }
    _atomic_json(approval_path, approval)
    state["phase"] = "approved"
    state["approval_sha256"] = _sha256(approval_path)
    _atomic_json(_state_path(workspace), state)
    return state


def _verify_approval(workspace: Path, state: dict[str, Any]) -> str:
    approval_path = _regular_file(workspace / APPROVAL_NAME, "promotion approval")
    digest = _sha256(approval_path)
    if digest != state.get("approval_sha256"):
        raise PersonalDeployError("promotion approval changed")
    approval = _load_json(approval_path, "promotion approval")
    _exact_keys(approval, APPROVAL_KEYS, "promotion approval")
    expected = {
        "schema": 1,
        "kind": "menhir-personal-promotion-approval",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "staging_receipt_sha256": state["staging_receipt_sha256"],
    }
    for key, value in expected.items():
        if approval.get(key) != value:
            raise PersonalDeployError(f"promotion approval {key} mismatch")
    if not isinstance(approval.get("approved_by"), str) \
            or re.fullmatch(r"[A-Za-z0-9._@+-]{1,128}", approval["approved_by"]) is None:
        raise PersonalDeployError("promotion approval identity is invalid")
    _utc(approval.get("approved_utc"), "approved_utc")
    return digest


def _validate_promotion_receipt(
    path: Path,
    state: dict[str, Any],
    approval_sha: str,
) -> dict[str, Any]:
    receipt = _load_json(path, "promotion receipt")
    _exact_keys(receipt, PROMOTION_KEYS, "promotion receipt")
    expected = {
        "schema": 1,
        "kind": "menhir-personal-promotion",
        "result": "passed",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "staging_receipt_sha256": state["staging_receipt_sha256"],
        "approval_sha256": approval_sha,
        "deployment_class": state["deployment_class"],
        "ingress_mode": state["ingress_mode"],
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise PersonalDeployError(f"promotion receipt {key} mismatch")
    started = _utc(receipt.get("started_utc"), "promotion started_utc")
    completed = _utc(receipt.get("completed_utc"), "promotion completed_utc")
    if completed < started:
        raise PersonalDeployError("promotion receipt completion precedes start")
    elapsed = receipt.get("elapsed_seconds")
    if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0:
        raise PersonalDeployError("promotion receipt elapsed_seconds is invalid")
    expected_budget = 300 if state["deployment_class"] == "app-only" else 600
    if elapsed > expected_budget:
        raise PersonalDeployError("promotion exceeded its foreground time budget")
    for key in ("promotion_wrapper_sha256", "operator_wrapper_sha256"):
        if not isinstance(receipt.get(key), str) or SHA256_RE.fullmatch(receipt[key]) is None:
            raise PersonalDeployError(f"promotion receipt {key} is invalid")
    if receipt.get("transaction_kind") != state["deployment_class"]:
        raise PersonalDeployError("promotion receipt transaction kind mismatch")
    transaction = receipt.get("transaction")
    if not isinstance(transaction, dict):
        raise PersonalDeployError("promotion receipt lacks the root transaction")
    transaction_path = path.with_name(ROOT_TRANSACTION_RECEIPT_NAME)
    if _sha256(_regular_file(transaction_path, "root transaction receipt")) \
            != receipt.get("transaction_receipt_sha256"):
        raise PersonalDeployError("promotion receipt root transaction digest mismatch")
    if _load_json(transaction_path, "root transaction receipt") != transaction:
        raise PersonalDeployError("embedded root transaction differs from its receipt")
    expected_kind = {
        "app-only": "menhir-app-only-transaction",
        "security-config": "menhir-security-config-transaction",
        "maintenance": "menhir-maintenance-transaction",
    }[state["deployment_class"]]
    if transaction.get("schema") != 1 or transaction.get("kind") != expected_kind \
            or transaction.get("result") != "passed" or transaction.get("stage") != "complete" \
            or transaction.get("candidate_release_id") != state["release_id"] \
            or transaction.get("candidate_release_sha256") != state["release_sha256"]:
        raise PersonalDeployError("root transaction is not bound to the promoted release")
    if not isinstance(transaction.get("runner_sha256"), str) \
            or SHA256_RE.fullmatch(transaction["runner_sha256"]) is None:
        raise PersonalDeployError("root transaction runner digest is invalid")
    transaction_started = _utc(transaction.get("started_utc"), "transaction started_utc")
    transaction_completed = _utc(transaction.get("completed_utc"), "transaction completed_utc")
    if transaction_started < started or transaction_completed > completed \
            or transaction_completed < transaction_started:
        raise PersonalDeployError("root transaction timestamps escape the promotion window")
    if state["deployment_class"] in {"app-only", "security-config"} \
            and transaction.get("database_container_id") \
            != transaction.get("database_container_id_after"):
        raise PersonalDeployError("root transaction does not prove unchanged Neo4j")
    if transaction.get("ingress_container_id") \
            != transaction.get("ingress_container_id_after"):
        raise PersonalDeployError("root transaction does not prove unchanged Cloudflared")
    return receipt


def _promotion_command(workspace: Path, state: dict[str, Any], wrapper: Path) -> list[str]:
    mode = {
        "app-only": "AppOnly",
        "security-config": "SecurityConfig",
        "maintenance": "Maintenance",
    }[state["deployment_class"]]
    return [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(wrapper),
        "-Mode", mode,
        "-BundlePath", str(Path(state["release_workspace"]) / BUNDLE_NAME),
        "-ExpectedBundleSha256", state["bundle_sha256"],
        "-Release", state["release_id"],
        "-ExpectedReleaseSha256", state["release_sha256"],
        "-StagingReceipt", str(workspace / STAGING_RECEIPT_NAME),
        "-ExpectedStagingReceiptSha256", state["staging_receipt_sha256"],
        "-Approval", str(workspace / APPROVAL_NAME),
        "-ExpectedApprovalSha256", state["approval_sha256"],
        "-SourceRepository", str(Path(__file__).resolve().parent.parent),
        "-TransactionReceipt", str(workspace / ROOT_TRANSACTION_RECEIPT_NAME),
        "-ResultReceipt", str(workspace / PROMOTION_RECEIPT_NAME),
    ]


def promote_flow(
    workspace: Path,
    confirmation: str | None = None,
    staging_confirmation: str | None = None,
    *,
    execute: bool,
    wrapper_path: Path = DEFAULT_PROMOTION_WRAPPER,
    command_runner: Callable[[list[str]], None] | None = None,
) -> dict[str, Any] | list[str]:
    workspace = _directory(workspace, "personal deployment workspace")
    state = _load_state(workspace)
    if confirmation is not None and confirmation != state["release_id"]:
        raise PersonalDeployError("promotion release confirmation must exactly match the release ID")
    staging_sha = _verify_staging(workspace, state)
    if staging_confirmation is not None and staging_confirmation != staging_sha:
        raise PersonalDeployError("promotion staging confirmation must exactly match the receipt digest")
    approval_sha = _verify_approval(workspace, state)
    wrapper = _regular_file(wrapper_path, "production promotion wrapper")
    command = _promotion_command(workspace, state, wrapper)
    receipt_path = workspace / PROMOTION_RECEIPT_NAME
    if state["phase"] == "promoted":
        if _sha256(_regular_file(receipt_path, "promotion receipt")) \
                != state["promotion_receipt_sha256"]:
            raise PersonalDeployError("recorded promotion receipt changed")
        _validate_promotion_receipt(receipt_path, state, approval_sha)
        return state
    if state["phase"] != "approved":
        raise PersonalDeployError("only an approved release can be promoted")
    if not execute:
        return command
    transaction_path = workspace / ROOT_TRANSACTION_RECEIPT_NAME
    if receipt_path.exists() or receipt_path.is_symlink() \
            or transaction_path.exists() or transaction_path.is_symlink():
        raise PersonalDeployError("promotion receipt path must not already exist")
    (command_runner or (lambda value: subprocess.run(value, check=True)))(command)
    if not receipt_path.exists():
        raise PersonalDeployError("production wrapper returned without a promotion receipt")
    _validate_promotion_receipt(receipt_path, state, approval_sha)
    state["phase"] = "promoted"
    state["promotion_receipt_sha256"] = _sha256(receipt_path)
    _atomic_json(_state_path(workspace), state)
    return state


def status_flow(workspace: Path) -> dict[str, Any]:
    workspace = _directory(workspace, "personal deployment workspace")
    state = _load_state(workspace)
    if state["phase"] in {"staged", "approved", "promoted"}:
        _verify_staging(workspace, state)
    if state["phase"] in {"approved", "promoted"}:
        _verify_approval(workspace, state)
    if state["phase"] == "promoted":
        if _sha256(workspace / PROMOTION_RECEIPT_NAME) != state["promotion_receipt_sha256"]:
            raise PersonalDeployError("promotion receipt changed")
        _validate_promotion_receipt(
            workspace / PROMOTION_RECEIPT_NAME,
            state,
            str(state["approval_sha256"]),
        )
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select")
    select.add_argument("--release-workspace", type=Path, required=True)
    select.add_argument("--workspace", type=Path, required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--workspace", type=Path, required=True)
    stage.add_argument("--runner", type=Path, required=True)
    stage.add_argument("--execute", action="store_true")
    rehearse = commands.add_parser("rehearse")
    rehearse.add_argument("--release-workspace", type=Path, required=True)
    rehearse.add_argument("--workspace", type=Path, required=True)
    rehearse.add_argument("--runner", type=Path, required=True)
    rehearse.add_argument("--execute", action="store_true")
    approve = commands.add_parser("approve")
    approve.add_argument("--workspace", type=Path, required=True)
    approve.add_argument("--confirm-release-id")
    approve.add_argument("--confirm-staging-sha256")
    approve.add_argument("--approved-by")
    promote = commands.add_parser("promote")
    promote.add_argument("--workspace", type=Path, required=True)
    promote.add_argument("--confirm-release-id")
    promote.add_argument("--confirm-staging-sha256")
    promote.add_argument("--wrapper", type=Path, default=DEFAULT_PROMOTION_WRAPPER)
    promote.add_argument("--execute", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--workspace", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "select":
            result: Any = select_flow(args.release_workspace, args.workspace)
        elif args.command == "stage":
            result = stage_flow(args.workspace, args.runner, execute=args.execute)
        elif args.command == "rehearse":
            result = rehearse_flow(
                args.release_workspace,
                args.workspace,
                args.runner,
                execute=args.execute,
            )
        elif args.command == "approve":
            approved_by = args.approved_by
            if approved_by is None and args.confirm_release_id is not None \
                    and args.confirm_staging_sha256 is not None:
                # Preserve the legacy CLI's environment-derived identity only
                # when its two formerly-required confirmations are supplied.
                approved_by = os.environ.get("USERNAME") or os.environ.get("USER")
            result = approve_flow(
                args.workspace,
                args.confirm_release_id,
                args.confirm_staging_sha256,
                approved_by,
            )
        elif args.command == "promote":
            result = promote_flow(
                args.workspace,
                args.confirm_release_id,
                args.confirm_staging_sha256,
                execute=args.execute,
                wrapper_path=args.wrapper,
            )
        else:
            result = status_flow(args.workspace)
    except (OSError, subprocess.CalledProcessError, PersonalDeployError, ValueError) as exc:
        print(f"personal deployment failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
