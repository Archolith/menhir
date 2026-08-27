#!/usr/bin/env python3
"""Sign one external prerequisite observation on an independent worker.

The private key stays on that worker. The emitted observation is later combined
with one from a distinct network and verified against release-pinned keys.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from lib.menhir_schema import (
    prerequisite_observation_payload,
    validate_release,
)

CHECKS = {
    "firewall", "proxied_dns", "full_strict", "hostname_aop",
    "external_scan", "console_recovery", "caddy_volume_permissions",
}
MAX_JSON_BYTES = 1024 * 1024
MAX_KEY_BYTES = 64 * 1024


def _read_regular(path: Path, label: str, limit: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SystemExit(f"cannot inspect {label}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"{label} must be a regular non-symlink file")
    if info.st_size <= 0 or info.st_size > limit:
        raise SystemExit(f"{label} has an invalid size")
    return path.read_bytes()


def _strict_json(raw: bytes, label: str):
    def hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=hook)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"invalid {label}: {exc}") from exc


def _safe_id(value: str, label: str, limit: int = 64) -> str:
    if len(value) > limit or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise SystemExit(f"{label} must be a safe bounded identifier")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release")
    parser.add_argument("private_key")
    parser.add_argument("worker_id")
    parser.add_argument("network_id")
    parser.add_argument("route_version")
    parser.add_argument("checks_json")
    args = parser.parse_args()
    release_path = Path(args.release)
    _read_regular(release_path, "release", MAX_JSON_BYTES)
    release = validate_release(str(release_path))
    checks_path = Path(args.checks_json)
    checks = _strict_json(
        _read_regular(checks_path, "checks_json", MAX_JSON_BYTES), "checks_json"
    )
    if set(checks) != CHECKS or any(checks[key] is not True for key in CHECKS):
        raise SystemExit("checks_json must contain the exact seven successful checks")
    worker_id = _safe_id(args.worker_id, "worker_id")
    network_id = _safe_id(args.network_id, "network_id")
    route_version = _safe_id(args.route_version, "route_version", 128)
    if worker_id not in release["external_evidence_public_keys"]:
        raise SystemExit("worker_id is not pinned by the release")
    receipt = {
        "release_id": release["release_id"],
        "release_manifest_sha256": hashlib.sha256(release_path.read_bytes()).hexdigest(),
    }
    observation = {
        "worker_id": worker_id,
        "network_id": network_id,
        "observed_utc": datetime.now(timezone.utc).isoformat(),
        "route_version": route_version,
        "checks": checks,
    }
    key_path = Path(args.private_key)
    key = serialization.load_pem_private_key(
        _read_regular(key_path, "private_key", MAX_KEY_BYTES), password=None
    )
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("private_key must be an Ed25519 PEM key")
    signature = key.sign(prerequisite_observation_payload(receipt, observation))
    observation["signature"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
