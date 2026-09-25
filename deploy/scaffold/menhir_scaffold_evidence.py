"""Audit evidence evaluation and runtime observation for the Menhir scaffold."""

from __future__ import annotations

import datetime as dt
import json
import urllib.request
from typing import Any

from menhir_scaffold_constants import (
    BACKUP_RECEIPT, DRILL_RECEIPT, RELEASE_PATH, SCHEMA_PATH,
)
from menhir_scaffold_core import (
    ScaffoldError, age_hours, atomic_json, iso, parse_time, require_safe_root_file,
    run, sha256_file, strict_load, utc_now,
)


def evaluate_evidence(
    policy: dict[str, int], evidence: dict[str, Any], now: dt.datetime,
) -> list[str]:
    failures: list[str] = []
    distinct_generations = {
        value for value in evidence.get("retained_generations", [])
        if isinstance(value, str) and value.startswith("generation.")
    }
    if len(distinct_generations) < policy["minimum_encrypted_generations"]:
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
    if evidence.get("app_only_stage") not in (None, "complete"):
        failures.append("an unfinished app-only transaction is active")
    if evidence.get("security_config_stage") not in (None, "complete"):
        failures.append("an unfinished security-config transaction is active")
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
