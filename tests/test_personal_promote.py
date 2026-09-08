from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


WRAPPER = Path(__file__).parents[1] / "deploy" / "personal_promote.ps1"
REQUIRED_CHECKS = {
    "artifact_identity", "production_memory_limits", "production_network_shape",
    "oauth_policy_shape", "ingress_request_handling", "isolated_disposable_data",
    "non_production_credentials", "production_authority_absent", "oauth_discovery",
    "oauth_authorization_code_pkce", "mcp_initialize", "mcp_tools_list", "mcp_recall",
    "synthetic_write_allowed", "denied_operation_refused", "restart_persistence",
    "automatic_rollback",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            digest.update(
                f"{path.relative_to(root).as_posix()}\0{_sha(path)}\n".encode("utf-8")
            )
    return digest.hexdigest()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path, Path]:
    release_id = "menhir-prod-0.2.0-12"
    release_sha_image = "sha256:" + "1" * 64
    neo4j_sha_image = "sha256:" + "2" * 64
    bundle = tmp_path / "bundle"
    release_path = bundle / "rootfs/srv/menhir/production/release/release.json"
    release_path.parent.mkdir(parents=True)
    release_path.write_text(json.dumps({
        "release_id": release_id,
        "deployment_class": "security-config",
        "ingress_mode": "cloudflared",
        "notes_json_sha256": "5" * 64,
        "notes_markdown_sha256": "6" * 64,
        "images": {"menhir": release_sha_image, "neo4j": neo4j_sha_image},
    }), encoding="utf-8")
    release_sha = _sha(release_path)
    bundle_sha = _tree_sha(bundle)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    preflight = {
        "schema": 1,
        "kind": "menhir-production-readiness-preflight",
        "result": "passed",
        "observed_utc": now,
        "deployment_class": "security-config",
        "ingress_mode": "cloudflared",
        "candidate_release_id": release_id,
        "checks": {
            "live_services": {},
            "network_roles": {
                "ingress": {"identities": [{"container_id": "c" * 64}]},
            },
            "release_journal": {},
            "headroom": {
                "disk_free_bytes": 9,
                "disk_required_bytes": 8,
                "memory_available_bytes": 8,
                "memory_required_bytes": 7,
            },
            "maintenance_route": {"applicable": False},
        },
    }
    preflight["canonical_sha256"] = hashlib.sha256(json.dumps(
        preflight, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    staging_path = tmp_path / "staging.json"
    staging_path.write_text(json.dumps({
        "schema": 1,
        "kind": "menhir-personal-staging",
        "result": "passed",
        "release_id": release_id,
        "release_sha256": release_sha,
        "bundle_sha256": bundle_sha,
        "deployment_class": "security-config",
        "ingress_mode": "cloudflared",
        "images": {"menhir": release_sha_image, "neo4j": neo4j_sha_image},
        "runner_sha256": "3" * 64,
        "started_utc": now,
        "completed_utc": now,
        "test_identities": {
            "oauth_client_id": "menhir-staging-probe",
            "subject": "menhir-admin",
            "namespace": "menhir-staging",
        },
        "checks": {name: True for name in REQUIRED_CHECKS},
        "production_preflight": preflight,
    }), encoding="utf-8")
    staging_sha = _sha(staging_path)
    promotion_sha = _sha(WRAPPER)
    operator_sha = _sha(WRAPPER.with_name("personal_security_config.ps1"))
    root_runner_sha = _sha(WRAPPER.parent / "scaffold" / "menhir_security_config.py")
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps({
        "schema": 1,
        "kind": "menhir-personal-promotion-approval",
        "release_id": release_id,
        "release_sha256": release_sha,
        "bundle_sha256": bundle_sha,
        "staging_receipt_sha256": staging_sha,
        "promotion_wrapper_sha256": promotion_sha,
        "operator_wrapper_sha256": operator_sha,
        "root_runner_sha256": root_runner_sha,
        "approved_by": "owner",
        "approved_utc": now,
    }), encoding="utf-8")
    transaction_path = tmp_path / "root-transaction.json"
    transaction_path.write_text(json.dumps({
        "schema": 1,
        "kind": "menhir-security-config-transaction",
        "result": "passed",
        "stage": "complete",
        "runner_sha256": root_runner_sha,
        "started_utc": now,
        "completed_utc": now,
        "candidate_release_id": release_id,
        "candidate_release_sha256": release_sha,
        "database_container_id": "database-1",
        "database_container_id_after": "database-1",
        "ingress_container_id": "c" * 64,
        "ingress_container_id_after": "c" * 64,
    }), encoding="utf-8")
    result_path = tmp_path / "promotion-result.json"
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(WRAPPER),
        "-Mode", "SecurityConfig", "-BundlePath", str(bundle),
        "-ExpectedBundleSha256", bundle_sha, "-Release", release_id,
        "-ExpectedReleaseSha256", release_sha, "-StagingReceipt", str(staging_path),
        "-ExpectedStagingReceiptSha256", staging_sha, "-Approval", str(approval_path),
        "-ExpectedApprovalSha256", _sha(approval_path), "-SourceRepository", str(tmp_path),
        "-ExpectedPromotionWrapperSha256", promotion_sha,
        "-ExpectedOperatorWrapperSha256", operator_sha,
        "-ExpectedRootRunnerSha256", root_runner_sha,
        "-PromotionAttemptId", "a" * 32,
        "-PromotionStartedUtc", now,
        "-TransactionReceipt", str(transaction_path),
        "-ResultReceipt", str(result_path),
    ]
    return command, result_path, staging_path


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="requires Windows PowerShell")
def test_promotion_gate_validates_evidence_before_calling_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, result_path, _ = _fixture(tmp_path, monkeypatch)
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert receipt["transaction_kind"] == "security-config"


@pytest.mark.skipif(shutil.which("pwsh.exe") is None, reason="requires PowerShell 7")
def test_promotion_gate_accepts_json_timestamps_under_powershell_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, result_path, _ = _fixture(tmp_path, monkeypatch)
    command[0] = shutil.which("pwsh.exe") or "pwsh.exe"

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result_path.read_text(encoding="utf-8-sig"))
    assert receipt["promotion_attempt_id"] == "a" * 32
    assert "transaction" not in receipt


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="requires Windows PowerShell")
def test_promotion_gate_blocks_tampered_receipt_before_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, result_path, staging = _fixture(tmp_path, monkeypatch)
    staging.write_text(staging.read_text(encoding="utf-8") + " ", encoding="utf-8")
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert not result_path.exists()


@pytest.mark.skipif(shutil.which("pwsh.exe") is None, reason="requires PowerShell 7")
def test_promotion_gate_recomputes_preflight_seal_after_outer_rebinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, result_path, staging = _fixture(tmp_path, monkeypatch)
    command[0] = shutil.which("pwsh.exe") or "pwsh.exe"
    value = json.loads(staging.read_text(encoding="utf-8"))
    value["production_preflight"]["checks"]["headroom"]["disk_free_bytes"] += 1
    staging.write_text(json.dumps(value), encoding="utf-8")
    staging_sha = _sha(staging)
    command[command.index("-ExpectedStagingReceiptSha256") + 1] = staging_sha
    approval = Path(command[command.index("-Approval") + 1])
    approval_value = json.loads(approval.read_text(encoding="utf-8"))
    approval_value["staging_receipt_sha256"] = staging_sha
    approval.write_text(json.dumps(approval_value), encoding="utf-8")
    command[command.index("-ExpectedApprovalSha256") + 1] = _sha(approval)

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode != 0
    assert "preflight seal is invalid" in result.stderr
    assert not result_path.exists()


@pytest.mark.skipif(shutil.which("pwsh.exe") is None, reason="requires PowerShell 7")
def test_promotion_gate_refuses_mode_downgrade_from_release_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    command, result_path, staging = _fixture(tmp_path, monkeypatch)
    command[0] = shutil.which("pwsh.exe") or "pwsh.exe"
    bundle = Path(command[command.index("-BundlePath") + 1])
    release_path = bundle / "rootfs/srv/menhir/production/release/release.json"
    release = json.loads(release_path.read_text(encoding="utf-8"))
    release["deployment_class"] = "maintenance"
    release_path.write_text(json.dumps(release), encoding="utf-8")
    release_sha = _sha(release_path)
    bundle_sha = _tree_sha(bundle)
    command[command.index("-ExpectedReleaseSha256") + 1] = release_sha
    command[command.index("-ExpectedBundleSha256") + 1] = bundle_sha
    value = json.loads(staging.read_text(encoding="utf-8"))
    value["release_sha256"] = release_sha
    value["bundle_sha256"] = bundle_sha
    staging.write_text(json.dumps(value), encoding="utf-8")
    staging_sha = _sha(staging)
    command[command.index("-ExpectedStagingReceiptSha256") + 1] = staging_sha
    approval = Path(command[command.index("-Approval") + 1])
    approval_value = json.loads(approval.read_text(encoding="utf-8"))
    approval_value["release_sha256"] = release_sha
    approval_value["bundle_sha256"] = bundle_sha
    approval_value["staging_receipt_sha256"] = staging_sha
    approval.write_text(json.dumps(approval_value), encoding="utf-8")
    command[command.index("-ExpectedApprovalSha256") + 1] = _sha(approval)

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode != 0
    assert "mode differs from the immutable release authority" in result.stderr
    assert not result_path.exists()


def test_promotion_passes_approved_root_runner_to_operator_wrapper() -> None:
    text = WRAPPER.read_text(encoding="utf-8")

    invocation = text[text.index("& $operatorWrapper"):text.index("$powerShellSucceeded")]
    assert "-ExpectedRootRunnerSha256 $ExpectedRootRunnerSha256" in invocation
    assert "-ExpectedReleaseSha256 $ExpectedReleaseSha256" in invocation
    assert "-ExpectedIngressContainerId $expectedIngressContainerId" in invocation


def test_security_config_adopts_matching_root_receipt_before_upload() -> None:
    wrapper = WRAPPER.with_name("personal_security_config.ps1").read_text(encoding="utf-8")
    adoption = wrapper.index("$existingReceiptJson")
    credentials = wrapper.index("Get-Command docker-credential-desktop")
    deployment = wrapper.index("$remoteRunner deploy")
    assert adoption < credentials < deployment
    assert "$existingReceipt.candidate_release_sha256 -eq $ExpectedReleaseSha256" in wrapper
    assert "$existingReceipt.ingress_container_id_after -eq $ExpectedIngressContainerId" in wrapper
    assert "$existingReceiptJson," in wrapper
    assert "$receiptJson," in wrapper
    assert "($existingReceipt | ConvertTo-Json" not in wrapper
    assert "($receipt | ConvertTo-Json" not in wrapper
