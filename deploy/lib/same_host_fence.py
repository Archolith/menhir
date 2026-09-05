#!/usr/bin/env python3
"""Crash-safe same-host Docker writer-fence authority for Menhir releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import menhir_schema


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DEPLOYMENT = {
    "topology": "same-host-docker",
    "legacy_container": "menhir-prod-app",
    "production_container": "menhir-prod-app",
    "candidate_container": "menhir-candidate-app",
    "legacy_database_container": "menhir-prod-neo4j",
    "candidate_database_container": "menhir-candidate-neo4j",
    "compose_project": "menhir-prod",
    "compose_service": "menhir",
}


def _strict(path: str | Path) -> dict:
    return menhir_schema.load_strict(str(path))


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _host_id() -> str:
    path = Path("/etc/machine-id")
    if not path.is_file() or path.is_symlink():
        raise ValueError("/etc/machine-id must be a regular non-symlink file")
    value = path.read_text(encoding="ascii").strip()
    if not value:
        raise ValueError("/etc/machine-id is empty")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _env(container: dict) -> dict[str, str]:
    rows = container.get("Config", {}).get("Env") or []
    result = {}
    for row in rows:
        if isinstance(row, str) and "=" in row:
            key, value = row.split("=", 1)
            result[key] = value
    return result


def _name(container: dict) -> str:
    return str(container.get("Name", "")).lstrip("/")


def _labels(container: dict) -> dict:
    value = container.get("Config", {}).get("Labels") or {}
    return value if isinstance(value, dict) else {}


def _container_id(container: dict) -> str:
    value = container.get("Id")
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise ValueError("Docker inspect did not return a full 64-hex container id")
    return value


def _image_id(container: dict) -> str:
    value = container.get("Image")
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("Docker inspect did not return a digest-shaped image id")
    return value


def _release(path: str | Path) -> dict:
    release = menhir_schema.validate_release(str(path))
    if release.get("deployment") != _DEPLOYMENT:
        raise ValueError("release deployment authority is not the reviewed same-host Docker topology")
    return release


def _write_atomic(path: str | Path, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(container: dict, *, expected_name: str, expected_service: str) -> dict:
    if _name(container) != expected_name:
        raise ValueError(f"legacy container name is not {expected_name}")
    labels = _labels(container)
    if labels.get("com.docker.compose.project") != _DEPLOYMENT["compose_project"]:
        raise ValueError(f"{expected_name} is not owned by Compose project menhir-prod")
    if labels.get("com.docker.compose.service") != expected_service:
        raise ValueError(f"{expected_name} is not Compose service {expected_service}")
    state = container.get("State") or {}
    if state.get("Running") is not True:
        raise ValueError(f"{expected_name} is not running; refusing to guess its identity")
    restart = (container.get("HostConfig") or {}).get("RestartPolicy") or {}
    networks = sorted(((container.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    mounts = sorted(
        str(row.get("Source")) for row in (container.get("Mounts") or [])
        if isinstance(row, dict) and row.get("Source")
    )
    return {
        "container_id": _container_id(container),
        "container_name": _name(container),
        "image_id": _image_id(container),
        "image_ref": str((container.get("Config") or {}).get("Image", "")),
        "compose_project": labels["com.docker.compose.project"],
        "compose_service": labels["com.docker.compose.service"],
        "restart_policy": str(restart.get("Name", "")),
        "networks": networks,
        "mount_sources": mounts,
    }


def capture_intent(release_path: str, inspect_path: str, output_path: str) -> dict:
    release = _release(release_path)
    rows = _strict(inspect_path)
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError("Docker inspect must contain the exact legacy app and database containers")
    by_name = {_name(row): row for row in rows if isinstance(row, dict)}
    app = by_name.get(_DEPLOYMENT["legacy_container"])
    database = by_name.get(_DEPLOYMENT["legacy_database_container"])
    if app is None or database is None or len(by_name) != 2:
        raise ValueError("Docker inspect does not contain the exact legacy app and database pair")
    if _env(app).get("MENHIR_RUNTIME_MODE") != "production":
        raise ValueError("legacy container does not identify itself as a production writer")
    value = {
        "schema": 1,
        "kind": "same-host-writer-fence-intent",
        "release_id": release["release_id"],
        "release_manifest_sha256": _sha(release_path),
        "host_machine_id_sha256": _host_id(),
        "legacy": {
            "app": _identity(app, expected_name=_DEPLOYMENT["legacy_container"], expected_service="menhir"),
            "database": _identity(database, expected_name=_DEPLOYMENT["legacy_database_container"], expected_service="neo4j"),
        },
        "captured_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_atomic(output_path, value)
    return value


def validate_intent(value: dict, release_path: str) -> dict:
    expected = {
        "schema", "kind", "release_id", "release_manifest_sha256",
        "host_machine_id_sha256", "legacy", "captured_utc",
    }
    if set(value) != expected or value.get("schema") != 1 \
            or value.get("kind") != "same-host-writer-fence-intent":
        raise ValueError("same-host writer-fence intent schema mismatch")
    release = _release(release_path)
    if value["release_id"] != release["release_id"] \
            or value["release_manifest_sha256"] != _sha(release_path):
        raise ValueError("writer-fence intent is bound to a different release")
    if value["host_machine_id_sha256"] != _host_id():
        raise ValueError("writer-fence intent was captured on a different host")
    legacy = value.get("legacy")
    if not isinstance(legacy, dict) or set(legacy) != {"app", "database"}:
        raise ValueError("writer-fence intent must bind the legacy app and database")
    keys = {
        "container_id", "container_name", "image_id", "image_ref",
        "compose_project", "compose_service", "restart_policy", "networks",
        "mount_sources",
    }
    for role, expected_name, expected_service in (
        ("app", _DEPLOYMENT["legacy_container"], "menhir"),
        ("database", _DEPLOYMENT["legacy_database_container"], "neo4j"),
    ):
        item = legacy[role]
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError(f"writer-fence intent legacy {role} identity schema mismatch")
        if not _HEX64.fullmatch(str(item.get("container_id", ""))) \
                or not _DIGEST.fullmatch(str(item.get("image_id", ""))):
            raise ValueError("writer-fence intent contains malformed Docker identities")
        for key, expected_value in (
            ("container_name", expected_name),
            ("compose_project", _DEPLOYMENT["compose_project"]),
            ("compose_service", expected_service),
        ):
            if item.get(key) != expected_value:
                raise ValueError(f"writer-fence intent legacy {role} has unexpected {key}")
        for list_key in ("networks", "mount_sources"):
            if not isinstance(item.get(list_key), list) \
                    or any(not isinstance(row, str) or not row for row in item[list_key]):
                raise ValueError(f"writer-fence intent legacy {role} {list_key} are malformed")
    return value


def _validate_census(containers: list, intent: dict, release: dict,
                     *, allow_production: bool = False) -> None:
    if not isinstance(containers, list):
        raise ValueError("Docker census must be a JSON list")
    old_ids = {item["container_id"] for item in intent["legacy"].values()}
    old_names = {item["container_name"] for item in intent["legacy"].values()}
    old_app_image = intent["legacy"]["app"]["image_id"]
    old_database_image = intent["legacy"]["database"]["image_id"]
    old_database_mounts = set(intent["legacy"]["database"]["mount_sources"])
    production_seen: set[str] = set()
    for container in containers:
        if not isinstance(container, dict):
            raise ValueError("Docker census contains a non-object entry")
        cid = _container_id(container)
        name = _name(container)
        labels = _labels(container)
        env = _env(container)
        state = container.get("State") or {}
        running = state.get("Running") is True
        image_ref = str((container.get("Config") or {}).get("Image", ""))
        mount_sources = {
            str(row.get("Source")) for row in (container.get("Mounts") or [])
            if isinstance(row, dict) and row.get("Source")
        }
        candidate_fixed_mounts = {
            "/srv/menhir/production/secrets/menhir",
            "/srv/menhir/production/secrets/oauth",
            "/srv/menhir/production/policy",
            "/srv/menhir/production/state/oauth",
        }
        candidate_probe_mounts = mount_sources - candidate_fixed_mounts
        candidate_mounts_exact = (
            len(candidate_probe_mounts) == 1
            and re.fullmatch(
                r"/srv/menhir/backups/candidate/generation\.[A-Za-z0-9]+/probe-output/telemetry",
                next(iter(candidate_probe_mounts)),
            ) is not None
        )
        candidate = (
            name == _DEPLOYMENT["candidate_container"]
            and labels.get("com.docker.compose.project") == "menhir-candidate"
            and labels.get("com.docker.compose.service") == "menhir"
            and env.get("MENHIR_RUNTIME_MODE") == "candidate-readonly"
            and env.get("MENHIR_STARTUP_SCOPE") == "production"
            and image_ref.endswith("@" + release["images"]["menhir"])
            and candidate_fixed_mounts <= mount_sources
            and candidate_mounts_exact
        )
        candidate_database = (
            name == _DEPLOYMENT["candidate_database_container"]
            and labels.get("com.docker.compose.project") == "menhir-candidate"
            and labels.get("com.docker.compose.service") == "neo4j"
            and image_ref.endswith("@" + release["images"]["neo4j"])
            and mount_sources == {
                "/srv/menhir/production/state/neo4j/data",
                "/srv/menhir/production/state/neo4j/logs",
                "/srv/menhir/production/secrets/neo4j/neo4j-auth",
            }
        )
        candidate_claim = (
            name in {_DEPLOYMENT["candidate_container"], _DEPLOYMENT["candidate_database_container"]}
            or (labels.get("com.docker.compose.project") == "menhir-candidate"
                and labels.get("com.docker.compose.service") in {"menhir", "neo4j"})
        )
        if candidate_claim and not (candidate or candidate_database):
            raise ValueError("candidate identity, image, mode, or authority mounts differ from the reviewed release")
        if cid in old_ids or name in old_names:
            # The reviewed replacement intentionally reuses the stable production
            # names.  Identity, image, labels, and runtime mode must all prove it
            # is the new target before those names are allowed back into service.
            expected_role = "app" if name == _DEPLOYMENT["production_container"] else "database"
            expected_service = "menhir" if expected_role == "app" else "neo4j"
            expected_image = release["images"]["menhir" if expected_role == "app" else "neo4j"]
            is_reviewed_production = (
                allow_production
                and cid not in old_ids
                and labels.get("com.docker.compose.project") == _DEPLOYMENT["compose_project"]
                and labels.get("com.docker.compose.service") == expected_service
                and image_ref.endswith("@" + expected_image)
                and (expected_role != "app" or (
                    env.get("MENHIR_RUNTIME_MODE") == "production"
                    and env.get("MENHIR_STARTUP_SCOPE") == "production"
                ))
                and running
            )
            expected_mounts = ({
                "/srv/menhir/production/secrets/menhir",
                "/srv/menhir/production/secrets/oauth",
                "/srv/menhir/production/policy",
                "/srv/menhir/production/state/oauth",
                "/srv/menhir/production/state/telemetry",
            } if expected_role == "app" else {
                "/srv/menhir/production/state/neo4j/data",
                "/srv/menhir/production/state/neo4j/logs",
                "/srv/menhir/production/secrets/neo4j/neo4j-auth",
            })
            is_reviewed_production = is_reviewed_production and mount_sources == expected_mounts
            if not is_reviewed_production:
                raise ValueError("legacy identity or unreviewed replacement still exists; stop, disable, and remove it")
            production_seen.add(expected_role)
            continue
        if candidate or candidate_database:
            continue
        same_service = (
            labels.get("com.docker.compose.project") == _DEPLOYMENT["compose_project"]
            and labels.get("com.docker.compose.service") in {"menhir", "neo4j"}
        )
        production_mode = env.get("MENHIR_RUNTIME_MODE") == "production"
        old_image_writer = _image_id(container) == old_app_image and env.get("MENHIR_STARTUP_SCOPE") == "production"
        old_database_writer = _image_id(container) == old_database_image and bool(old_database_mounts & mount_sources)
        if same_service or production_mode or old_image_writer or old_database_writer:
            detail = f"name={name or '<unnamed>'} id={cid[:12]} running={str(running).lower()}"
            raise ValueError(f"competing Menhir writer-capable container remains: {detail}")
    if allow_production and production_seen != {"app", "database"}:
        raise ValueError("reviewed production app/database pair is not running")


def finalize(release_path: str, intent_path: str, census_path: str, output_path: str) -> dict:
    intent = validate_intent(_strict(intent_path), release_path)
    release = _release(release_path)
    _validate_census(_strict(census_path), intent, release)
    value = {
        "schema": 1,
        "kind": "same-host-writer-fence",
        "release_id": intent["release_id"],
        "release_manifest_sha256": intent["release_manifest_sha256"],
        "host_machine_id_sha256": intent["host_machine_id_sha256"],
        "legacy": intent["legacy"],
        "legacy_container_removed": True,
        "competing_writer_census": "clear",
        "checked_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_atomic(output_path, value)
    return value


def verify(release_path: str, receipt_path: str, census_path: str,
           *, allow_production: bool = False) -> dict:
    receipt = _strict(receipt_path)
    expected = {
        "schema", "kind", "release_id", "release_manifest_sha256",
        "host_machine_id_sha256", "legacy", "legacy_container_removed",
        "competing_writer_census", "checked_utc",
    }
    if set(receipt) != expected or receipt.get("schema") != 1 \
            or receipt.get("kind") != "same-host-writer-fence":
        raise ValueError("same-host writer-fence receipt schema mismatch")
    intent = {
        "schema": 1,
        "kind": "same-host-writer-fence-intent",
        "release_id": receipt["release_id"],
        "release_manifest_sha256": receipt["release_manifest_sha256"],
        "host_machine_id_sha256": receipt["host_machine_id_sha256"],
        "legacy": receipt["legacy"],
        "captured_utc": receipt["checked_utc"],
    }
    validate_intent(intent, release_path)
    release = _release(release_path)
    if receipt.get("legacy_container_removed") is not True \
            or receipt.get("competing_writer_census") != "clear":
        raise ValueError("same-host writer-fence receipt does not assert a closed legacy writer")
    _validate_census(_strict(census_path), intent, release,
                     allow_production=allow_production)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture-intent")
    capture.add_argument("release")
    capture.add_argument("inspect")
    capture.add_argument("output")
    finish = sub.add_parser("finalize")
    finish.add_argument("release")
    finish.add_argument("intent")
    finish.add_argument("census")
    finish.add_argument("output")
    check = sub.add_parser("verify")
    check.add_argument("release")
    check.add_argument("receipt")
    check.add_argument("census")
    check.add_argument("--allow-production", action="store_true")
    args = parser.parse_args()
    if args.command == "capture-intent":
        capture_intent(args.release, args.inspect, args.output)
    elif args.command == "finalize":
        finalize(args.release, args.intent, args.census, args.output)
    else:
        verify(args.release, args.receipt, args.census,
               allow_production=args.allow_production)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
