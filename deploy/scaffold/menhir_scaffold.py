#!/usr/bin/env python3
"""Capture and verify the one-time Menhir VPS host scaffold."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

# Siblings are imported by name because this runner is deployed as a bare script.
_SCAFFOLD_DIR = str(Path(__file__).resolve().parent)
if _SCAFFOLD_DIR not in sys.path:
    sys.path.append(_SCAFFOLD_DIR)

from menhir_scaffold_constants import (
    ADMISSION_LOCK, ADMISSION_READY, ADMISSION_UNIT, ATTEMPT_ID, BACKUP_RECEIPT, CONTRACT_KEYS,
    CONTRACT_PATH, DESKTOP_RECEIPT, DRILL_RECEIPT, ENCRYPTED_BACKUP_ROOT, FIRST_MUTATION, GENERATION,
    HEX64, MAINTENANCE_HISTORY, MAINTENANCE_MARKERS, MAINTENANCE_STAGES, MAINTENANCE_STATE_KEYS,
    RECEIPT_KEYS, RECEIPT_PATH, RELEASE_ID, RELEASE_PATH, RELEASE_RUN, REHEARSAL_RECEIPT, SAFE_REASON,
    SCAFFOLD_DRILL_KEYS, SCAFFOLD_DRILL_METHODS, SCHEMA_PATH, STATUS_ROOT, VERIFY_ARTIFACTS,
)
from menhir_scaffold_core import (
    ScaffoldError, age_hours, atomic_json, build_receipt, iso, machine_id_digest, parse_time,
    read_os_release, require_root, require_safe_root_file, run, sha256_file, stat_row, strict_load,
    utc_now,
)
from menhir_scaffold_evidence import (evaluate_evidence, inspect_runtime, public_ready, write_drill_receipt)
from menhir_scaffold_maintenance import (_admission_lock_held, _admission_unit_active,
    _ready_attempt, hold_maintenance, maintenance_binding, validate_maintenance_state)
from menhir_scaffold_verify import (assert_contract_matches, capture, grp, inspect_network,
    observe_static, pwd, validate_contract, verify_encrypted_archives, verify_static)
from menhir_scaffold_cli import (add_maintenance_binding_arguments, binding_from_arguments, parser)


def assert_maintenance(binding: dict[str, str]) -> dict[str, Any]:
    require_root()
    require_safe_root_file(RELEASE_RUN, "maintenance journal")
    state = validate_maintenance_state(strict_load(RELEASE_RUN), binding)
    if not _admission_unit_active() or not _admission_lock_held() \
            or _ready_attempt() != binding["promotion_attempt_id"]:
        raise ScaffoldError("maintenance admission fence is not held by this transaction")
    return state


def _archive_maintenance_markers(archive: Path) -> dict[str, str]:
    moved: dict[str, str] = {}
    for name in MAINTENANCE_MARKERS:
        source = STATUS_ROOT / name
        if source.exists():
            require_safe_root_file(source, name)
            digest = sha256_file(source)
            os.replace(source, archive / name)
            moved[name] = digest
    return moved


def _mutation_belongs_to(state: dict[str, Any]) -> bool:
    """True when first-mutation was written during this maintenance.

    The marker is the point of no return for one transaction. A marker older
    than the journal belongs to a previous cycle whose completion did not
    archive it, and must not veto abandoning the current one.
    """
    if not FIRST_MUTATION.exists():
        return False
    started = parse_time(state.get("started_utc"), "maintenance start")
    mutated = dt.datetime.fromtimestamp(FIRST_MUTATION.stat().st_mtime, dt.timezone.utc)
    return mutated >= started


def _archive_completed_maintenance() -> None:
    require_safe_root_file(RELEASE_RUN, "completed maintenance journal")
    payload_sha = sha256_file(RELEASE_RUN)
    MAINTENANCE_HISTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(MAINTENANCE_HISTORY, 0, 0)
    target = MAINTENANCE_HISTORY / f"{payload_sha}.json"
    if target.exists():
        require_safe_root_file(target, "archived maintenance journal")
        if sha256_file(target) != payload_sha:
            raise ScaffoldError("maintenance history digest collision")
        RELEASE_RUN.unlink()
    else:
        os.replace(RELEASE_RUN, target)
    # The cycle's markers go with its journal. A completed maintenance must
    # leave nothing behind that the next begin-maintenance or abandon reads as
    # its own.
    markers_dir = MAINTENANCE_HISTORY / f"{payload_sha}.markers"
    markers_dir.mkdir(exist_ok=True, mode=0o700)
    _archive_maintenance_markers(markers_dir)


def begin_maintenance(binding: dict[str, str]) -> dict[str, Any]:
    require_root()
    if RELEASE_RUN.exists():
        require_safe_root_file(RELEASE_RUN, "maintenance journal")
        current = strict_load(RELEASE_RUN)
        if set(current) == MAINTENANCE_STATE_KEYS:
            current = validate_maintenance_state(current)
            if current["stage"] == "complete":
                if all(current.get(key) == value for key, value in binding.items()):
                    raise ScaffoldError("completed maintenance must be adopted, not restarted")
                _archive_completed_maintenance()
            else:
                validate_maintenance_state(current, binding)
        elif current.get("kind") == "menhir-release-run" and current.get("stage") == "complete":
            _archive_completed_maintenance()
        else:
            raise ScaffoldError("an incompatible maintenance journal is active")
    if _admission_unit_active():
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if _admission_lock_held() \
                    and _ready_attempt() == binding["promotion_attempt_id"]:
                return assert_maintenance(binding)
            time.sleep(0.1)
        raise ScaffoldError("active maintenance admission holder is not ready")
    if ADMISSION_READY.exists():
        require_safe_root_file(ADMISSION_READY, "stale maintenance admission readiness")
        ADMISSION_READY.unlink()
    subprocess.run(
        ["systemctl", "reset-failed", ADMISSION_UNIT], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    command = [
        "systemd-run", f"--unit={ADMISSION_UNIT}", "--collect",
        "--property=Type=simple", "--property=Restart=no",
        "/usr/bin/flock", "-n", "-E", "75", str(ADMISSION_LOCK),
        str(Path(__file__).resolve()), "hold-maintenance",
        *sum(([f"--{key.replace('_', '-')}", value] for key, value in binding.items()), []),
    ]
    started = subprocess.run(command, check=False, capture_output=True, text=True)
    if started.returncode != 0:
        raise ScaffoldError("could not start the root maintenance admission holder")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _admission_unit_active() and _admission_lock_held() \
                and _ready_attempt() == binding["promotion_attempt_id"]:
            return assert_maintenance(binding)
        time.sleep(0.1)
    raise ScaffoldError("root maintenance admission holder did not become ready")


def _prove_artifact_only_complete(
    state: dict[str, Any], binding: dict[str, str],
    contract_path: Path, receipt_path: Path,
) -> None:
    """Prove that this maintenance is finished without a cutover.

    A release that ships the prior release's image unchanged has nothing to
    cut over: the installer places the artifacts and stops, and the only lane
    to `complete` -- candidate, accept, promote -- would deploy the identical
    image beside itself for no reason. Before this, such a maintenance could
    never close, which blocked the app-only lane and failed the audit.

    Completion is proven, not declared: the live release authority must be the
    one this maintenance was bound to, every installed artifact must verify,
    the running containers must carry that authority's image digests, no
    candidate may exist, and the public endpoint must be ready.
    """
    if state["stage"] != "start":
        raise ScaffoldError("artifact-only completion applies to a maintenance at start")
    require_safe_root_file(RELEASE_PATH, "live release authority")
    if sha256_file(RELEASE_PATH) != binding["release_manifest_sha256"]:
        raise ScaffoldError("live release authority is not this maintenance's release")
    verified = subprocess.run(
        [str(VERIFY_ARTIFACTS)], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if verified.returncode != 0:
        raise ScaffoldError("installed artifacts do not verify against the release authority")
    contract = verify_static(contract_path, receipt_path)["contract"]
    healthy, candidates = inspect_runtime(contract)
    if not healthy:
        raise ScaffoldError("production is not running this release's images healthily")
    if candidates:
        raise ScaffoldError("candidate containers exist; this is not artifact-only")
    if not public_ready(contract["runtime"]["public_ready_url"]):
        raise ScaffoldError("public endpoint is not ready")


def complete_maintenance(
    binding: dict[str, str],
    *,
    artifact_only: bool = False,
    contract_path: Path | None = None,
    receipt_path: Path | None = None,
) -> dict[str, Any]:
    require_root()
    require_safe_root_file(RELEASE_RUN, "maintenance journal")
    state = validate_maintenance_state(strict_load(RELEASE_RUN), binding)
    if artifact_only and state["stage"] != "complete":
        _prove_artifact_only_complete(
            state, binding, contract_path or CONTRACT_PATH, receipt_path or RECEIPT_PATH,
        )
        now = iso(utc_now())
        state = {**state, "stage": "complete", "updated_utc": now, "completed_utc": now}
        validate_maintenance_state(state, binding)
        atomic_json(RELEASE_RUN, state)
    if state["stage"] != "complete":
        raise ScaffoldError("maintenance admission cannot close before acceptance")
    stopped = subprocess.run(
        ["systemctl", "stop", ADMISSION_UNIT], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if stopped.returncode != 0 and _admission_unit_active():
        raise ScaffoldError("could not release the maintenance admission fence")
    if ADMISSION_READY.exists():
        require_safe_root_file(ADMISSION_READY, "maintenance admission readiness")
        ADMISSION_READY.unlink()
    return state


def validate_scaffold_drill(backup: dict[str, Any]) -> dict[str, Any]:
    """Validate the original scaffold-owned restore-drill receipt."""
    require_safe_root_file(DRILL_RECEIPT, "restore drill receipt")
    drill = strict_load(DRILL_RECEIPT)
    if set(drill) != SCAFFOLD_DRILL_KEYS or drill.get("schema") != 1 \
            or drill.get("kind") != "menhir-scaffold-restore-drill":
        raise ScaffoldError("restore drill receipt schema mismatch")
    if not isinstance(drill.get("generation"), str) \
            or not GENERATION.fullmatch(drill["generation"]):
        raise ScaffoldError("restore drill generation is invalid")
    if not HEX64.fullmatch(drill.get("backup_receipt_sha256", "")) \
            or drill["backup_receipt_sha256"] != sha256_file(BACKUP_RECEIPT):
        raise ScaffoldError("restore drill does not bind the current backup receipt")
    if drill.get("method") not in SCAFFOLD_DRILL_METHODS:
        raise ScaffoldError("restore drill method is invalid")
    parse_time(drill.get("checked_utc"), "restore drill")
    parse_time(drill.get("recorded_utc"), "restore drill recording")
    return drill


def validate_release_rehearsal(backup: dict[str, Any]) -> dict[str, Any]:
    """Validate maintenance rehearsal evidence against the current backup and release."""
    require_safe_root_file(REHEARSAL_RECEIPT, "release rehearsal receipt")
    require_safe_root_file(RELEASE_PATH, "live release descriptor")
    require_safe_root_file(SCHEMA_PATH, "backup promotion validator")
    rehearsal = strict_load(REHEARSAL_RECEIPT)
    binding = backup.get("release")
    if not isinstance(binding, dict):
        raise ScaffoldError("backup receipt release binding is invalid")
    run([
        "python3", str(SCHEMA_PATH), "validate-receipt-binding",
        str(REHEARSAL_RECEIPT), "rehearsal", str(RELEASE_PATH),
        str(backup.get("generation", "")), str(backup.get("manifest_sha256", "")),
        str(binding.get("menhir_image_digest", "")),
        str(binding.get("neo4j_image_digest", "")),
    ])
    return rehearsal


def restore_drill_evidence(
    backup: dict[str, Any], max_age_hours: int, now: dt.datetime,
) -> dict[str, Any]:
    """Choose the newest valid restore proof bound to the current backup generation."""
    candidates: list[dict[str, Any]] = []
    refusals: list[str] = []

    def admit(receipt: dict[str, Any], source: str) -> None:
        checked = parse_time(receipt.get("checked_utc"), "restore drill")
        if age_hours(checked, now) > max_age_hours:
            raise ScaffoldError("restore drill is stale")
        candidates.append({**receipt, "source": source})

    if DRILL_RECEIPT.exists():
        try:
            drill = validate_scaffold_drill(backup)
            if drill.get("generation") != backup.get("generation"):
                raise ScaffoldError(
                    "scaffold restore drill is not bound to the current VPS generation"
                )
            admit(drill, "scaffold-restore-drill")
        except ScaffoldError as exc:
            refusals.append(str(exc))
    if REHEARSAL_RECEIPT.exists():
        try:
            rehearsal = validate_release_rehearsal(backup)
            admit(rehearsal, "release-rehearsal")
        except ScaffoldError as exc:
            refusals.append(str(exc))
    if not candidates:
        detail = "; ".join(refusals) if refusals else "no restore receipt exists"
        raise ScaffoldError(f"no valid current-generation restore drill: {detail}")
    return max(
        candidates,
        key=lambda item: parse_time(item.get("checked_utc"), "restore drill"),
    )


def operational_evidence(
    contract: dict[str, Any], now: dt.datetime | None = None,
) -> dict[str, Any]:
    audit_now = now or utc_now()
    require_safe_root_file(BACKUP_RECEIPT, "VPS backup receipt")
    require_safe_root_file(DESKTOP_RECEIPT, "desktop archive receipt")
    backup = strict_load(BACKUP_RECEIPT)
    desktop = strict_load(DESKTOP_RECEIPT)
    require_safe_root_file(SCHEMA_PATH, "backup promotion validator")
    run(["python3", str(SCHEMA_PATH), "validate-receipt", str(BACKUP_RECEIPT), "backup-local"])
    retained_generations = verify_encrypted_archives(backup)
    drill = restore_drill_evidence(
        backup, contract["backup_policy"]["restore_drill_max_age_hours"], audit_now,
    )
    runtime_healthy, candidates = inspect_runtime(contract)
    stage = None
    if RELEASE_RUN.exists():
        stage = strict_load(RELEASE_RUN).get("stage")
    app_only_stage = None
    app_only_active = STATUS_ROOT / "app-only-active.json"
    if app_only_active.exists():
        require_safe_root_file(app_only_active, "active app-only transaction")
        app_only_stage = strict_load(app_only_active).get("stage")
    security_config_stage = None
    security_config_active = STATUS_ROOT / "security-config-active.json"
    if security_config_active.exists():
        require_safe_root_file(
            security_config_active, "active security-config transaction"
        )
        security_config_stage = strict_load(security_config_active).get("stage")
    return {
        "encrypted_generations": len(retained_generations),
        "retained_generations": retained_generations,
        "vps_backup_utc": backup.get("checked_utc"),
        "desktop_archive_utc": desktop.get("archived_utc"),
        "restore_drill_utc": drill.get("checked_utc"),
        "backup_generation": backup.get("generation"),
        "desktop_generation": desktop.get("generation"),
        "drill_generation": drill.get("generation"),
        "restore_drill_source": drill.get("source"),
        "maintenance_stage": stage,
        "app_only_stage": app_only_stage,
        "security_config_stage": security_config_stage,
        "candidate_containers": candidates,
        "runtime_healthy": runtime_healthy,
        "public_ready": public_ready(contract["runtime"]["public_ready_url"]),
    }


def verify_installed_artifacts() -> str:
    # Until release 0.2.0-16 the OAuth gateway ran verify-artifacts as its
    # ExecStartPre, so a drifted install failed at boot. The gateway is gone;
    # the daily audit is now the only unattended run of the verifier.
    if not VERIFY_ARTIFACTS.is_file():
        raise ScaffoldError(f"installed artifact verifier is missing: {VERIFY_ARTIFACTS}")
    verified = subprocess.run(
        [str(VERIFY_ARTIFACTS)], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if verified.returncode != 0:
        raise ScaffoldError("installed artifacts do not verify against the release authority")
    return "ok"


def verify_app_only(contract_path: Path, receipt_path: Path) -> dict[str, Any]:
    verified = verify_static(contract_path, receipt_path)
    artifacts = verify_installed_artifacts()
    now = utc_now()
    evidence = operational_evidence(verified["contract"], now)
    failures = evaluate_evidence(verified["contract"]["backup_policy"], evidence, now)
    if failures:
        raise ScaffoldError("app-only admission refused: " + "; ".join(failures))
    return {"static": "ok", "artifacts": artifacts, "app_only": "admitted", "evidence": evidence}


def seed_drill() -> dict[str, Any]:
    require_root()
    require_safe_root_file(BACKUP_RECEIPT, "VPS backup receipt")
    backup = strict_load(BACKUP_RECEIPT)
    if DRILL_RECEIPT.exists():
        try:
            current = validate_scaffold_drill(backup)
            if current.get("generation") == backup.get("generation"):
                return current
        except ScaffoldError:
            pass
    rehearsal = validate_release_rehearsal(backup)
    return write_drill_receipt(
        backup, rehearsal.get("checked_utc"), "release-rehearsal-clean-load-and-consistency-check",
    )


def abandon_maintenance(contract_path: Path, receipt_path: Path, reason: str) -> dict[str, Any]:
    require_root()
    if not SAFE_REASON.fullmatch(reason):
        raise ScaffoldError("maintenance-abort reason is invalid")
    verified = verify_static(contract_path, receipt_path)
    if not RELEASE_RUN.exists():
        raise ScaffoldError("there is no active maintenance transaction")
    state = validate_maintenance_state(strict_load(RELEASE_RUN))
    # Only a mutation made by THIS maintenance forbids abandoning it. A marker
    # left by an earlier, completed cycle is archived below with everything else.
    if _mutation_belongs_to(state):
        raise ScaffoldError("cannot abandon maintenance after first mutation")
    if state.get("stage") not in {"start", "backup", "staged", "rehearsal", "candidate", "accepted", "routed"}:
        raise ScaffoldError("maintenance transaction is not safely pre-mutation")
    healthy, candidates = inspect_runtime(verified["contract"])
    if not healthy or candidates or not public_ready(verified["contract"]["runtime"]["public_ready_url"]):
        raise ScaffoldError("healthy exact production without candidates is required before archival")
    release = strict_load(RELEASE_PATH)
    if state.get("stage") != "start" and state.get("release_id") != release.get("release_id"):
        # Past `start`, the maintenance's own release is installed and the live
        # authority must agree. At `start` nothing has been installed under
        # this journal -- including after an install that rolled back, which
        # restores the prior release and is exactly the case abandon exists for.
        raise ScaffoldError("maintenance state is not bound to the live release")
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    archive = STATUS_ROOT / "abandoned" / f"{timestamp}-{state['release_id']}"
    archive.mkdir(parents=True, mode=0o700)
    moved: dict[str, str] = {}
    journal_digest = sha256_file(RELEASE_RUN)
    os.replace(RELEASE_RUN, archive / "release-run.json")
    moved["release-run.json"] = journal_digest
    moved.update(_archive_maintenance_markers(archive))
    value = {
        "schema": 1,
        "kind": "menhir-maintenance-abort",
        "release_id": state["release_id"],
        "stage": state["stage"],
        "reason": reason,
        "aborted_utc": iso(utc_now()),
        "archive": str(archive),
        "moved_sha256": moved,
        "first_mutation": False,
        "production_verified": True,
    }
    atomic_json(archive / "ABORT-RECEIPT.json", value)
    subprocess.run(
        ["systemctl", "stop", ADMISSION_UNIT], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if ADMISSION_READY.exists():
        require_safe_root_file(ADMISSION_READY, "maintenance admission readiness")
        ADMISSION_READY.unlink()
    return value


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "capture":
            value = capture(args.contract, args.receipt)
        elif args.command == "verify":
            value = verify_app_only(args.contract, args.receipt) if args.app_only \
                else {"static": "ok", "receipt": verify_static(args.contract, args.receipt)["receipt"]}
        elif args.command == "status":
            verified = verify_static(args.contract, args.receipt)
            now = utc_now()
            evidence = operational_evidence(verified["contract"], now)
            value = {
                "static": "ok", "evidence": evidence,
                "app_only_failures": evaluate_evidence(
                    verified["contract"]["backup_policy"], evidence, now,
                ),
            }
        elif args.command == "seed-drill":
            value = seed_drill()
        elif args.command == "begin-maintenance":
            value = begin_maintenance(binding_from_arguments(args))
        elif args.command == "hold-maintenance":
            hold_maintenance(binding_from_arguments(args))
            return 0
        elif args.command == "assert-maintenance":
            value = assert_maintenance(binding_from_arguments(args))
        elif args.command == "complete-maintenance":
            value = complete_maintenance(
                binding_from_arguments(args),
                artifact_only=args.artifact_only,
                contract_path=args.contract,
                receipt_path=args.receipt,
            )
        else:
            value = abandon_maintenance(args.contract, args.receipt, args.reason)
    except ScaffoldError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
