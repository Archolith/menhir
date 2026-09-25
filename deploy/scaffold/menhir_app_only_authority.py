"""Promotion authority, transaction receipt, and live-equality validation."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from menhir_app_only_constants import (
    APPROVAL_KEYS, APPROVAL_NAME, BUNDLE_ID, HEX64, LAST, LIVE_ENV, LIVE_RELEASE, PREFLIGHT_KEYS,
    STATUS, STAGING_CHECKS, STAGING_KEYS, STAGING_RECEIPT_MAX_AGE, STAGING_RECEIPT_NAME,
)
from menhir_app_only_core import (
    AppOnlyError, parse_env, parse_utc, require_root_file, sha256, strict_load,
)
from menhir_app_only_runtime import inspect_cloudflared, inspect_container


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
    host_now = dt.datetime.now(dt.timezone.utc)
    if staging_completed < host_now - STAGING_RECEIPT_MAX_AGE:
        raise AppOnlyError("staging receipt is more than 24 hours old")
    if staging_completed < staging_started or preflight_observed > staging_started \
            or approved < staging_completed \
            or promotion_started < approved \
            or promotion_started > host_now + dt.timedelta(minutes=1):
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
