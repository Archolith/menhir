from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scaffold" / "menhir_scaffold.py"
SPEC = importlib.util.spec_from_file_location("menhir_scaffold", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
scaffold = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scaffold)


def contract() -> dict:
    return {
        "schema": 1,
        "kind": "menhir-host-scaffold-contract",
        "host": {"os_id": "ubuntu", "os_version": "24.04"},
        "directories": [{"path": "/srv/x", "uid": 0, "gid": 0, "mode": "0755"}],
        "files": [{"path": "/etc/x", "uid": 0, "gid": 0, "mode": "0400", "digest": False}],
        "identities": [{"name": "svc", "uid": 1, "gid": 1, "home": "/x", "shell": "/bin/false"}],
        "groups": [{"name": "ops", "gid": 2, "members": ["svc"]}],
        "network": {"name": "n", "driver": "bridge", "subnet": "10.0.0.0/24", "gateway": "10.0.0.1"},
        "units": [{"name": "x.service", "enabled": "enabled", "active": "active"}],
        "backup_policy": {
            "minimum_encrypted_generations": 2,
            "vps_backup_max_age_hours": 24,
            "desktop_archive_max_age_hours": 24,
            "restore_drill_max_age_hours": 168,
        },
        "runtime": {
            "app_container": "app", "database_container": "db",
            "compose_project": "p", "app_service": "app",
            "database_service": "db", "public_ready_url": "https://example.test/readyz",
        },
    }


def valid_evidence(now: dt.datetime) -> dict:
    stamp = now.isoformat()
    return {
        "encrypted_generations": 2,
        "vps_backup_utc": stamp,
        "desktop_archive_utc": stamp,
        "restore_drill_utc": stamp,
        "backup_generation": "generation.a",
        "desktop_generation": "generation.a",
        "drill_generation": "generation.a",
        "retained_generations": ["generation.a", "generation.previous"],
        "maintenance_stage": None,
        "candidate_containers": [],
        "runtime_healthy": True,
        "public_ready": True,
    }


def test_contract_rejects_duplicate_paths() -> None:
    value = contract()
    value["files"][0]["path"] = value["directories"][0]["path"]
    with pytest.raises(scaffold.ScaffoldError, match="unique safe absolute"):
        scaffold.validate_contract(value)


def test_strict_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "value.json"
    path.write_text('{"schema":1,"schema":1}', encoding="ascii")
    with pytest.raises(scaffold.ScaffoldError, match="duplicate JSON key"):
        scaffold.strict_load(path)


def test_fresh_operational_evidence_is_admitted() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    failures = scaffold.evaluate_evidence(contract()["backup_policy"], valid_evidence(now), now)
    assert failures == []


def test_stale_and_mismatched_evidence_is_refused() -> None:
    now = dt.datetime(2026, 9, 2, 12, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now - dt.timedelta(hours=25))
    evidence["desktop_generation"] = "generation.missing"
    evidence["maintenance_stage"] = "candidate"
    failures = scaffold.evaluate_evidence(contract()["backup_policy"], evidence, now)
    assert "VPS backup is stale" in failures
    assert "desktop archive is stale" in failures
    assert "desktop archive generation is no longer retained on the VPS" in failures
    assert "an unfinished maintenance transaction is active" in failures


def test_missing_runtime_and_candidate_are_refused() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now)
    evidence["runtime_healthy"] = False
    evidence["candidate_containers"] = ["menhir-candidate-app"]
    evidence["public_ready"] = False
    failures = scaffold.evaluate_evidence(contract()["backup_policy"], evidence, now)
    assert "candidate containers remain on the host" in failures
    assert "production runtime is not healthy and release-bound" in failures
    assert "public readiness is not ready production mode" in failures


def test_fresh_retained_desktop_archive_can_lag_latest_backup() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now)
    evidence["backup_generation"] = "generation.new"
    evidence["drill_generation"] = "generation.new"
    assert scaffold.evaluate_evidence(contract()["backup_policy"], evidence, now) == []


def test_stat_row_rejects_wrong_path_type(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_text("x", encoding="ascii")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(scaffold.ScaffoldError, match="not a directory"):
        scaffold.stat_row(str(regular), False, "directory")
    with pytest.raises(scaffold.ScaffoldError, match="not a regular file"):
        scaffold.stat_row(str(directory), False, "file")


def test_public_ready_identifies_the_scaffold_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def open_request(request, timeout):
        captured["user_agent"] = request.get_header("User-agent")
        captured["accept"] = request.get_header("Accept")
        captured["timeout"] = timeout
        return io.BytesIO(b'{"status":"ready","mode":"production"}')

    monkeypatch.setattr(scaffold.urllib.request, "urlopen", open_request)
    assert scaffold.public_ready("https://example.test/readyz") is True
    assert captured == {
        "user_agent": "Menhir-Scaffold/1",
        "accept": "application/json",
        "timeout": 10,
    }


def test_seed_drill_preserves_current_generation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup = tmp_path / "backup.json"
    drill = tmp_path / "drill.json"
    backup.write_text('{"generation":"generation.current"}', encoding="ascii")
    expected = {
        "generation": "generation.current",
        "checked_utc": "2026-09-01T02:00:00+00:00",
    }
    drill.write_text(json.dumps(expected), encoding="ascii")
    monkeypatch.setattr(scaffold, "BACKUP_RECEIPT", backup)
    monkeypatch.setattr(scaffold, "DRILL_RECEIPT", drill)
    monkeypatch.setattr(scaffold, "require_root", lambda: None)
    monkeypatch.setattr(scaffold, "require_safe_root_file", lambda path, label: None)
    assert scaffold.seed_drill() == expected
