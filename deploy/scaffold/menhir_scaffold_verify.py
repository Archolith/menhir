"""Contract validation and static host observation for the Menhir scaffold."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

try:  # The verifier runs on Linux; this keeps pure policy tests importable on Windows.
    import grp
    import pwd
except ModuleNotFoundError:  # pragma: no cover - exercised only by non-POSIX test hosts
    grp = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

from menhir_scaffold_constants import (
    CONTRACT_KEYS, ENCRYPTED_BACKUP_ROOT, HEX64, RECEIPT_KEYS,
)
from menhir_scaffold_core import (
    ScaffoldError, atomic_json, build_receipt, machine_id_digest, read_os_release,
    require_root, require_safe_root_file, run, sha256_file, stat_row, strict_load,
    utc_now,
)


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


def verify_encrypted_archives(backup: dict[str, Any]) -> list[str]:
    """Rehash the distinct encrypted generations bound by the backup receipt."""
    local = backup.get("local_encrypted_archives")
    archives = local.get("archives") if isinstance(local, dict) else None
    if not isinstance(archives, list) or not archives:
        raise ScaffoldError("backup receipt contains no encrypted archive evidence")
    generations: set[str] = set()
    for row in archives:
        if not isinstance(row, dict):
            raise ScaffoldError("backup receipt archive entry is invalid")
        generation = row.get("generation")
        path_value = row.get("path")
        if not isinstance(generation, str) or not isinstance(path_value, str):
            raise ScaffoldError("backup receipt archive identity is invalid")
        path = Path(path_value)
        if path.parent != ENCRYPTED_BACKUP_ROOT:
            raise ScaffoldError("encrypted archive is outside the fixed backup root")
        try:
            info = path.lstat()
        except OSError as exc:
            raise ScaffoldError(f"encrypted archive is missing: {path}") from exc
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ScaffoldError("encrypted archive must be a regular non-symlink file")
        if info.st_size != row.get("size") or sha256_file(path) != row.get("sha256"):
            raise ScaffoldError("encrypted archive differs from its backup receipt")
        generations.add(generation)
    if local.get("retained_generation_count") != len(generations):
        raise ScaffoldError("retained generation count is not distinct receipt evidence")
    return sorted(generations)


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
