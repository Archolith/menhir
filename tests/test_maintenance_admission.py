from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD_PATH = ROOT / "deploy" / "scaffold" / "menhir_scaffold.py"
APP_ONLY_PATH = ROOT / "deploy" / "scaffold" / "menhir_app_only.py"
SECURITY_PATH = ROOT / "deploy" / "scaffold" / "menhir_security_config.py"
RELEASE_RUN_PATH = ROOT / "deploy" / "release-run.sh"
PROMOTE_PATH = ROOT / "deploy" / "personal_promote.ps1"
SHARED_WRAPPER = Path.home() / "IdeaProjects" / "scripts" / "deploy-menhir.ps1"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scaffold = load("maintenance_admission_scaffold", SCAFFOLD_PATH)
app_only = load("maintenance_admission_app_only", APP_ONLY_PATH)


def binding(attempt: str = "a" * 32) -> dict[str, str]:
    return scaffold.maintenance_binding(
        "menhir-prod-0.2.0-14",
        "1" * 64,
        "2" * 64,
        "3" * 64,
        attempt,
        "2026-01-08T12:00:00+00:00",
        "2026-01-08T12:01:00+00:00",
    )


def journal(bound: dict[str, str], *, complete: bool = False) -> dict[str, object]:
    completed = "2026-01-08T12:03:00+00:00" if complete else None
    return {
        "schema": 1,
        "kind": "menhir-release-run",
        "release_id": bound["release_id"],
        "release_manifest_sha256": bound["release_manifest_sha256"],
        "stage": "complete" if complete else "backup",
        "generation": "generation.20260108T120200Z",
        "started_utc": "2026-01-08T12:02:00+00:00",
        "updated_utc": completed or "2026-01-08T12:02:30+00:00",
        "completed_utc": completed,
        "runner_sha256": bound["runner_sha256"],
        "approval_sha256": bound["approval_sha256"],
        "promotion_attempt_id": bound["promotion_attempt_id"],
        "approved_utc": bound["approved_utc"],
        "promotion_started_utc": bound["promotion_started_utc"],
    }


def test_maintenance_journal_exact_schema_includes_runner_and_validates() -> None:
    bound = binding()
    value = journal(bound)

    assert "runner_sha256" in scaffold.MAINTENANCE_STATE_KEYS
    assert set(value) == scaffold.MAINTENANCE_STATE_KEYS
    assert scaffold.validate_maintenance_state(value, bound) is value


def test_maintenance_journal_rejects_preapproval_and_old_attempt() -> None:
    bound = binding()
    preapproval = journal(bound)
    preapproval["started_utc"] = "2026-01-08T11:59:59+00:00"
    with pytest.raises(scaffold.ScaffoldError, match="chronology"):
        scaffold.validate_maintenance_state(preapproval, bound)

    old_attempt = journal(bound)
    with pytest.raises(scaffold.ScaffoldError, match="another promotion_attempt_id"):
        scaffold.validate_maintenance_state(old_attempt, binding("b" * 32))


def test_begin_resumes_crashed_holder_without_rewriting_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = binding()
    state_path = tmp_path / "release-run.json"
    original = journal(bound)
    state_path.write_text(json.dumps(original), encoding="ascii")
    original_bytes = state_path.read_bytes()
    commands: list[list[str]] = []
    active = iter((False, True, True))

    monkeypatch.setattr(scaffold, "RELEASE_RUN", state_path)
    monkeypatch.setattr(scaffold, "ADMISSION_READY", tmp_path / "ready.json")
    monkeypatch.setattr(scaffold, "require_root", lambda: None)
    monkeypatch.setattr(scaffold, "require_safe_root_file", lambda path, label: None)
    monkeypatch.setattr(scaffold, "_admission_unit_active", lambda: next(active))
    monkeypatch.setattr(scaffold, "_admission_lock_held", lambda: True)
    monkeypatch.setattr(
        scaffold, "_ready_attempt", lambda: bound["promotion_attempt_id"],
    )

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(scaffold.subprocess, "run", fake_run)

    resumed = scaffold.begin_maintenance(bound)

    assert resumed["started_utc"] == original["started_utc"]
    assert state_path.read_bytes() == original_bytes
    holder = next(command for command in commands if command[0] == "systemd-run")
    assert "hold-maintenance" in holder
    assert str(scaffold.ADMISSION_LOCK) in holder


def test_app_and_security_lanes_explicitly_share_admission_authority() -> None:
    security = SECURITY_PATH.read_text(encoding="utf-8")
    assert app_only.ADMISSION_LOCK == Path(
        "/run/lock/menhir-production-admission.lock"
    )
    assert "ADMISSION_LOCK = app.ADMISSION_LOCK" in security
    assert security.count("lock = acquire_security_config_admission()") == 3
    assert "for path in (ADMISSION_LOCK, MUTATION_LOCK)" in APP_ONLY_PATH.read_text(
        encoding="utf-8"
    )


def test_release_runner_requires_bound_holder_and_preserves_chronology() -> None:
    release_run = RELEASE_RUN_PATH.read_text(encoding="utf-8")
    first_assert = release_run.index("assert-maintenance")
    first_stage_write = release_run.index("write_stage()")
    acceptance = release_run.index('python3 "$acceptance_probe" production')
    final_assert = release_run.rindex("assert-maintenance")
    assert first_assert < first_stage_write
    assert acceptance < final_assert
    assert 'value={**prior,"stage":stage' in release_run
    assert 'value["completed_utc"]=now' in release_run


def test_personal_promoter_binds_maintenance_to_approval_and_attempt() -> None:
    promote = PROMOTE_PATH.read_text(encoding="utf-8")
    assert "-ApprovalSha256 $ExpectedApprovalSha256" in promote
    assert "-PromotionAttemptId $PromotionAttemptId" in promote
    assert "$transaction.approval_sha256 -ne $ExpectedApprovalSha256" in promote
    assert "$transaction.promotion_attempt_id -ne $PromotionAttemptId" in promote


@pytest.mark.skipif(not SHARED_WRAPPER.exists(), reason="shared wrapper is outside CI checkout")
def test_shared_wrapper_begins_maintenance_then_delegates_bootstrap_to_installer() -> None:
    wrapper = SHARED_WRAPPER.read_text(encoding="utf-8")
    begin = wrapper.index("begin-maintenance $admissionArguments")
    installer = wrapper.index("sudo -n bash '$trustedBundle/install.sh'")
    release_run = wrapper.index(
        'Invoke-Vps "sudo -n env MENHIR_ROOT_RUNNER_SHA256=', installer,
    )
    complete = wrapper.index("complete-maintenance $admissionArguments", release_run)
    preinstall = wrapper[begin:installer]

    assert begin < installer < release_run < complete
    assert "$bootstrapJob" not in wrapper
    assert "$temporaryBin" not in preinstall
    assert "find /srv/menhir/backups/encrypted" not in preinstall
    assert "menhir_schema.py" not in preinstall
    assert "backup_cleanup_txn.py" not in preinstall
    assert "menhir-backup-local" not in preinstall
    assert "backup-generation.sh" not in preinstall
    assert "sudo -n install" not in preinstall
    assert "MENHIR_PROMOTION_ATTEMPT_ID='$PromotionAttemptId'" in wrapper
    assert '"started_utc": state.get("started_utc")' in wrapper
    assert '"completed_utc": state.get("completed_utc")' in wrapper
