from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "deploy" / "personal_deploy.py"
SPEC = importlib.util.spec_from_file_location("personal_deploy", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _product_release(tmp_path: Path) -> Path:
    workspace = tmp_path / "product-release"
    bundle = workspace / MODULE.BUNDLE_NAME
    release_path = workspace / MODULE.RELEASE_NAME
    bundle.mkdir(parents=True)
    release = {
        "release_id": "menhir-prod-0.2.0-12",
        "images": {
            "menhir": "sha256:" + "1" * 64,
            "neo4j": "sha256:" + "2" * 64,
        },
    }
    release_path.write_text(json.dumps(release), encoding="utf-8")
    (bundle / "payload").write_text("immutable\n", encoding="ascii")
    state = {
        "kind": "menhir-release-flow",
        "phase": "bundled",
        "release_id": release["release_id"],
        "release_sha256": _sha(release_path),
        "bundle_sha256": MODULE._tree_sha256(bundle),
        "deployment_class": "security-config",
    }
    (workspace / MODULE.RELEASE_STATE_NAME).write_text(json.dumps(state), encoding="utf-8")
    return workspace


def _selected(tmp_path: Path) -> tuple[Path, Path, dict]:
    product = _product_release(tmp_path)
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()
    state = MODULE.select_flow(product, deployment)
    return product, deployment, state


def _runner(tmp_path: Path) -> Path:
    runner = tmp_path / "stage.py"
    runner.write_text("# immutable staging runner\n", encoding="ascii")
    runner.chmod(0o700)
    return runner.resolve()


def _receipt(state: dict, runner: Path, *, checks: dict | None = None, hours_old: int = 0) -> dict:
    completed = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    return {
        "schema": 1,
        "kind": "menhir-personal-staging",
        "result": "passed",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "deployment_class": state["deployment_class"],
        "images": {
            "menhir": state["menhir_image"],
            "neo4j": state["neo4j_image"],
        },
        "runner_sha256": MODULE._runner_sha256(runner),
        "started_utc": (completed - timedelta(minutes=4)).isoformat().replace("+00:00", "Z"),
        "completed_utc": completed.isoformat().replace("+00:00", "Z"),
        "test_identities": {
            "oauth_client_id": "menhir-staging-probe",
            "subject": "menhir-admin",
            "namespace": "menhir-staging",
        },
        "checks": checks or {name: True for name in MODULE.STAGING_CHECKS},
    }


def _stage(tmp_path: Path) -> tuple[Path, Path, dict]:
    _, deployment, selected = _selected(tmp_path)
    runner = _runner(tmp_path)

    def run(command: list[str]) -> None:
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(json.dumps(_receipt(selected, runner)), encoding="utf-8")

    state = MODULE.stage_flow(deployment, runner, execute=True, command_runner=run)
    assert isinstance(state, dict)
    return deployment, runner, state


def test_select_binds_exact_finalized_release(tmp_path: Path) -> None:
    product, deployment, state = _selected(tmp_path)

    assert state["phase"] == "selected"
    assert state["release_workspace"] == str(product.resolve())
    assert state["release_id"] == "menhir-prod-0.2.0-12"
    assert MODULE.status_flow(deployment) == state


def test_select_rejects_changed_product_bundle(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    (product / MODULE.BUNDLE_NAME / "extra").write_text("drift\n", encoding="ascii")
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()

    with pytest.raises(MODULE.PersonalDeployError, match="artifacts changed"):
        MODULE.select_flow(product, deployment)


def test_stage_preview_is_read_only(tmp_path: Path) -> None:
    _, deployment, _ = _selected(tmp_path)
    runner = _runner(tmp_path)

    command = MODULE.stage_flow(deployment, runner, execute=False)

    assert isinstance(command, list)
    assert "--expected-bundle-sha256" in command
    assert MODULE.status_flow(deployment)["phase"] == "selected"


def test_real_staging_runner_identity_binds_desktop_and_vps_components(tmp_path: Path) -> None:
    wrapper = tmp_path / "personal_stage.ps1"
    companion = tmp_path / "personal_stage_vps.py"
    wrapper.write_text("# desktop\n", encoding="ascii")
    companion.write_text("# vps one\n", encoding="ascii")
    first = MODULE._runner_sha256(wrapper.resolve())
    companion.write_text("# vps two\n", encoding="ascii")
    assert MODULE._runner_sha256(wrapper.resolve()) != first


def test_stage_accepts_only_complete_digest_bound_receipt(tmp_path: Path) -> None:
    deployment, _, state = _stage(tmp_path)

    assert state["phase"] == "staged"
    assert state["staging_receipt_sha256"] == _sha(deployment / MODULE.STAGING_RECEIPT_NAME)
    assert MODULE.status_flow(deployment) == state


def test_stage_rejects_missing_required_check(tmp_path: Path) -> None:
    _, deployment, selected = _selected(tmp_path)
    runner = _runner(tmp_path)
    checks = {name: True for name in MODULE.STAGING_CHECKS}
    checks["automatic_rollback"] = False

    def run(command: list[str]) -> None:
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(
            json.dumps(_receipt(selected, runner, checks=checks)),
            encoding="utf-8",
        )

    with pytest.raises(MODULE.PersonalDeployError, match="every required check"):
        MODULE.stage_flow(deployment, runner, execute=True, command_runner=run)
    assert MODULE.status_flow(deployment)["phase"] == "selected"


def test_stage_rejects_stale_receipt(tmp_path: Path) -> None:
    _, deployment, selected = _selected(tmp_path)
    runner = _runner(tmp_path)

    def run(command: list[str]) -> None:
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(json.dumps(_receipt(selected, runner, hours_old=25)), encoding="utf-8")

    with pytest.raises(MODULE.PersonalDeployError, match="stale"):
        MODULE.stage_flow(deployment, runner, execute=True, command_runner=run)


def test_approval_requires_release_and_receipt_confirmations(tmp_path: Path) -> None:
    deployment, _, staged = _stage(tmp_path)

    with pytest.raises(MODULE.PersonalDeployError, match="release confirmation"):
        MODULE.approve_flow(deployment, "wrong", staged["staging_receipt_sha256"], "owner")
    with pytest.raises(MODULE.PersonalDeployError, match="staging confirmation"):
        MODULE.approve_flow(deployment, staged["release_id"], "0" * 64, "owner")


def test_one_approval_unlocks_promotion_preview(tmp_path: Path) -> None:
    deployment, _, staged = _stage(tmp_path)
    approved = MODULE.approve_flow(
        deployment,
        staged["release_id"],
        staged["staging_receipt_sha256"],
        "owner",
    )
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# production wrapper\n", encoding="ascii")

    command = MODULE.promote_flow(
        deployment,
        approved["release_id"],
        approved["staging_receipt_sha256"],
        execute=False,
        wrapper_path=wrapper.resolve(),
    )

    assert isinstance(command, list)
    assert command[command.index("-Mode") + 1] == "Maintenance"
    assert command[command.index("-ExpectedStagingReceiptSha256") + 1] == approved["staging_receipt_sha256"]
    assert command[command.index("-ExpectedApprovalSha256") + 1] == approved["approval_sha256"]
    assert command[command.index("-ExpectedReleaseSha256") + 1] == approved["release_sha256"]
    assert MODULE.status_flow(deployment)["phase"] == "approved"


def test_promotion_runs_once_and_records_bound_receipt(tmp_path: Path) -> None:
    deployment, _, staged = _stage(tmp_path)
    approved = MODULE.approve_flow(
        deployment,
        staged["release_id"],
        staged["staging_receipt_sha256"],
        "owner",
    )
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# production wrapper\n", encoding="ascii")
    seen: list[list[str]] = []

    promoted = MODULE.promote_flow(
        deployment,
        approved["release_id"],
        approved["staging_receipt_sha256"],
        execute=True,
        wrapper_path=wrapper.resolve(),
        command_runner=seen.append,
    )

    assert seen and isinstance(promoted, dict) and promoted["phase"] == "promoted"
    assert MODULE.status_flow(deployment) == promoted
    MODULE.promote_flow(
        deployment,
        approved["release_id"],
        approved["staging_receipt_sha256"],
        execute=True,
        wrapper_path=wrapper.resolve(),
        command_runner=lambda _: pytest.fail("completed promotion ran twice"),
    )


def test_receipt_tampering_blocks_approval_and_promotion(tmp_path: Path) -> None:
    deployment, _, staged = _stage(tmp_path)
    receipt = deployment / MODULE.STAGING_RECEIPT_NAME
    receipt.write_text(receipt.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(MODULE.PersonalDeployError, match="changed"):
        MODULE.approve_flow(
            deployment,
            staged["release_id"],
            staged["staging_receipt_sha256"],
            "owner",
        )
