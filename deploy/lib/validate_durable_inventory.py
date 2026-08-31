#!/usr/bin/env python3
"""Validate Menhir's exhaustive production durable-state census."""
from __future__ import annotations
import json
import os
import posixpath
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EXPECTED = {
    "state/neo4j/data": ("neo4j", "neo4j-offline-dump-all-databases", "neo4j-admin-load-and-check", ("neo4j",)),
    "state/oauth": ("state/oauth", "sqlite-backup-all-databases", "sqlite-integrity-and-copy", ("menhir",)),
    "state/telemetry": ("state/telemetry", "sqlite-backup-all-databases", "sqlite-integrity-and-copy", ("menhir",)),
    "secrets": ("secrets", "exact-tree-copy", "exact-tree-copy-and-permission-map", ()),
    "policy": ("policy", "exact-tree-copy", "exact-tree-copy", ()),
    "release/release.json": ("config/release.json", "exact-file-copy", "release-authority-validation", ()),
}
EXCLUDED = {
    "state/neo4j/logs": "operational logs are non-authoritative and independently rotated",
    "runtime/menhir/tmp/logs/server.access.log": (
        "container-local access log is non-authoritative and discarded with the container"
    ),
    "runtime/menhir/tmp/logs/server.err.log": (
        "container-local error log is non-authoritative and discarded with the container"
    ),
    "runtime/menhir/tmp/logs/server.log": (
        "container-local server log is non-authoritative and discarded with the container"
    ),
}
EXPECTED_EPHEMERAL_WRITES = {
    "menhir": frozenset({
        "/tmp/logs/server.access.log",
        "/tmp/logs/server.err.log",
        "/tmp/logs/server.log",
    }),
    "neo4j": frozenset(),
}
COMPOSE_MOUNTS = {
    "${MENHIR_STATE_ROOT:-/srv/menhir/production/state}/neo4j/data",
    "${MENHIR_STATE_ROOT:-/srv/menhir/production/state}/neo4j/logs",
    "${MENHIR_PROD_SECRETS_DIR:-/srv/menhir/production/secrets}/menhir",
    "${MENHIR_PROD_SECRETS_DIR:-/srv/menhir/production/secrets}/oauth",
    "${MENHIR_PROD_SECRETS_DIR:-/srv/menhir/production/secrets}/neo4j/neo4j-auth",
    "${MENHIR_PROD_POLICY_DIR:-/srv/menhir/production/policy}",
    "${MENHIR_STATE_ROOT:-/srv/menhir/production/state}/oauth",
    "${MENHIR_TELEMETRY_ROOT:-/srv/menhir/production/state/telemetry}",
}

def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out

def validate(inventory_path: Path, compose_path: Path) -> None:
    with inventory_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_pairs)
    if set(value) != {"schema", "authorities", "excluded"} or value["schema"] != 1:
        raise ValueError("durable inventory top-level schema is invalid")
    found = {}
    for row in value["authorities"]:
        if not isinstance(row, dict) or set(row) != {"source", "backup_path", "capture", "restore", "writers"}:
            raise ValueError("durable inventory authority entry is invalid")
        if row["source"] in found:
            raise ValueError(f"duplicate durable authority source: {row['source']}")
        writers = row["writers"]
        if not isinstance(writers, list) or len(writers) != len(set(writers)) \
                or any(writer not in {"menhir", "neo4j"} for writer in writers):
            raise ValueError("durable inventory writer census is invalid")
        found[row["source"]] = (
            row["backup_path"], row["capture"], row["restore"], tuple(writers)
        )
    if found != EXPECTED:
        raise ValueError("durable authority census differs from the production contract")
    excluded = value["excluded"]
    excluded_map = {row["path"]: row["reason"] for row in excluded
                    if isinstance(row, dict) and set(row) == {"path", "reason"}}
    if len(excluded_map) != len(excluded) or excluded_map != EXCLUDED:
        raise ValueError("durable inventory exclusions differ from the production contract")
    compose = compose_path.read_text(encoding="utf-8")
    actual = {line.strip().split(": ", 1)[1] for line in compose.splitlines()
              if line.strip().startswith(("source: ${", "file: ${"))}
    if actual != COMPOSE_MOUNTS:
        raise ValueError("production Compose persistent bind set changed; update durable inventory")


def reconcile_live(live: dict) -> dict:
    """Reconcile independently observed containers, mounts, and open files."""
    if not isinstance(live, dict) or set(live) != {"services", "mounts", "open_files"}:
        raise ValueError("live durable census schema is invalid")
    if set(live["services"]) != {"menhir", "neo4j"}:
        raise ValueError("live writer service set differs from production contract")
    for service, identity in live["services"].items():
        if not isinstance(identity, dict) or set(identity) != {"container_id", "pid"} \
                or not isinstance(identity["container_id"], str) \
                or not identity["container_id"] \
                or not isinstance(identity["pid"], int) or identity["pid"] <= 0:
            raise ValueError("live writer service identity is invalid: %s" % service)
    mount_rows = live["mounts"]
    if not isinstance(mount_rows, list):
        raise ValueError("live mount census must be a list")
    mounts = {}
    for row in mount_rows:
        if not isinstance(row, dict) or set(row) != {"service", "source", "destination", "rw"}:
            raise ValueError("live mount row is invalid")
        if row["service"] not in {"menhir", "neo4j"} \
                or not isinstance(row["source"], str) or not row["source"].startswith("/") \
                or not isinstance(row["destination"], str) \
                or not row["destination"].startswith("/"):
            raise ValueError("live mount row has invalid service/path identity")
        key = (row["service"], posixpath.normpath(row["source"]))
        if key in mounts or not isinstance(row["rw"], bool):
            raise ValueError("live mount census contains a duplicate/invalid row")
        mounts[key] = row
    expected_rw = {
        ("neo4j", "/srv/menhir/production/state/neo4j/data"),
        ("neo4j", "/srv/menhir/production/state/neo4j/logs"),
        ("menhir", "/srv/menhir/production/state/oauth"),
        ("menhir", "/srv/menhir/production/state/telemetry"),
    }
    expected_ro = {
        ("neo4j", "/srv/menhir/production/secrets/neo4j/neo4j-auth"),
        ("menhir", "/srv/menhir/production/secrets/menhir"),
        ("menhir", "/srv/menhir/production/secrets/oauth"),
        ("menhir", "/srv/menhir/production/policy"),
    }
    if set(mounts) != expected_rw | expected_ro:
        raise ValueError("live mount set differs from the complete production census")
    for key in expected_rw:
        if mounts[key]["rw"] is not True:
            raise ValueError("declared writer mount is not writable: %s" % (key,))
    for key in expected_ro:
        if mounts[key]["rw"] is not False:
            raise ValueError("non-writer authority mount is writable: %s" % (key,))
    open_files = live["open_files"]
    if not isinstance(open_files, dict) or set(open_files) != {"menhir", "neo4j"}:
        raise ValueError("live open-file writer census is invalid")
    authority_destinations = {
        service: tuple(posixpath.normpath(row["destination"])
                       for row in mount_rows if row["service"] == service)
        for service in ("menhir", "neo4j")
    }
    for service in ("menhir", "neo4j"):
        values = open_files.get(service)
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("live open-file rows are invalid for %s" % service)
        normalized = [posixpath.normpath(v.removesuffix(" (deleted)")) for v in values]
        for value in normalized:
            if not any(value == root or value.startswith(root + "/")
                       for root in authority_destinations[service]) \
                    and value not in EXPECTED_EPHEMERAL_WRITES[service]:
                raise ValueError("writer %s has an open file outside declared mounts: %s"
                                 % (service, value))
    return live


def _run_json(command: list[str]):
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return json.loads(output, object_pairs_hook=_pairs)


def collect_live(compose_path: Path, env_path: Path) -> dict:
    base = ["docker", "compose", "--project-name", "menhir-prod", "--env-file",
            str(env_path), "--file", str(compose_path)]
    services = {}
    mounts = []
    open_files = {}
    for service in ("menhir", "neo4j"):
        cid = subprocess.run(
            base + ["ps", "-q", service], check=True, capture_output=True, text=True
        ).stdout.strip()
        if not cid:
            raise ValueError("production writer container is not running: %s" % service)
        inspected = _run_json(["docker", "inspect", cid])[0]
        pid = inspected.get("State", {}).get("Pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("production writer PID is unavailable: %s" % service)
        services[service] = {"container_id": cid, "pid": pid}
        for mount in inspected.get("Mounts", []):
            source = mount.get("Source")
            destination = mount.get("Destination")
            if isinstance(source, str) and isinstance(destination, str):
                mounts.append({
                    "service": service,
                    "source": source,
                    "destination": destination,
                    "rw": bool(mount.get("RW")),
                })
        observed = []
        fd_root = Path("/proc") / str(pid) / "fd"
        for descriptor in fd_root.iterdir():
            try:
                target = os.readlink(descriptor)
                target_path = target.removesuffix(" (deleted)")
                if not target_path.startswith("/"):
                    continue
                host_view = Path("/proc") / str(pid) / "root" / target_path.lstrip("/")
                if not stat.S_ISREG(host_view.stat().st_mode):
                    continue
                flags_path = Path("/proc") / str(pid) / "fdinfo" / descriptor.name
                flags_line = next(
                    (line for line in flags_path.read_text(encoding="ascii").splitlines()
                     if line.startswith("flags:")),
                    "",
                )
                if not flags_line:
                    continue
                flags = int(flags_line.split()[1], 8)
                if flags & os.O_ACCMODE == os.O_RDONLY:
                    continue
            except OSError:
                continue
            observed.append(target)
        open_files[service] = sorted(set(observed))
    return {"services": services, "mounts": mounts, "open_files": open_files}


def write_report(path: Path, live: dict) -> None:
    value = {
        "schema": 1,
        "kind": "durable-live-census",
        "checked_utc": datetime.now(timezone.utc).isoformat(),
        "live": live,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".durable-census-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o400); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def main(argv: list[str]) -> int:
    if len(argv) not in {3, 6}:
        print("usage: validate_durable_inventory.py <inventory.json> <compose.yml> [--live ENV_FILE REPORT]", file=sys.stderr)
        return 2
    try:
        validate(Path(argv[1]), Path(argv[2]))
        if len(argv) == 6:
            if argv[3] != "--live":
                raise ValueError("unknown durable census option")
            live = collect_live(Path(argv[2]), Path(argv[4]))
            reconcile_live(live)
            write_report(Path(argv[5]), live)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"durable inventory validation failed: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
