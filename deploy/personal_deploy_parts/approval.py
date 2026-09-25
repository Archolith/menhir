"""Approval flow: verify staging, then write one receipt-bound approval."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import APPROVAL_KEYS, APPROVAL_NAME
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
    if state["phase"] in {"approved", "promoting", "promoted"}:
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
        "promotion_wrapper_sha256": state["promotion_wrapper_sha256"],
        "operator_wrapper_sha256": state["operator_wrapper_sha256"],
        "root_runner_sha256": state["root_runner_sha256"],
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
        "promotion_wrapper_sha256": state["promotion_wrapper_sha256"],
        "operator_wrapper_sha256": state["operator_wrapper_sha256"],
        "root_runner_sha256": state["root_runner_sha256"],
    }
    for key, value in expected.items():
        if approval.get(key) != value:
            raise PersonalDeployError(f"promotion approval {key} mismatch")
    if not isinstance(approval.get("approved_by"), str) \
            or re.fullmatch(r"[A-Za-z0-9._@+-]{1,128}", approval["approved_by"]) is None:
        raise PersonalDeployError("promotion approval identity is invalid")
    _utc(approval.get("approved_utc"), "approved_utc")
    return digest
