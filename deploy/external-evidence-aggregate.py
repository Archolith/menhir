#!/usr/bin/env python3
"""Aggregate independently signed public-network observations."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.menhir_schema import validate_prerequisite_binding, validate_release

OBSERVATION_KEYS = {
    "worker_id", "network_id", "observed_utc", "route_version", "checks", "signature",
}
CHECK_KEYS = {
    "firewall", "proxied_dns", "full_strict", "hostname_aop",
    "external_scan", "console_recovery", "caddy_volume_permissions",
}
MAX_JSON_BYTES = 1024 * 1024


def read_regular(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SystemExit(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"{label} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > MAX_JSON_BYTES:
        raise SystemExit(f"{label} has an invalid size")
    return path.read_bytes()


def strict_json(path: Path, label: str):
    def hook(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(
            read_regular(path, label).decode("utf-8"), object_pairs_hook=hook
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"invalid {label}: {exc}") from exc


def safe_id(value: object, label: str, limit: int = 64) -> str:
    if not isinstance(value, str) or len(value) > limit or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", value
    ):
        raise SystemExit(f"{label} must be a safe bounded identifier")
    return value


def fresh(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise SystemExit(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SystemExit(f"{label} must include a timezone")
    now = datetime.now(timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed > now + timedelta(seconds=60) or parsed < now - timedelta(minutes=15):
        raise SystemExit(f"{label} is outside the accepted freshness window")


def atomic_write(output: Path, payload: bytes) -> None:
    if os.path.lexists(output):
        raise SystemExit("output already exists or is a symlink")
    parent = output.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise SystemExit(f"cannot inspect output directory: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit("output directory must be a real directory")
    fd, temporary = tempfile.mkstemp(prefix=".external-evidence-", dir=parent)
    descriptor_open = True
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            descriptor_open = False
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if os.path.lexists(output):
            raise SystemExit("output appeared during aggregation")
        os.replace(temporary, output)
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor_open:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release")
    parser.add_argument("route_version")
    parser.add_argument("output")
    parser.add_argument("observations", nargs="+")
    args = parser.parse_args()
    if len(args.observations) < 2:
        raise SystemExit("at least two observation files are required")
    release_path = Path(args.release)
    read_regular(release_path, "release")
    release = validate_release(str(release_path))
    route_version = safe_id(args.route_version, "route_version", 128)
    workers: set[str] = set()
    networks: set[str] = set()
    observations = []
    for index, name in enumerate(args.observations):
        observation = strict_json(Path(name), f"observation[{index}]")
        if not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS:
            raise SystemExit("observation has unexpected or missing keys")
        worker = safe_id(observation.get("worker_id"), "worker_id")
        network = safe_id(observation.get("network_id"), "network_id")
        if worker in workers or network in networks:
            raise SystemExit("workers and networks must be distinct")
        if worker not in release["external_evidence_public_keys"]:
            raise SystemExit("observation worker is not release-pinned")
        if observation.get("route_version") != route_version:
            raise SystemExit("observation route_version mismatch")
        fresh(observation.get("observed_utc"), "observed_utc")
        checks = observation.get("checks")
        if not isinstance(checks, dict) or set(checks) != CHECK_KEYS or any(
            checks[key] is not True for key in CHECK_KEYS
        ):
            raise SystemExit("observation checks must be the exact seven true booleans")
        if not isinstance(observation.get("signature"), str):
            raise SystemExit("observation signature must be a string")
        workers.add(worker)
        networks.add(network)
        observations.append(observation)

    receipt = {
        "schema": 1,
        "kind": "external-prerequisite",
        "release_id": release["release_id"],
        "release_manifest_sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "route_version": route_version,
        "observations": observations,
    }
    output = Path(args.output)
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    atomic_write(output, payload)
    try:
        validate_prerequisite_binding(str(output), str(release_path))
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
