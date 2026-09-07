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

APP_ONLY_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scaffold" / "menhir_app_only.py"
APP_SPEC = importlib.util.spec_from_file_location("menhir_app_only", APP_ONLY_SCRIPT)
assert APP_SPEC is not None and APP_SPEC.loader is not None
app_only = importlib.util.module_from_spec(APP_SPEC)
APP_SPEC.loader.exec_module(app_only)


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
        "app_only_stage": None,
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


def test_unfinished_app_only_transaction_is_refused() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now)
    evidence["app_only_stage"] = "replacing"
    assert "an unfinished app-only transaction is active" in scaffold.evaluate_evidence(
        contract()["backup_policy"], evidence, now,
    )


def test_fresh_retained_desktop_archive_can_lag_latest_backup() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now)
    evidence["backup_generation"] = "generation.new"
    evidence["drill_generation"] = "generation.new"
    assert scaffold.evaluate_evidence(contract()["backup_policy"], evidence, now) == []


def test_duplicate_archives_do_not_satisfy_distinct_generation_minimum() -> None:
    now = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
    evidence = valid_evidence(now)
    evidence["encrypted_generations"] = 2
    evidence["retained_generations"] = ["generation.same"]
    failures = scaffold.evaluate_evidence(contract()["backup_policy"], evidence, now)
    assert "insufficient encrypted backup generations" in failures


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


def release_pair() -> tuple[dict, dict, dict, dict, str]:
    live = {
        "release_id": "release-1",
        "repos": {"menhir": "a" * 40, "oauth": "b" * 40},
        "images": {
            "menhir": "sha256:" + "1" * 64,
            "neo4j": "sha256:" + "2" * 64,
            "caddy": "sha256:" + "3" * 64,
        },
        "provenance_sha256": "4" * 64,
        "sbom_sha256": "5" * 64,
        "scan_evidence_sha256": "6" * 64,
        "wheel_manifest_sha256": "7" * 64,
        "dockerfile_wheel_manifest_sha256": "8" * 64,
        "rendered": {"production_env_sha256": "9" * 64, "policy_sha256": "a" * 64},
        "security_review": {"verdict": "APPROVED", "authority_sha256": "b" * 64},
        "rollback_anchors": {"prior_release_id": "release-0"},
        "artifacts": {
            "/srv/menhir/production/release/production.env": {
                "kind": "rendered", "sha256": "9" * 64,
            },
            "/srv/menhir/production/bin/tool.py": {
                "kind": "git", "repository": "menhir", "path": "deploy/tool.py",
                "commit": "a" * 40, "blob_oid": "c" * 40, "sha256": "d" * 64,
            },
        },
    }
    candidate = json.loads(json.dumps(live))
    candidate["release_id"] = "release-2"
    candidate["repos"]["menhir"] = "e" * 40
    candidate["images"]["menhir"] = "sha256:" + "f" * 64
    for key in (
        "provenance_sha256", "sbom_sha256", "scan_evidence_sha256",
        "wheel_manifest_sha256", "dockerfile_wheel_manifest_sha256",
    ):
        candidate[key] = "0" * 64
    candidate_env_sha = "1" * 64
    candidate["rendered"]["production_env_sha256"] = candidate_env_sha
    candidate["security_review"] = {"verdict": "APPROVED", "authority_sha256": "2" * 64}
    live_sha = "3" * 64
    candidate["rollback_anchors"] = {
        "prior_release_id": live["release_id"],
        "prior_release_sha256": live_sha,
        "prior_images": live["images"].copy(),
    }
    candidate["artifacts"]["/srv/menhir/production/release/production.env"]["sha256"] = candidate_env_sha
    candidate["artifacts"]["/srv/menhir/production/bin/tool.py"]["commit"] = "e" * 40
    live_env = {
        "MENHIR_IMAGE": "ghcr.io/a/menhir:1@" + live["images"]["menhir"],
        "NEO4J_IMAGE": "ghcr.io/a/neo4j:1@" + live["images"]["neo4j"],
        "MENHIR_RELEASE_COMMIT": live["repos"]["menhir"],
        "MENHIR_RELEASE_ID": live["release_id"],
        "FIXED": "yes",
    }
    candidate_env = live_env.copy()
    candidate_env.update({
        "MENHIR_IMAGE": "ghcr.io/a/menhir:1@" + candidate["images"]["menhir"],
        "MENHIR_RELEASE_COMMIT": candidate["repos"]["menhir"],
        "MENHIR_RELEASE_ID": candidate["release_id"],
    })
    return live, candidate, live_env, candidate_env, live_sha


def test_app_only_classifier_accepts_only_image_release_metadata() -> None:
    live, candidate, live_env, candidate_env, live_sha = release_pair()
    result = app_only.classify_release(
        live, candidate, live_sha, live_env, candidate_env,
        candidate["rendered"]["production_env_sha256"],
    )
    assert result["classification"] == "app-only"
    assert result["candidate_release_id"] == "release-2"


@pytest.mark.parametrize("mutation,match", [
    ("same-id", "new immutable release_id"),
    ("bad-rollback", "rollback anchors"),
    ("protected-artifact", "protected release surfaces"),
    ("protected-env", "protected production environment"),
])
def test_app_only_classifier_refuses_protected_changes(mutation: str, match: str) -> None:
    live, candidate, live_env, candidate_env, live_sha = release_pair()
    if mutation == "same-id":
        candidate["release_id"] = live["release_id"]
        candidate_env["MENHIR_RELEASE_ID"] = live["release_id"]
    elif mutation == "bad-rollback":
        candidate["rollback_anchors"]["prior_release_sha256"] = "0" * 64
    elif mutation == "protected-artifact":
        candidate["artifacts"]["/srv/menhir/production/bin/tool.py"]["sha256"] = "0" * 64
    else:
        candidate_env["FIXED"] = "changed"
    with pytest.raises(app_only.AppOnlyError, match=match):
        app_only.classify_release(
            live, candidate, live_sha, live_env, candidate_env,
            candidate["rendered"]["production_env_sha256"],
        )


def test_app_replacement_primitive_has_one_authoritative_implementation() -> None:
    scaffold_root = Path(__file__).resolve().parents[1] / "deploy" / "scaffold"
    matching = [
        path.name for path in scaffold_root.glob("*.py")
        if "--force-recreate" in path.read_text(encoding="utf-8")
    ]
    assert matching == ["menhir_app_only.py"]


def test_probe_token_is_jit_minted_without_persistent_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "client-policy.json"
    policy_path.write_text(json.dumps({
        "clients": {
            "menhir-deploy-probe": {
                "label": "menhir-deploy-probe",
                "scopes": ["menhir:read"],
                "maximum_tier": "readonly",
                "namespace": "",
                "allowed_tools": ["recall_memories"],
                "denied_tools": ["add_memory"],
            },
        },
    }), encoding="ascii")
    observed: dict[str, object] = {}

    def fake_run(command, timeout, *, input_bytes=None):
        observed.update(command=command, timeout=timeout, script=input_bytes)
        return "header.payload.signature"

    monkeypatch.setattr(app_only, "LIVE_POLICY", policy_path)
    monkeypatch.setattr(app_only, "require_root_file", lambda path, label: None)
    monkeypatch.setattr(app_only, "run", fake_run)
    assert app_only.mint_probe_token() == "header.payload.signature"
    assert observed["command"] == [
        "docker", "exec", "-i", "menhir-prod-app", "python", "-",
    ]
    assert b'"exp": now + 60' in observed["script"]
    assert b'MENHIR_OAUTH_SIGNING_KEY_PATH' in observed["script"]
    assert "acceptance-token" not in APP_ONLY_SCRIPT.read_text(encoding="utf-8")


def test_probe_policy_refuses_broader_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "client-policy.json"
    policy_path.write_text(json.dumps({
        "clients": {
            "menhir-deploy-probe": {
                "label": "menhir-deploy-probe",
                "scopes": ["menhir:read", "menhir:write"],
                "maximum_tier": "agent",
                "namespace": "",
                "allowed_tools": ["recall_memories"],
                "denied_tools": ["add_memory"],
            },
        },
    }), encoding="ascii")
    monkeypatch.setattr(app_only, "LIVE_POLICY", policy_path)
    monkeypatch.setattr(app_only, "require_root_file", lambda path, label: None)
    with pytest.raises(app_only.AppOnlyError, match="exact read-only"):
        app_only.require_probe_policy()


def test_maintenance_release_uses_release_owned_in_memory_probe_acceptance() -> None:
    release_run = (
        Path(__file__).resolve().parents[1] / "deploy" / "release-run.sh"
    ).read_text(encoding="utf-8")
    assert 'production_lock="/run/lock/menhir-production.lock"' in release_run
    assert 'flock -n 9' in release_run
    assert 'acceptance_probe="${SCRIPT_DIR}/mcp_acceptance_probe.py"' in release_run
    assert '"${SCRIPT_DIR}/verify-artifacts"' in release_run
    assert 'python3 "$acceptance_probe" production' in release_run
    assert "/srv/menhir/scaffold/bin/menhir_app_only.py" not in release_run
    assert "acceptance-token" not in release_run
