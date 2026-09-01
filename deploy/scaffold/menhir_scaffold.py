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
import urllib.request
from pathlib import Path
from typing import Any

try:  # The verifier runs on Linux; this keeps pure policy tests importable on Windows.
    import grp
    import pwd
except ModuleNotFoundError:  # pragma: no cover - exercised only by non-POSIX test hosts
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]


CONTRACT_PATH = Path("/etc/menhir/scaffold-contract.json")
RECEIPT_PATH = Path("/var/lib/menhir-production/scaffold-receipt.json")
STATUS_ROOT = Path("/var/lib/menhir-production")
RELEASE_PATH = Path("/srv/menhir/production/release/release.json")
BACKUP_RECEIPT = STATUS_ROOT / "backup-local-receipt.json"
DESKTOP_RECEIPT = STATUS_ROOT / "desktop-archive-receipt.json"
DRILL_RECEIPT = STATUS_ROOT / "scaffold-restore-drill-receipt.json"
RELEASE_RUN = STATUS_ROOT / "release-run.json"
FIRST_MUTATION = STATUS_ROOT / "first-mutation"
HEX64 = re.compile(r"[0-9a-f]{64}")
SAFE_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/+-]{0,255}")
CONTRACT_KEYS = {
    "schema", "kind", "host", "directories", "files", "identities",
    "groups", "network", "units", "backup_policy", "runtime",
}
RECEIPT_KEYS = {
    "schema", "kind", "contract_sha256", "verifier_sha256",
    "machine_id_sha256", "captured_utc", "static",
}


class ScaffoldError(RuntimeError):
    """Raised when the host cannot prove the scaffold contract."""


def strict_load(path: Path) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ScaffoldError(f"duplicate JSON key in {path}: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScaffoldError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScaffoldError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def parse_time(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str):
        raise ScaffoldError(f"{label} timestamp is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScaffoldError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ScaffoldError(f"{label} timestamp lacks a timezone")
    return parsed.astimezone(dt.timezone.utc)


def require_root() -> None:
    if os.geteuid() != 0:
        raise ScaffoldError("this command must run as root")


def require_safe_root_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ScaffoldError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ScaffoldError(f"{label} must be a regular non-symlink file: {path}")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise ScaffoldError(f"{label} must be root-owned and not group/other writable")


def validate_contract(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != CONTRACT_KEYS or value.get("schema") != 1 \
            or value.get("kind") != "menhir-host-scaffold-contract":
        raise ScaffoldError("scaffold contract schema mismatch")
    for name in ("directories", "files", "identities", "groups", "units"):
        if not isinstance(value[name], list) or not value[name]:
            raise ScaffoldError(f"contract {name} must be a non-empty list")
    for row in value["directories"]:
        if set(row) != {"path", "uid", "gid", "mode"}:
            raise ScaffoldError("directory contract row mismatch")
    for row in value["files"]:
        if set(row) != {"path", "uid", "gid", "mode", "digest"}:
            raise ScaffoldError("file contract row mismatch")
        if not isinstance(row["digest"], bool):
            raise ScaffoldError("file digest flag must be boolean")
    paths = [row["path"] for row in value["directories"] + value["files"]]
    if len(paths) != len(set(paths)) or any(
            not isinstance(path, str) or not path.startswith("/")
            or ".." in path.split("/") for path in paths
    ):
        raise ScaffoldError("contract paths must be unique safe absolute paths")
    policy = value["backup_policy"]
    if set(policy) != {
        "minimum_encrypted_generations", "vps_backup_max_age_hours",
        "desktop_archive_max_age_hours", "restore_drill_max_age_hours",
    } or any(not isinstance(item, int) or item <= 0 for item in policy.values()):
        raise ScaffoldError("backup policy is invalid")
    return value


def run(command: list[str], *, check: bool = True) -> str:
    try:
        result = subprocess.run(
            command, check=check, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScaffoldError(f"command failed: {' '.join(command)}: {exc}") from exc
    return result.stdout.strip()


def stat_row(path: str, include_digest: bool, expected_type: str) -> dict[str, Any]:
    target = Path(path)
    try:
        info = target.lstat()
    except OSError as exc:
        raise ScaffoldError(f"required scaffold path is missing: {path}") from exc
    if target.is_symlink():
        raise ScaffoldError(f"scaffold path must not be a symlink: {path}")
    if expected_type == "directory" and not stat.S_ISDIR(info.st_mode):
        raise ScaffoldError(f"scaffold directory is not a directory: {path}")
    if expected_type == "file" and not stat.S_ISREG(info.st_mode):
        raise ScaffoldError(f"scaffold file is not a regular file: {path}")
    row: dict[str, Any] = {
        "path": path,
        "uid": info.st_uid,
        "gid": info.st_gid,
        "mode": f"{stat.S_IMODE(info.st_mode):04o}",
    }
    if include_digest:
        if not stat.S_ISREG(info.st_mode):
            raise ScaffoldError(f"digest-bound scaffold path is not a file: {path}")
        row["sha256"] = sha256_file(target)
    return row


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return {"os_id": values.get("ID", ""), "os_version": values.get("VERSION_ID", "")}


def inspect_network(name: str) -> dict[str, str]:
    values = json.loads(run(["docker", "network", "inspect", name]))
    if not isinstance(values, list) or len(values) != 1:
        raise ScaffoldError(f"Docker network inspection is ambiguous: {name}")
    value = values[0]
    configs = value.get("IPAM", {}).get("Config", [])
    if len(configs) != 1:
        raise ScaffoldError(f"Docker network has unexpected IPAM configuration: {name}")
    return {
        "name": value.get("Name", ""),
        "driver": value.get("Driver", ""),
        "subnet": configs[0].get("Subnet", ""),
        "gateway": configs[0].get("Gateway", ""),
    }


def observe_static(contract: dict[str, Any]) -> dict[str, Any]:
    if pwd is None or grp is None:
        raise ScaffoldError("POSIX identity inspection is unavailable")
    directories = [
        stat_row(row["path"], False, "directory") for row in contract["directories"]
    ]
    files = [
        stat_row(row["path"], row["digest"], "file") for row in contract["files"]
    ]
    identities = []
    for row in contract["identities"]:
        try:
            value = pwd.getpwnam(row["name"])
        except KeyError as exc:
            raise ScaffoldError(f"required user is absent: {row['name']}") from exc
        identities.append({
            "name": value.pw_name, "uid": value.pw_uid, "gid": value.pw_gid,
            "home": value.pw_dir, "shell": value.pw_shell,
        })
    groups = []
    for row in contract["groups"]:
        try:
            value = grp.getgrnam(row["name"])
        except KeyError as exc:
            raise ScaffoldError(f"required group is absent: {row['name']}") from exc
        groups.append({
            "name": value.gr_name, "gid": value.gr_gid,
            "members": sorted(value.gr_mem),
        })
    units = []
    for row in contract["units"]:
        units.append({
            "name": row["name"],
            "enabled": run(["systemctl", "is-enabled", row["name"]], check=False),
            "active": run(["systemctl", "is-active", row["name"]], check=False),
        })
    return {
        "host": read_os_release(),
        "directories": directories,
        "files": files,
        "identities": identities,
        "groups": groups,
        "network": inspect_network(contract["network"]["name"]),
        "units": units,
    }


def assert_contract_matches(contract: dict[str, Any], observed: dict[str, Any]) -> None:
    for name in ("host", "network"):
        if observed[name] != contract[name]:
            raise ScaffoldError(f"scaffold {name} differs from contract")
    for name in ("directories", "identities", "groups", "units"):
        expected = contract[name]
        actual = observed[name]
        if name == "groups":
            expected = [{**row, "members": sorted(row["members"])} for row in expected]
        if actual != expected:
            raise ScaffoldError(f"scaffold {name} differs from contract")
    expected_files = [{k: row[k] for k in ("path", "uid", "gid", "mode")} for row in contract["files"]]
    actual_files = [{k: row[k] for k in ("path", "uid", "gid", "mode")} for row in observed["files"]]
    if actual_files != expected_files:
        raise ScaffoldError("scaffold files differ from contract")
    for expected, actual in zip(contract["files"], observed["files"], strict=True):
        if expected["digest"] and not HEX64.fullmatch(actual.get("sha256", "")):
            raise ScaffoldError(f"scaffold file digest is missing: {expected['path']}")


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
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


def build_receipt(
    contract_path: Path,
    verifier_path: Path,
    observed: dict[str, Any],
    machine_id_sha256: str,
    captured: dt.datetime,
) -> dict[str, Any]:
    if not HEX64.fullmatch(machine_id_sha256):
        raise ScaffoldError("machine-id digest is malformed")
    return {
        "schema": 1,
        "kind": "menhir-host-scaffold-receipt",
        "contract_sha256": sha256_file(contract_path),
        "verifier_sha256": sha256_file(verifier_path),
        "machine_id_sha256": machine_id_sha256,
        "captured_utc": iso(captured),
        "static": observed,
    }


def machine_id_digest() -> str:
    return sha256_file(Path("/etc/machine-id"))


def capture(contract_path: Path, receipt_path: Path) -> dict[str, Any]:
    require_root()
    require_safe_root_file(contract_path, "scaffold contract")
    contract = validate_contract(strict_load(contract_path))
    observed = observe_static(contract)
    assert_contract_matches(contract, observed)
    receipt = build_receipt(
        contract_path, Path(__file__).resolve(), observed,
        machine_id_digest(), utc_now(),
    )
    atomic_json(receipt_path, receipt)
    return receipt


def verify_static(contract_path: Path, receipt_path: Path) -> dict[str, Any]:
    require_root()
    require_safe_root_file(contract_path, "scaffold contract")
    require_safe_root_file(receipt_path, "scaffold receipt")
    contract = validate_contract(strict_load(contract_path))
    receipt = strict_load(receipt_path)
    if set(receipt) != RECEIPT_KEYS or receipt.get("schema") != 1 \
            or receipt.get("kind") != "menhir-host-scaffold-receipt":
        raise ScaffoldError("scaffold receipt schema mismatch")
    if receipt["contract_sha256"] != sha256_file(contract_path):
        raise ScaffoldError("scaffold receipt does not bind the installed contract")
    if receipt["verifier_sha256"] != sha256_file(Path(__file__).resolve()):
        raise ScaffoldError("scaffold receipt does not bind the installed verifier")
    if receipt["machine_id_sha256"] != machine_id_digest():
        raise ScaffoldError("scaffold receipt belongs to another host")
    observed = observe_static(contract)
    assert_contract_matches(contract, observed)
    if receipt["static"] != observed:
        raise ScaffoldError("live scaffold differs from the captured receipt")
    return {"contract": contract, "receipt": receipt, "observed": observed}


def age_hours(value: dt.datetime, now: dt.datetime) -> float:
    seconds = (now - value).total_seconds()
    if seconds < -60:
        raise ScaffoldError("evidence timestamp is in the future")
    return max(0.0, seconds / 3600)


def evaluate_evidence(
    policy: dict[str, int], evidence: dict[str, Any], now: dt.datetime,
) -> list[str]:
    failures: list[str] = []
    if evidence["encrypted_generations"] < policy["minimum_encrypted_generations"]:
        failures.append("insufficient encrypted backup generations")
    checks = (
        ("vps_backup_utc", "vps_backup_max_age_hours", "VPS backup"),
        ("desktop_archive_utc", "desktop_archive_max_age_hours", "desktop archive"),
        ("restore_drill_utc", "restore_drill_max_age_hours", "restore drill"),
    )
    for value_key, limit_key, label in checks:
        try:
            value = parse_time(evidence[value_key], label)
            if age_hours(value, now) > policy[limit_key]:
                failures.append(f"{label} is stale")
        except ScaffoldError as exc:
            failures.append(str(exc))
    if evidence.get("desktop_generation") not in evidence.get("retained_generations", []):
        failures.append("desktop archive generation is no longer retained on the VPS")
    if evidence.get("backup_generation") != evidence.get("drill_generation"):
        failures.append("restore drill is not bound to the current VPS generation")
    if evidence.get("maintenance_stage") not in (None, "complete"):
        failures.append("an unfinished maintenance transaction is active")
    if evidence.get("candidate_containers"):
        failures.append("candidate containers remain on the host")
    if not evidence.get("runtime_healthy"):
        failures.append("production runtime is not healthy and release-bound")
    if not evidence.get("public_ready"):
        failures.append("public readiness is not ready production mode")
    return failures


def inspect_runtime(contract: dict[str, Any]) -> tuple[bool, list[str]]:
    runtime = contract["runtime"]
    names = [runtime["app_container"], runtime["database_container"]]
    values = json.loads(run(["docker", "inspect", *names]))
    if not isinstance(values, list) or len(values) != 2:
        return False, []
    by_name = {item.get("Name", "").lstrip("/"): item for item in values}
    require_safe_root_file(RELEASE_PATH, "live release descriptor")
    release = strict_load(RELEASE_PATH)
    expected = {
        runtime["app_container"]: (runtime["app_service"], release["images"]["menhir"]),
        runtime["database_container"]: (runtime["database_service"], release["images"]["neo4j"]),
    }
    healthy = True
    for name, (service, digest) in expected.items():
        item = by_name.get(name, {})
        labels = item.get("Config", {}).get("Labels", {}) or {}
        configured_image = item.get("Config", {}).get("Image", "")
        healthy = healthy and all((
            item.get("State", {}).get("Running") is True,
            item.get("State", {}).get("Health", {}).get("Status") == "healthy",
            labels.get("com.docker.compose.project") == runtime["compose_project"],
            labels.get("com.docker.compose.service") == service,
            configured_image.endswith("@" + digest),
        ))
    all_names = run(["docker", "ps", "-a", "--format", "{{.Names}}"]).splitlines()
    candidates = sorted(name for name in all_names if name.startswith("menhir-candidate-"))
    return healthy, candidates


def public_ready(url: str) -> bool:
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Menhir-Scaffold/1", "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
    except Exception:
        return False
    return value.get("status") == "ready" and value.get("mode") == "production"


def operational_evidence(contract: dict[str, Any]) -> dict[str, Any]:
    require_safe_root_file(BACKUP_RECEIPT, "VPS backup receipt")
    require_safe_root_file(DESKTOP_RECEIPT, "desktop archive receipt")
    require_safe_root_file(DRILL_RECEIPT, "restore drill receipt")
    backup = strict_load(BACKUP_RECEIPT)
    desktop = strict_load(DESKTOP_RECEIPT)
    drill = strict_load(DRILL_RECEIPT)
    runtime_healthy, candidates = inspect_runtime(contract)
    stage = None
    if RELEASE_RUN.exists():
        stage = strict_load(RELEASE_RUN).get("stage")
    archives = list(Path("/srv/menhir/backups/encrypted").glob("*.tar.gz.age"))
    retained_generations = sorted({
        path.name.split("-", 1)[0]
        for path in archives
        if path.name.startswith("generation.") and "-" in path.name
    })
    return {
        "encrypted_generations": len(archives),
        "retained_generations": retained_generations,
        "vps_backup_utc": backup.get("checked_utc"),
        "desktop_archive_utc": desktop.get("archived_utc"),
        "restore_drill_utc": drill.get("checked_utc"),
        "backup_generation": backup.get("generation"),
        "desktop_generation": desktop.get("generation"),
        "drill_generation": drill.get("generation"),
        "maintenance_stage": stage,
        "candidate_containers": candidates,
        "runtime_healthy": runtime_healthy,
        "public_ready": public_ready(contract["runtime"]["public_ready_url"]),
    }


def verify_app_only(contract_path: Path, receipt_path: Path) -> dict[str, Any]:
    verified = verify_static(contract_path, receipt_path)
    evidence = operational_evidence(verified["contract"])
    failures = evaluate_evidence(verified["contract"]["backup_policy"], evidence, utc_now())
    if failures:
        raise ScaffoldError("app-only admission refused: " + "; ".join(failures))
    return {"static": "ok", "app_only": "admitted", "evidence": evidence}


def write_drill_receipt(backup: dict[str, Any], checked_utc: str, method: str) -> dict[str, Any]:
    value = {
        "schema": 1,
        "kind": "menhir-scaffold-restore-drill",
        "generation": backup.get("generation"),
        "backup_receipt_sha256": sha256_file(BACKUP_RECEIPT),
        "checked_utc": checked_utc,
        "recorded_utc": iso(utc_now()),
        "method": method,
    }
    if not isinstance(value["generation"], str) or not value["generation"].startswith("generation."):
        raise ScaffoldError("backup generation is invalid")
    parse_time(value["checked_utc"], "restore drill")
    atomic_json(DRILL_RECEIPT, value)
    return value


def seed_drill() -> dict[str, Any]:
    require_root()
    require_safe_root_file(BACKUP_RECEIPT, "VPS backup receipt")
    backup = strict_load(BACKUP_RECEIPT)
    if DRILL_RECEIPT.exists():
        require_safe_root_file(DRILL_RECEIPT, "restore drill receipt")
        current = strict_load(DRILL_RECEIPT)
        if current.get("generation") == backup.get("generation"):
            parse_time(current.get("checked_utc"), "restore drill")
            return current
    require_safe_root_file(STATUS_ROOT / "rehearsal-receipt.json", "rehearsal receipt")
    rehearsal = strict_load(STATUS_ROOT / "rehearsal-receipt.json")
    if backup.get("generation") != rehearsal.get("generation"):
        raise ScaffoldError("existing rehearsal is not bound to the current backup generation")
    return write_drill_receipt(
        backup, rehearsal.get("checked_utc"), "release-rehearsal-clean-load-and-consistency-check",
    )


def record_backup_drill() -> dict[str, Any]:
    require_root()
    require_safe_root_file(BACKUP_RECEIPT, "VPS backup receipt")
    backup = strict_load(BACKUP_RECEIPT)
    return write_drill_receipt(
        backup, backup.get("checked_utc"), "backup-generation-clean-load-and-consistency-check",
    )


def abandon_maintenance(contract_path: Path, receipt_path: Path, reason: str) -> dict[str, Any]:
    require_root()
    if not SAFE_REASON.fullmatch(reason):
        raise ScaffoldError("maintenance-abort reason is invalid")
    verified = verify_static(contract_path, receipt_path)
    if FIRST_MUTATION.exists():
        raise ScaffoldError("cannot abandon maintenance after first mutation")
    if not RELEASE_RUN.exists():
        raise ScaffoldError("there is no active maintenance transaction")
    state = strict_load(RELEASE_RUN)
    if state.get("stage") not in {"start", "backup", "staged", "rehearsal", "candidate", "accepted", "routed"}:
        raise ScaffoldError("maintenance transaction is not safely pre-mutation")
    healthy, candidates = inspect_runtime(verified["contract"])
    if not healthy or candidates or not public_ready(verified["contract"]["runtime"]["public_ready_url"]):
        raise ScaffoldError("healthy exact production without candidates is required before archival")
    release = strict_load(RELEASE_PATH)
    if state.get("release_id") != release.get("release_id"):
        raise ScaffoldError("maintenance state is not bound to the live release")
    timestamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    archive = STATUS_ROOT / "abandoned" / f"{timestamp}-{state['release_id']}"
    archive.mkdir(parents=True, mode=0o700)
    markers = [
        "release-run.json", "candidate-generation", "candidate-prestart-authority.json",
        "candidate-accept-receipt.json", "candidate-accepted", "restore-selection",
        "same-host-writer-fence-intent.json", "same-host-writer-fence.json",
    ]
    moved: dict[str, str] = {}
    for name in markers:
        source = STATUS_ROOT / name
        if source.exists():
            require_safe_root_file(source, name)
            digest = sha256_file(source)
            os.replace(source, archive / name)
            moved[name] = digest
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
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    result.add_argument("--receipt", type=Path, default=RECEIPT_PATH)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("capture")
    verify = commands.add_parser("verify")
    verify.add_argument("--app-only", action="store_true")
    commands.add_parser("status")
    commands.add_parser("seed-drill")
    commands.add_parser("record-backup-drill")
    abandon = commands.add_parser("abandon-maintenance")
    abandon.add_argument("--reason", required=True)
    return result


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
            evidence = operational_evidence(verified["contract"])
            value = {
                "static": "ok", "evidence": evidence,
                "app_only_failures": evaluate_evidence(
                    verified["contract"]["backup_policy"], evidence, utc_now(),
                ),
            }
        elif args.command == "seed-drill":
            value = seed_drill()
        elif args.command == "record-backup-drill":
            value = record_backup_drill()
        else:
            value = abandon_maintenance(args.contract, args.receipt, args.reason)
    except ScaffoldError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
