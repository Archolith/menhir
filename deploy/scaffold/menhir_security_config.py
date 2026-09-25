#!/usr/bin/env python3
"""Execute the bounded Menhir application-and-security-configuration transaction."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

# Siblings are imported by name because this runner is deployed as a bare script.
_SCAFFOLD_DIR = str(Path(__file__).resolve().parent)
if _SCAFFOLD_DIR not in sys.path:
    sys.path.append(_SCAFFOLD_DIR)

import menhir_app_only as app

from menhir_security_config_bundle import (
    load_bundle, require_candidate_file, validate_policy_files, validate_source_manifest,
)
from menhir_security_config_classify import (
    classify_bundle, classify_release, comparable_artifact,
)
from menhir_security_config_constants import (
    ADMISSION_LOCK, ACTIVE, ALLOWED_CONFIG_DESTINATIONS, ALLOWED_ENV_CHANGES,
    ALLOWED_RENDERED_CHANGES, BUNDLE_ID, DESTINATIONS, Error, LAST, MODES, ROOT, STATUS,
    TARGETS, UPLOAD_ROOT,
)


def runner_authority_sha256() -> str:
    return app.composite_sha256((
        ("menhir_app_only.py", Path(app.__file__).resolve()),
        ("menhir_security_config.py", Path(__file__).resolve()),
    ))


def require_runner_authority(expected: object) -> str:
    if not isinstance(expected, str) or app.HEX64.fullmatch(expected) is None:
        raise Error("expected root runner SHA-256 is malformed")
    actual = runner_authority_sha256()
    if actual != expected:
        raise Error("root runner composite differs from the owner-approved authority")
    return actual


def acquire_security_config_admission() -> app.DeploymentLocks:
    """Acquire the same cross-lane authority used by app-only and maintenance."""
    if ADMISSION_LOCK != Path("/run/lock/menhir-production-admission.lock"):
        raise Error("security-config admission authority is inconsistent")
    return app.acquire_lock()


def write_stage(transaction: dict[str, Any], stage: str, **extra: Any) -> None:
    transaction.update(extra)
    transaction["stage"] = stage
    transaction["updated_utc"] = app.now_iso()
    app.atomic_json(ACTIVE, transaction)


def finalize(transaction: dict[str, Any]) -> None:
    app.atomic_json(LAST, transaction)
    try:
        ACTIVE.unlink()
    except FileNotFoundError:
        pass


def install_files(root: Path, prefix: str) -> None:
    # Commit policy before replacing the app that consumes it.
    for name in ("client-policy.json", "production.env", "release.json"):
        source = root / f"{prefix}-{name}"
        app.require_root_file(source, f"{prefix} {name}")
        app.atomic_bytes(TARGETS[name], source.read_bytes(), MODES[name])


def assert_unchanged(transaction: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    running_app = app.inspect_container("menhir-prod-app")
    database = app.inspect_container("menhir-prod-neo4j")
    ingress = app.inspect_cloudflared()
    if database.get("Id") != transaction["database_container_id"]:
        raise Error("Neo4j changed during security-config transaction")
    if ingress.get("Id") != transaction["ingress_container_id"]:
        raise Error("Cloudflared changed during security-config transaction")
    return running_app, database, ingress


def apply_candidate(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    install_files(tx, "candidate")
    app.replace_app(
        tx / "candidate-production.env", transaction["candidate_image"],
        transaction["candidate_release_id"], transaction["database_container_id"],
    )
    candidate_env = app.parse_env(tx / "candidate-production.env")
    app.accept_production(
        candidate_env["MENHIR_PUBLIC_BASE_URL"], transaction["candidate_release_id"],
        transaction["candidate_image"], transaction["database_container_id"],
    )


def rollback(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    install_files(tx, "prior")
    app.replace_app(
        tx / "prior-production.env", transaction["prior_image"],
        transaction["prior_release_id"], transaction["database_container_id"],
    )
    write_stage(transaction, "rolled_back", result="failed", completed_utc=app.now_iso())
    finalize(transaction)


def deploy(
    bundle_id: str,
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_ingress_container_id: str,
    expected_bundle_sha256: str = "",
    expected_staging_receipt_sha256: str = "",
    expected_approval_sha256: str = "",
    promotion_attempt_id: str = "",
    approved_utc: str = "",
    promotion_started_utc: str = "",
) -> dict[str, Any]:
    runner_sha256 = require_runner_authority(expected_runner_sha256)
    if app.HEX64.fullmatch(expected_release_sha256) is None:
        raise Error("expected release SHA-256 is malformed")
    if app.HEX64.fullmatch(expected_ingress_container_id) is None:
        raise Error("expected Cloudflared container ID is malformed")
    lock = acquire_security_config_admission()
    transaction: dict[str, Any] | None = None
    try:
        app.require_no_incomplete_transactions()
        uploaded = load_bundle(bundle_id)
        authority = app.validate_promotion_authority(
            uploaded["path"], uploaded["release"],
            deployment_class="security-config",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        tx_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + bundle_id
        tx = STATUS / "security-config" / tx_id
        bundle, classification = classify_bundle(bundle_id, tx)
        app.validate_promotion_authority(
            bundle["path"], bundle["release"],
            deployment_class="security-config",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        if classification["candidate_release_sha256"] != expected_release_sha256:
            app.discard_unstarted_transaction(
                tx, "security-config", status_root=STATUS,
            )
            raise Error("uploaded release differs from the owner-approved authority")
        for name, target in TARGETS.items():
            app.require_root_file(target, f"live {name}")
            app.atomic_bytes(tx / f"prior-{name}", target.read_bytes(), 0o400)
            candidate = tx / name
            os.replace(candidate, tx / f"candidate-{name}")
        before_app = app.inspect_container("menhir-prod-app")
        database = app.inspect_container("menhir-prod-neo4j")
        ingress = app.inspect_cloudflared()
        if ingress.get("Id") != expected_ingress_container_id:
            raise Error("Cloudflared changed since the approved staging preflight")
        transaction = {
            "schema": 1,
            "kind": "menhir-security-config-transaction",
            "runner_sha256": runner_sha256,
            "transaction_id": tx_id,
            "transaction_root": str(tx),
            "bundle_id": bundle_id,
            "prior_release_id": classification["live_release_id"],
            "candidate_release_id": classification["candidate_release_id"],
            "prior_release_sha256": app.sha256(tx / "prior-release.json"),
            "candidate_release_sha256": classification["candidate_release_sha256"],
            "prior_image": classification["prior_image"],
            "candidate_image": classification["candidate_image"],
            "prior_app_container_id": before_app.get("Id"),
            "database_container_id": database.get("Id"),
            "ingress_container_id": ingress.get("Id"),
            **authority,
            "started_utc": app.now_iso(),
        }
        write_stage(transaction, "classified")
        image_ref = bundle["env"]["MENHIR_IMAGE"]
        app.docker_pull(image_ref, tx / "docker-config.json", tx / "docker-config")
        write_stage(transaction, "pulled")
        write_stage(transaction, "applying")
        apply_candidate(transaction)
        current_app, current_database, current_ingress = assert_unchanged(transaction)
        write_stage(
            transaction, "complete", result="passed", completed_utc=app.now_iso(),
            candidate_app_container_id=current_app.get("Id"),
            database_container_id_after=current_database.get("Id"),
            ingress_container_id_after=current_ingress.get("Id"),
        )
        finalize(transaction)
        return transaction
    except Exception as exc:
        if transaction is not None and transaction.get("stage") in {"applying"}:
            try:
                rollback(transaction)
            except Exception as rollback_exc:
                raise Error(
                    f"security-config failed ({exc}); rollback failed ({rollback_exc})"
                ) from rollback_exc
        if isinstance(exc, Error):
            raise
        raise Error(str(exc)) from exc
    finally:
        lock.close()


def assert_live_equality(transaction: dict[str, Any]) -> None:
    root = app._transaction_root(transaction, "security-config")
    for name, target in TARGETS.items():
        app.require_root_file(target, f"live {name}")
        candidate = root / f"candidate-{name}"
        app.require_root_file(candidate, f"transaction candidate {name}")
        if app.sha256(target) != app.sha256(candidate):
            raise Error(f"completed security-config receipt no longer matches live {name}")
    current_app, current_database, current_ingress = assert_unchanged(transaction)
    image_ref = str(current_app.get("Config", {}).get("Image", ""))
    if current_app.get("Id") != transaction.get("candidate_app_container_id") \
            or not image_ref.endswith("@" + str(transaction.get("candidate_image"))) \
            or current_database.get("Id") != transaction.get("database_container_id_after") \
            or current_ingress.get("Id") != transaction.get("ingress_container_id_after"):
        raise Error("completed security-config receipt no longer matches live containers")


def adopt(
    expected_runner_sha256: str,
    expected_release_sha256: str,
    expected_ingress_container_id: str,
    expected_bundle_sha256: str,
    expected_staging_receipt_sha256: str,
    expected_approval_sha256: str,
    promotion_attempt_id: str,
    approved_utc: str,
    promotion_started_utc: str,
) -> dict[str, Any]:
    runner_sha256 = require_runner_authority(expected_runner_sha256)
    lock = acquire_security_config_admission()
    try:
        app.require_no_incomplete_transactions()
        transaction = last_receipt()
        app.validate_transaction_authority(
            transaction,
            lane="security-config",
            expected_kind="menhir-security-config-transaction",
            expected_runner_sha256=runner_sha256,
            expected_release_sha256=expected_release_sha256,
            expected_bundle_sha256=expected_bundle_sha256,
            expected_staging_receipt_sha256=expected_staging_receipt_sha256,
            expected_approval_sha256=expected_approval_sha256,
            expected_ingress_container_id=expected_ingress_container_id,
            promotion_attempt_id=promotion_attempt_id,
            approved_utc=approved_utc,
            promotion_started_utc=promotion_started_utc,
        )
        assert_live_equality(transaction)
        return transaction
    finally:
        lock.close()


def recover() -> dict[str, Any]:
    if not ACTIVE.exists():
        raise Error("there is no incomplete security-config transaction")
    lock = acquire_security_config_admission()
    try:
        app.require_root_file(ACTIVE, "active security-config transaction")
        transaction = app.strict_load(ACTIVE)
        if transaction.get("kind") != "menhir-security-config-transaction":
            raise Error("active security-config transaction schema mismatch")
        runner_sha256 = require_runner_authority(transaction.get("runner_sha256"))
        app.validate_active_transaction_authority(
            transaction,
            lane="security-config",
            expected_kind="menhir-security-config-transaction",
            expected_runner_sha256=runner_sha256,
        )
        if transaction.get("stage") == "complete":
            finalize(transaction)
        else:
            rollback(transaction)
        return transaction
    finally:
        lock.close()


def last_receipt() -> dict[str, Any]:
    app.require_root_file(LAST, "last security-config transaction receipt")
    value = app.strict_load(LAST)
    if value.get("schema") != 1 or value.get("kind") != "menhir-security-config-transaction" \
            or value.get("stage") != "complete" or value.get("result") != "passed":
        raise Error("last security-config transaction receipt is not complete")
    if value.get("database_container_id") != value.get("database_container_id_after"):
        raise Error("last security-config receipt does not prove unchanged Neo4j")
    if value.get("ingress_container_id") != value.get("ingress_container_id_after"):
        raise Error("last security-config receipt does not prove unchanged Cloudflared")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("bundle_id")
    deploy_command = commands.add_parser("deploy")
    deploy_command.add_argument("bundle_id")
    deploy_command.add_argument("expected_runner_sha256")
    deploy_command.add_argument("expected_release_sha256")
    deploy_command.add_argument("expected_ingress_container_id")
    for name in (
        "expected_bundle_sha256", "expected_staging_receipt_sha256",
        "expected_approval_sha256", "promotion_attempt_id", "approved_utc",
        "promotion_started_utc",
    ):
        deploy_command.add_argument(name)
    adopt_command = commands.add_parser("adopt")
    for name in (
        "expected_runner_sha256", "expected_release_sha256",
        "expected_ingress_container_id", "expected_bundle_sha256",
        "expected_staging_receipt_sha256", "expected_approval_sha256",
        "promotion_attempt_id", "approved_utc", "promotion_started_utc",
    ):
        adopt_command.add_argument(name)
    commands.add_parser("recover")
    commands.add_parser("receipt")
    return result


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("REFUSED: security-config authority must run as root", file=sys.stderr)
        return 1
    args = parser().parse_args(argv)
    try:
        if args.command == "classify":
            _, value = classify_bundle(args.bundle_id, require_authority=False)
        elif args.command == "deploy":
            value = deploy(
                args.bundle_id,
                args.expected_runner_sha256,
                args.expected_release_sha256,
                args.expected_ingress_container_id,
                args.expected_bundle_sha256,
                args.expected_staging_receipt_sha256,
                args.expected_approval_sha256,
                args.promotion_attempt_id,
                args.approved_utc,
                args.promotion_started_utc,
            )
        elif args.command == "adopt":
            value = adopt(
                args.expected_runner_sha256,
                args.expected_release_sha256,
                args.expected_ingress_container_id,
                args.expected_bundle_sha256,
                args.expected_staging_receipt_sha256,
                args.expected_approval_sha256,
                args.promotion_attempt_id,
                args.approved_utc,
                args.promotion_started_utc,
            )
        elif args.command == "recover":
            value = recover()
        else:
            value = last_receipt()
    except Error as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
