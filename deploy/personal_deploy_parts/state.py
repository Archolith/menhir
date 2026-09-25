"""Personal deployment state persistence: load, validate, and select."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .binding import _release_binding
from .constants import ATTEMPT_RE, KIND, PHASES, SCHEMA, SHA256_RE, STATE_KEYS, STATE_NAME
from .fsio import (
    PersonalDeployError,
    _atomic_json,
    _directory,
    _exact_keys,
    _load_json,
    _utc,
)


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
    attempt = state.get("promotion_attempt_id")
    started = state.get("promotion_started_utc")
    if state["phase"] in {"promoting", "promoted"}:
        if not isinstance(attempt, str) or ATTEMPT_RE.fullmatch(attempt) is None:
            raise PersonalDeployError("promotion attempt identity is invalid")
        _utc(started, "promotion_started_utc")
    elif attempt is not None or started is not None:
        raise PersonalDeployError("inactive deployment has promotion attempt state")
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
        "promotion_attempt_id": None,
        "promotion_started_utc": None,
    }
    _atomic_json(_state_path(workspace), state)
    return state
