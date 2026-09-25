"""Maintenance journal binding, validation, and the admission fence holder."""

from __future__ import annotations

import datetime as dt
import subprocess
import time
from typing import Any

from menhir_scaffold_constants import (
    ADMISSION_LOCK, ADMISSION_READY, ADMISSION_UNIT, ATTEMPT_ID, GENERATION, HEX64,
    MAINTENANCE_STAGES, MAINTENANCE_STATE_KEYS, RELEASE_ID, RELEASE_RUN,
)
from menhir_scaffold_core import (
    ScaffoldError, atomic_json, iso, parse_time, require_root, require_safe_root_file,
    strict_load, utc_now,
)


def maintenance_binding(
    release_id: str,
    release_sha256: str,
    runner_sha256: str,
    approval_sha256: str,
    promotion_attempt_id: str,
    approved_utc: str,
    promotion_started_utc: str,
) -> dict[str, str]:
    if RELEASE_ID.fullmatch(release_id) is None:
        raise ScaffoldError("maintenance release id is invalid")
    for value, label in (
        (release_sha256, "release"),
        (runner_sha256, "runner"),
        (approval_sha256, "approval"),
    ):
        if HEX64.fullmatch(value) is None:
            raise ScaffoldError(f"maintenance {label} digest is invalid")
    if ATTEMPT_ID.fullmatch(promotion_attempt_id) is None:
        raise ScaffoldError("maintenance promotion attempt id is invalid")
    approved = parse_time(approved_utc, "maintenance approval")
    promotion_started = parse_time(promotion_started_utc, "maintenance promotion start")
    if promotion_started < approved:
        raise ScaffoldError("maintenance promotion predates approval")
    if promotion_started > utc_now() + dt.timedelta(minutes=1):
        raise ScaffoldError("maintenance promotion start is in the future")
    return {
        "release_id": release_id,
        "release_manifest_sha256": release_sha256,
        "runner_sha256": runner_sha256,
        "approval_sha256": approval_sha256,
        "promotion_attempt_id": promotion_attempt_id,
        "approved_utc": iso(approved),
        "promotion_started_utc": iso(promotion_started),
    }


def validate_maintenance_state(
    value: dict[str, Any], binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    if set(value) != MAINTENANCE_STATE_KEYS or value.get("schema") != 1 \
            or value.get("kind") != "menhir-release-run":
        raise ScaffoldError("maintenance journal schema mismatch")
    if value.get("stage") not in MAINTENANCE_STAGES:
        raise ScaffoldError("maintenance journal stage is invalid")
    generation = value.get("generation")
    if generation != "" and (
        not isinstance(generation, str) or GENERATION.fullmatch(generation) is None
    ):
        raise ScaffoldError("maintenance journal generation is invalid")
    started = parse_time(value.get("started_utc"), "maintenance start")
    updated = parse_time(value.get("updated_utc"), "maintenance update")
    approved = parse_time(value.get("approved_utc"), "maintenance approval")
    promotion_started = parse_time(
        value.get("promotion_started_utc"), "maintenance promotion start",
    )
    if started < approved or started < promotion_started or updated < started:
        raise ScaffoldError("maintenance journal chronology is invalid")
    completed_value = value.get("completed_utc")
    if value["stage"] == "complete":
        completed = parse_time(completed_value, "maintenance completion")
        if completed < started or updated < completed:
            raise ScaffoldError("maintenance completion chronology is invalid")
    elif completed_value is not None:
        raise ScaffoldError("incomplete maintenance journal has a completion timestamp")
    for key, pattern in (
        ("release_manifest_sha256", HEX64),
        ("runner_sha256", HEX64),
        ("approval_sha256", HEX64),
        ("promotion_attempt_id", ATTEMPT_ID),
    ):
        if not isinstance(value.get(key), str) or pattern.fullmatch(value[key]) is None:
            raise ScaffoldError(f"maintenance journal {key} is invalid")
    if RELEASE_ID.fullmatch(str(value.get("release_id", ""))) is None:
        raise ScaffoldError("maintenance journal release id is invalid")
    if binding is not None:
        for key, expected in binding.items():
            if value.get(key) != expected:
                raise ScaffoldError(f"maintenance journal belongs to another {key}")
    return value


def _admission_unit_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", ADMISSION_UNIT], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _admission_lock_held() -> bool:
    result = subprocess.run(
        ["flock", "-n", str(ADMISSION_LOCK), "true"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode not in {0, 1}:
        raise ScaffoldError("could not inspect the maintenance admission lock")
    return result.returncode == 1


def _ready_attempt() -> str | None:
    if not ADMISSION_READY.exists():
        return None
    require_safe_root_file(ADMISSION_READY, "maintenance admission readiness")
    value = strict_load(ADMISSION_READY)
    if set(value) != {"schema", "kind", "promotion_attempt_id"} \
            or value.get("schema") != 1 \
            or value.get("kind") != "menhir-maintenance-admission-ready" \
            or ATTEMPT_ID.fullmatch(str(value.get("promotion_attempt_id", ""))) is None:
        raise ScaffoldError("maintenance admission readiness schema mismatch")
    return str(value["promotion_attempt_id"])


def hold_maintenance(binding: dict[str, str]) -> None:
    require_root()
    if RELEASE_RUN.exists():
        require_safe_root_file(RELEASE_RUN, "maintenance journal")
        state = validate_maintenance_state(strict_load(RELEASE_RUN), binding)
        if state["stage"] == "complete":
            raise ScaffoldError("completed maintenance cannot reacquire admission")
    else:
        started = utc_now()
        state = {
            "schema": 1,
            "kind": "menhir-release-run",
            "release_id": binding["release_id"],
            "release_manifest_sha256": binding["release_manifest_sha256"],
            "stage": "start",
            "generation": "",
            "started_utc": iso(started),
            "updated_utc": iso(started),
            "completed_utc": None,
            "runner_sha256": binding["runner_sha256"],
            "approval_sha256": binding["approval_sha256"],
            "promotion_attempt_id": binding["promotion_attempt_id"],
            "approved_utc": binding["approved_utc"],
            "promotion_started_utc": binding["promotion_started_utc"],
        }
        validate_maintenance_state(state, binding)
        atomic_json(RELEASE_RUN, state)
    atomic_json(ADMISSION_READY, {
        "schema": 1,
        "kind": "menhir-maintenance-admission-ready",
        "promotion_attempt_id": binding["promotion_attempt_id"],
    })
    while True:
        time.sleep(30)
