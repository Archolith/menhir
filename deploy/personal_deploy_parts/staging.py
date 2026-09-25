"""Staging flow: runner commands, staging receipt validation, and rehearsal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .binding import _release_binding
from .constants import (
    BUNDLE_NAME,
    MAX_STAGING_AGE,
    POWERSHELL,
    PREFLIGHT_CHECK_KEYS,
    PREFLIGHT_KEYS,
    SHA256_RE,
    STAGING_CHECKS,
    STAGING_KEYS,
    STAGING_RECEIPT_NAME,
)
from .fsio import (
    PersonalDeployError,
    _atomic_json,
    _directory,
    _exact_keys,
    _load_json,
    _regular_file,
    _sha256,
    _utc,
)
from .state import _load_state, _state_path, select_flow


def _runner(path: Path, label: str) -> Path:
    path = _regular_file(path, label)
    if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
        raise PersonalDeployError(f"{label} is not executable")
    return path


def _runner_sha256(path: Path) -> str:
    """Return the root staging authority that the VPS verifies at runtime."""
    if path.name.lower() != "personal_stage.ps1":
        return _sha256(path)
    companion = _regular_file(path.with_name("personal_stage_vps.py"), "VPS staging runner")
    return _sha256(companion)


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
