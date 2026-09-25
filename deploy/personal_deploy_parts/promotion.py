"""Promotion and status flows: promotion receipts, transactions, and the wrapper."""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable

from .approval import _verify_approval
from .constants import (
    APPROVAL_NAME,
    BUNDLE_NAME,
    DEFAULT_PROMOTION_WRAPPER,
    POWERSHELL,
    PROMOTION_KEYS,
    PROMOTION_RECEIPT_NAME,
    ROOT_TRANSACTION_RECEIPT_NAME,
    SCRIPT_DIR,
    SHA256_RE,
    STAGING_RECEIPT_NAME,
)
from .fsio import (
    PersonalDeployError,
    _atomic_json,
    _directory,
    _exact_keys,
    _load_json,
    _now,
    _regular_file,
    _sha256,
    _utc,
)
from .state import _load_state, _state_path
from .staging import _verify_staging


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
        "promotion_wrapper_sha256": state["promotion_wrapper_sha256"],
        "operator_wrapper_sha256": state["operator_wrapper_sha256"],
        "root_runner_sha256": state["root_runner_sha256"],
        "promotion_attempt_id": state["promotion_attempt_id"],
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
    expected_budget = {"app-only": 300, "security-config": 600}.get(
        state["deployment_class"]
    )
    if expected_budget is not None and elapsed > expected_budget:
        raise PersonalDeployError("promotion exceeded its foreground time budget")
    if receipt.get("transaction_kind") != state["deployment_class"]:
        raise PersonalDeployError("promotion receipt transaction kind mismatch")
    transaction_path = path.with_name(ROOT_TRANSACTION_RECEIPT_NAME)
    if _sha256(_regular_file(transaction_path, "root transaction receipt")) \
            != receipt.get("transaction_receipt_sha256"):
        raise PersonalDeployError("promotion receipt root transaction digest mismatch")
    transaction = _load_json(transaction_path, "root transaction receipt")
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
    if transaction["runner_sha256"] != state["root_runner_sha256"]:
        raise PersonalDeployError("root transaction runner differs from approved authority")
    approval = _load_json(path.with_name(APPROVAL_NAME), "promotion approval")
    common_authority = {
        "bundle_sha256": state["bundle_sha256"],
        "staging_receipt_sha256": state["staging_receipt_sha256"],
        "approval_sha256": approval_sha,
        "approved_by": approval["approved_by"],
        "approved_utc": approval["approved_utc"],
        "promotion_wrapper_sha256": state["promotion_wrapper_sha256"],
        "operator_wrapper_sha256": state["operator_wrapper_sha256"],
        "promotion_attempt_id": state["promotion_attempt_id"],
        "promotion_started_utc": state["promotion_started_utc"],
    }
    for key, expected in common_authority.items():
        if transaction.get(key) != expected:
            raise PersonalDeployError(f"root transaction {key} mismatch")
    transaction_started = _utc(transaction.get("started_utc"), "transaction started_utc")
    transaction_completed = _utc(transaction.get("completed_utc"), "transaction completed_utc")
    approved = _utc(approval.get("approved_utc"), "approval approved_utc")
    if transaction_started < started or transaction_started < approved \
            or transaction_completed > completed \
            or transaction_completed < transaction_started:
        raise PersonalDeployError("root transaction timestamps escape the promotion window")
    if receipt.get("started_utc") != state["promotion_started_utc"]:
        raise PersonalDeployError("promotion receipt start differs from its persisted attempt")
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
        "-SourceRepository", str(SCRIPT_DIR.parent),
        "-ExpectedPromotionWrapperSha256", state["promotion_wrapper_sha256"],
        "-ExpectedOperatorWrapperSha256", state["operator_wrapper_sha256"],
        "-ExpectedRootRunnerSha256", state["root_runner_sha256"],
        "-PromotionAttemptId", state["promotion_attempt_id"],
        "-PromotionStartedUtc", state["promotion_started_utc"],
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
    wrapper = _regular_file(DEFAULT_PROMOTION_WRAPPER, "production promotion wrapper")
    if wrapper_path.resolve() != wrapper.resolve():
        raise PersonalDeployError("production promotion wrapper override is not allowed")
    if _sha256(wrapper) != state["promotion_wrapper_sha256"]:
        raise PersonalDeployError("production promotion wrapper changed after selection")
    receipt_path = workspace / PROMOTION_RECEIPT_NAME
    if state["phase"] == "promoted":
        if _sha256(_regular_file(receipt_path, "promotion receipt")) \
                != state["promotion_receipt_sha256"]:
            raise PersonalDeployError("recorded promotion receipt changed")
        _validate_promotion_receipt(receipt_path, state, approval_sha)
        return state
    if state["phase"] not in {"approved", "promoting"}:
        raise PersonalDeployError("only an approved release can be promoted")
    if not execute:
        preview = dict(state)
        if preview["phase"] == "approved":
            preview["promotion_attempt_id"] = secrets.token_hex(16)
            preview["promotion_started_utc"] = _now()
        return _promotion_command(workspace, preview, wrapper)
    transaction_path = workspace / ROOT_TRANSACTION_RECEIPT_NAME
    if state["phase"] == "approved":
        if receipt_path.exists() or receipt_path.is_symlink() \
                or transaction_path.exists() or transaction_path.is_symlink():
            raise PersonalDeployError("unexpected receipt exists before promotion attempt")
        state["phase"] = "promoting"
        state["promotion_attempt_id"] = secrets.token_hex(16)
        state["promotion_started_utc"] = _now()
        _atomic_json(_state_path(workspace), state)
    command = _promotion_command(workspace, state, wrapper)
    if receipt_path.exists() or receipt_path.is_symlink():
        _validate_promotion_receipt(receipt_path, state, approval_sha)
    else:
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
    if state["phase"] in {"staged", "approved", "promoting", "promoted"}:
        _verify_staging(workspace, state)
    if state["phase"] in {"approved", "promoting", "promoted"}:
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
