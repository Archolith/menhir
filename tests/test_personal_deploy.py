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
    notes_json = workspace / "release-notes.json"
    notes_markdown = workspace / "release-notes.md"
    notes_json.write_text('{"release_id":"menhir-prod-0.2.0-12"}\n', encoding="ascii")
    notes_markdown.write_text("# Menhir release\n", encoding="ascii")
    release = {
        "release_id": "menhir-prod-0.2.0-12",
        "deployment_class": "security-config",
        "ingress_mode": "cloudflared",
        "notes_json_sha256": _sha(notes_json),
        "notes_markdown_sha256": _sha(notes_markdown),
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
        "ingress_mode": "cloudflared",
        "notes_json_sha256": release["notes_json_sha256"],
        "notes_markdown_sha256": release["notes_markdown_sha256"],
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
    started = completed - timedelta(minutes=4)
    preflight = {
        "schema": 1,
        "kind": "menhir-production-readiness-preflight",
        "result": "passed",
        "observed_utc": (started - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "deployment_class": state["deployment_class"],
        "ingress_mode": state["ingress_mode"],
        "candidate_release_id": state["release_id"],
        "checks": {
            "live_services": {},
            "network_roles": {},
            "release_journal": {},
            "headroom": {
                "disk_free_bytes": 9,
                "disk_required_bytes": 8,
                "memory_available_bytes": 8,
                "memory_required_bytes": 7,
            },
            "maintenance_route": {"applicable": state["deployment_class"] == "maintenance"},
        },
    }
    preflight["canonical_sha256"] = hashlib.sha256(json.dumps(
        preflight, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    return {
        "schema": 1,
        "kind": "menhir-personal-staging",
        "result": "passed",
        "release_id": state["release_id"],
        "release_sha256": state["release_sha256"],
        "bundle_sha256": state["bundle_sha256"],
        "deployment_class": state["deployment_class"],
        "ingress_mode": state["ingress_mode"],
        "images": {
            "menhir": state["menhir_image"],
            "neo4j": state["neo4j_image"],
        },
        "runner_sha256": MODULE._runner_sha256(runner),
        "started_utc": started.isoformat().replace("+00:00", "Z"),
        "completed_utc": completed.isoformat().replace("+00:00", "Z"),
        "test_identities": {
            "oauth_client_id": "menhir-staging-probe",
            "subject": "menhir-admin",
            "namespace": "menhir-staging",
        },
        "checks": checks or {name: True for name in MODULE.STAGING_CHECKS},
        "production_preflight": preflight,
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


def test_select_rejects_state_release_deployment_class_mismatch(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    release_path = product / MODULE.RELEASE_NAME
    release = json.loads(release_path.read_text(encoding="utf-8"))
    release["deployment_class"] = "maintenance"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    state_path = product / MODULE.RELEASE_STATE_NAME
    release_state = json.loads(state_path.read_text(encoding="utf-8"))
    release_state["release_sha256"] = _sha(release_path)
    state_path.write_text(json.dumps(release_state), encoding="utf-8")
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()

    with pytest.raises(MODULE.PersonalDeployError, match="state/release deployment class"):
        MODULE.select_flow(product, deployment)


def test_select_refuses_legacy_release_without_immutable_bindings(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    release_path = product / MODULE.RELEASE_NAME
    release = json.loads(release_path.read_text(encoding="utf-8"))
    for key in ("deployment_class", "notes_json_sha256", "notes_markdown_sha256"):
        release.pop(key)
    release_path.write_text(json.dumps(release), encoding="utf-8")
    state_path = product / MODULE.RELEASE_STATE_NAME
    release_state = json.loads(state_path.read_text(encoding="utf-8"))
    release_state["release_sha256"] = _sha(release_path)
    state_path.write_text(json.dumps(release_state), encoding="utf-8")
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()

    with pytest.raises(MODULE.PersonalDeployError, match="deployment class mismatch"):
        MODULE.select_flow(product, deployment)


def test_select_rejects_generated_release_notes_drift(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    (product / "release-notes.md").write_text("changed\n", encoding="ascii")
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()

    with pytest.raises(MODULE.PersonalDeployError, match="release-notes.md changed"):
        MODULE.select_flow(product, deployment)


def test_stage_preview_is_read_only(tmp_path: Path) -> None:
    _, deployment, _ = _selected(tmp_path)
    runner = _runner(tmp_path)

    command = MODULE.stage_flow(deployment, runner, execute=False)

    assert isinstance(command, list)
    assert "--expected-bundle-sha256" in command
    assert MODULE.status_flow(deployment)["phase"] == "selected"


def test_stage_refuses_direct_vps_runner(tmp_path: Path) -> None:
    _, deployment, _ = _selected(tmp_path)
    runner = tmp_path / "personal_stage_vps.py"
    runner.write_text("# companion only\n", encoding="ascii")

    with pytest.raises(MODULE.PersonalDeployError, match="cannot run directly"):
        MODULE.stage_flow(deployment, runner.resolve(), execute=False)


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


def test_stage_rejects_tampered_production_preflight(tmp_path: Path) -> None:
    _, deployment, selected = _selected(tmp_path)
    runner = _runner(tmp_path)

    def run(command: list[str]) -> None:
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt = _receipt(selected, runner)
        receipt["production_preflight"]["checks"]["headroom"]["disk_free_bytes"] = 0
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(MODULE.PersonalDeployError, match="preflight digest mismatch"):
        MODULE.stage_flow(deployment, runner, execute=True, command_runner=run)


def test_rehearse_selects_stages_and_resumes_without_reexecution(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()
    runner = _runner(tmp_path)
    seen: list[list[str]] = []

    def run(command: list[str]) -> None:
        seen.append(command)
        selected = MODULE.status_flow(deployment)
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(json.dumps(_receipt(selected, runner)), encoding="utf-8")

    staged = MODULE.rehearse_flow(
        product,
        deployment,
        runner,
        execute=True,
        command_runner=run,
    )
    resumed = MODULE.rehearse_flow(
        product,
        deployment,
        runner,
        execute=True,
        command_runner=lambda _: pytest.fail("completed rehearsal ran twice"),
    )

    assert len(seen) == 1
    assert isinstance(staged, dict) and staged["phase"] == "staged"
    assert resumed == staged
    assert not (deployment / MODULE.APPROVAL_NAME).exists()


def test_rehearse_resumes_an_existing_matching_selection(tmp_path: Path) -> None:
    product = _product_release(tmp_path)
    deployment = tmp_path / "personal-deployment"
    deployment.mkdir()
    runner = _runner(tmp_path)

    command = MODULE.rehearse_flow(product, deployment, runner, execute=False)
    selected = MODULE.status_flow(deployment)

    def run(command: list[str]) -> None:
        receipt_path = Path(command[command.index("--receipt") + 1])
        receipt_path.write_text(json.dumps(_receipt(selected, runner)), encoding="utf-8")

    state = MODULE.rehearse_flow(
        product,
        deployment,
        runner,
        execute=True,
        command_runner=run,
    )

    assert isinstance(command, list)
    assert selected["phase"] == "selected"
    assert isinstance(state, dict) and state["phase"] == "staged"


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


def test_approval_derives_validated_confirmations_but_requires_identity(tmp_path: Path) -> None:
    deployment, _, _ = _stage(tmp_path)

    with pytest.raises(MODULE.PersonalDeployError, match="identity"):
        MODULE.approve_flow(deployment)

    approved = MODULE.approve_flow(deployment, approved_by="owner")

    assert approved["phase"] == "approved"


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
    assert command[command.index("-Mode") + 1] == "SecurityConfig"
    assert command[command.index("-ExpectedStagingReceiptSha256") + 1] == approved["staging_receipt_sha256"]
    assert command[command.index("-ExpectedApprovalSha256") + 1] == approved["approval_sha256"]
    assert command[command.index("-ExpectedReleaseSha256") + 1] == approved["release_sha256"]
    assert command[command.index("-TransactionReceipt") + 1].endswith(
        MODULE.ROOT_TRANSACTION_RECEIPT_NAME
    )
    assert MODULE.status_flow(deployment)["phase"] == "approved"


def test_cli_derives_confirmations_and_promotion_still_requires_execute(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, _, _ = _stage(tmp_path)
    wrapper = tmp_path / "deploy-menhir.ps1"
    wrapper.write_text("# production wrapper\n", encoding="ascii")
    monkeypatch.setenv("USERNAME", "implicit-owner")

    assert MODULE.main([
        "approve",
        "--workspace", str(deployment),
    ]) == 1
    assert MODULE.status_flow(deployment)["phase"] == "staged"

    assert MODULE.main([
        "approve",
        "--workspace", str(deployment),
        "--approved-by", "owner",
    ]) == 0
    assert MODULE.main([
        "promote",
        "--workspace", str(deployment),
        "--wrapper", str(wrapper.resolve()),
    ]) == 0

    capsys.readouterr()
    assert MODULE.status_flow(deployment)["phase"] == "approved"
    assert not (deployment / MODULE.PROMOTION_RECEIPT_NAME).exists()


def test_legacy_cli_confirmations_keep_environment_identity_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, _, staged = _stage(tmp_path)
    monkeypatch.setenv("USERNAME", "legacy-owner")

    assert MODULE.main([
        "approve",
        "--workspace", str(deployment),
        "--confirm-release-id", staged["release_id"],
        "--confirm-staging-sha256", staged["staging_receipt_sha256"],
    ]) == 0

    capsys.readouterr()
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

    def run(command: list[str]) -> None:
        seen.append(command)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        transaction = {
            "schema": 1,
            "kind": "menhir-security-config-transaction",
            "runner_sha256": "c" * 64,
            "result": "passed",
            "stage": "complete",
            "candidate_release_id": approved["release_id"],
            "candidate_release_sha256": approved["release_sha256"],
            "database_container_id": "database-1",
            "database_container_id_after": "database-1",
            "ingress_container_id": "cloudflared-1",
            "ingress_container_id_after": "cloudflared-1",
            "started_utc": now,
            "completed_utc": now,
        }
        transaction_path = Path(command[command.index("-TransactionReceipt") + 1])
        transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
        receipt_path = Path(command[command.index("-ResultReceipt") + 1])
        receipt_path.write_text(json.dumps({
            "schema": 1,
            "kind": "menhir-personal-promotion",
            "result": "passed",
            "release_id": approved["release_id"],
            "release_sha256": approved["release_sha256"],
            "bundle_sha256": approved["bundle_sha256"],
            "staging_receipt_sha256": approved["staging_receipt_sha256"],
            "approval_sha256": approved["approval_sha256"],
            "deployment_class": approved["deployment_class"],
            "ingress_mode": approved["ingress_mode"],
            "started_utc": now,
            "completed_utc": now,
            "elapsed_seconds": 0,
            "promotion_wrapper_sha256": "a" * 64,
            "operator_wrapper_sha256": "b" * 64,
            "transaction_kind": approved["deployment_class"],
            "transaction_receipt_sha256": MODULE._sha256(transaction_path),
            "transaction": transaction,
        }), encoding="utf-8")

    promoted = MODULE.promote_flow(
        deployment,
        approved["release_id"],
        approved["staging_receipt_sha256"],
        execute=True,
        wrapper_path=wrapper.resolve(),
        command_runner=run,
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
