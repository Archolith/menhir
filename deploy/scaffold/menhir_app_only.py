#!/usr/bin/env python3
"""Classify and execute bounded Menhir application-image-only releases."""

from __future__ import annotations

import datetime as dt
import hashlib  # menhir_security_config accesses app.hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - pure classifier tests run on Windows
    fcntl = None  # type: ignore[assignment]

# Siblings are imported by name because this runner is deployed as a bare script.
_SCAFFOLD_DIR = str(Path(__file__).resolve().parent)
if _SCAFFOLD_DIR not in sys.path:
    sys.path.append(_SCAFFOLD_DIR)

from menhir_app_only_authority import (
    _transaction_root, assert_app_only_live_equality, discard_unstarted_transaction, last_receipt,
    validate_active_transaction_authority, validate_promotion_authority,
    validate_transaction_authority,
)
from menhir_app_only_classify import (
    classify_bundle, classify_release, live_info, load_bundle, recompute_source_classification,
    validate_release_file, validate_source_manifest,
)
from menhir_app_only_cli import parser
from menhir_app_only_constants import (
    ADMISSION_LOCK, ACTIVE, ALLOWED_ENV_CHANGES, ALLOWED_RELEASE_SCALARS, APPROVAL_KEYS,
    APPROVAL_NAME, APP_ONLY_SOURCE_PATTERNS, BUNDLE_ID, COMPOSE, DIGEST, ENV_KEY, HEX64, ID, LAST,
    LIVE_ENV, LIVE_RELEASE, LIVE_POLICY, MAINTENANCE_ACTIVE, MUTATION_LOCK, PREFLIGHT_KEYS,
    PROBE_CLIENT_ID, ROOT, SCHEMA, SCAFFOLD, SECURITY_CONFIG_ACTIVE, STAGING_CHECKS, STAGING_KEYS,
    STAGING_RECEIPT_MAX_AGE, STAGING_RECEIPT_NAME, STATUS, UPLOAD_ROOT,
)
from menhir_app_only_core import (
    AppOnlyError, DeploymentLocks, atomic_bytes, atomic_json, composite_sha256,
    is_app_only_source_path, json_differences, now_iso, parse_env, parse_utc, require_root_file,
    require_upload, run, set_path, sha256, strict_load,
)
from menhir_app_only_runtime import (
    PROBE_TOKEN_SCRIPT, docker_pull, inspect_cloudflared, inspect_container, mcp_post,
    request_json, wait_app,
)
from menhir_app_only_transactions import finalize_transaction, restore_authority, write_stage


def acquire_lock() -> DeploymentLocks:
    if fcntl is None:
        raise AppOnlyError("POSIX file locking is unavailable")
    handles: list[Any] = []
    try:
        for path in (ADMISSION_LOCK, MUTATION_LOCK):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise
            handles.append(handle)
    except OSError as exc:
        for handle in reversed(handles):
            handle.close()
        raise AppOnlyError("deployment admission is held") from exc
    return DeploymentLocks(handles)


def require_no_incomplete_transactions() -> None:
    """Reject a new mutation while any deployment lane has unfinished state."""
    for path, kind in (
        (ACTIVE, "app-only"),
        (SECURITY_CONFIG_ACTIVE, "security-config"),
        (MAINTENANCE_ACTIVE, "maintenance"),
    ):
        if not path.exists():
            continue
        require_root_file(path, f"active {kind} transaction")
        stage = strict_load(path).get("stage")
        if kind != "maintenance" or stage != "complete":
            raise AppOnlyError(
                f"an incomplete {kind} transaction exists; recover it before deploying"
            )


def require_runner_sha256(expected: object, path: Path = Path(__file__)) -> str:
    if not isinstance(expected, str) or HEX64.fullmatch(expected) is None:
        raise AppOnlyError("expected root runner SHA-256 is malformed")
    actual = sha256(path)
    if actual != expected:
        raise AppOnlyError("root runner differs from the owner-approved authority")
    return actual


def replace_app(env_path: Path, image_digest: str, release_id: str, database_id: str) -> None:
    run([
        "docker", "compose", "--project-name", "menhir-prod", "--env-file", str(env_path),
        "--file", str(COMPOSE), "up", "-d", "--no-deps", "--force-recreate", "menhir",
    ], 120)
    wait_app(image_digest, release_id, database_id, 120)


def require_probe_policy() -> None:
    """Prove the short-lived acceptance identity has only the reviewed read surface."""

    require_root_file(LIVE_POLICY, "production client policy")
    policy = strict_load(LIVE_POLICY)
    clients = policy.get("clients")
    probe = clients.get(PROBE_CLIENT_ID) if isinstance(clients, dict) else None
    expected = {
        "label": PROBE_CLIENT_ID,
        "scopes": ["menhir:read"],
        "maximum_tier": "readonly",
        "namespace": "",
        "allowed_tools": ["recall_memories"],
    }
    if not isinstance(probe, dict) or any(probe.get(key) != value for key, value in expected.items()):
        raise AppOnlyError("production policy lacks the exact read-only menhir-deploy-probe identity")
    denied = probe.get("denied_tools")
    if not isinstance(denied, list) or not denied or "recall_memories" in denied:
        raise AppOnlyError("menhir-deploy-probe deny boundary is invalid")


def mint_probe_token() -> str:
    """Mint one 60-second policy-bound JWT in memory inside the running app."""

    require_probe_policy()
    token = run(
        ["docker", "exec", "-i", "menhir-prod-app", "python", "-"],
        15,
        input_bytes=PROBE_TOKEN_SCRIPT.encode("ascii"),
    )
    if token.count(".") != 2 or any(char.isspace() for char in token):
        raise AppOnlyError("deploy-probe token mint returned malformed output")
    return token


def accept_production(base: str, release_id: str, image_digest: str, database_id: str) -> None:
    ready = request_json(base.rstrip("/") + "/readyz")
    if ready.get("status") != "ready" or ready.get("mode") != "production" \
            or ready.get("mutation_fence") is not False:
        raise AppOnlyError("public readiness is not writable production mode")
    request_json(base.rstrip("/") + "/.well-known/jwks.json")
    request_json(base.rstrip("/") + "/livez")
    app = inspect_container("menhir-prod-app")
    if app.get("State", {}).get("Health", {}).get("Status") != "healthy" \
            or not str(app.get("Config", {}).get("Image", "")).endswith("@" + image_digest):
        raise AppOnlyError("accepted runtime is not the candidate image")
    if inspect_container("menhir-prod-neo4j").get("Id") != database_id:
        raise AppOnlyError("Neo4j changed during app-only replacement")
    token = mint_probe_token()
    response, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "menhir-app-only-accept", "version": "1"}},
    })
    if "result" not in response:
        raise AppOnlyError("MCP initialize lacked a result")
    _, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, session)
    response, session = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, session)
    tools = {row.get("name") for row in response.get("result", {}).get("tools", [])}
    if "recall_memories" not in tools:
        raise AppOnlyError("acceptance identity cannot see recall_memories")
    response, _ = mcp_post(base, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recall_memories", "arguments": {
            "query": "Menhir app-only production acceptance", "limit": 1}},
    }, session)
    if response.get("result", {}).get("isError") is True:
        raise AppOnlyError("read-only recall acceptance returned a tool error")


def rollback(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    restore_authority(transaction, False)
    if transaction.get("stage") in {"replacing", "running_candidate"}:
        replace_app(
            tx / "prior-production.env", transaction["prior_image"],
            transaction["prior_release_id"], transaction["database_container_id"],
        )
    write_stage(transaction, "rolled_back", completed_utc=now_iso())
    finalize_transaction(transaction)


def rollforward(transaction: dict[str, Any]) -> None:
    tx = Path(transaction["transaction_root"])
    replace_app(
        tx / "candidate-production.env", transaction["candidate_image"],
        transaction["candidate_release_id"], transaction["database_container_id"],
    )
    restore_authority(transaction, True)
    accepted_app = inspect_container("menhir-prod-app")
    accepted_database = inspect_container("menhir-prod-neo4j")
    accepted_ingress = inspect_cloudflared()
    if accepted_database.get("Id") != transaction["database_container_id"]:
        raise AppOnlyError("Neo4j changed during app-only recovery")
    if accepted_ingress.get("Id") != transaction["ingress_container_id"]:
        raise AppOnlyError("Cloudflared changed during app-only recovery")
    write_stage(
        transaction, "complete", completed_utc=now_iso(), recovered=True,
        result="passed", candidate_app_container_id=accepted_app.get("Id"),
        database_container_id_after=accepted_database.get("Id"),
        ingress_container_id_after=accepted_ingress.get("Id"),
    )
    run([str(SCAFFOLD), "verify", "--app-only"], 30)
    finalize_transaction(transaction)


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
    runner_sha256 = require_runner_sha256(expected_runner_sha256)
    if HEX64.fullmatch(expected_release_sha256) is None:
        raise AppOnlyError("expected release SHA-256 is malformed")
    if HEX64.fullmatch(expected_ingress_container_id) is None:
        raise AppOnlyError("expected Cloudflared container ID is malformed")
    lock = acquire_lock()
    transaction: dict[str, Any] | None = None
    try:
        require_no_incomplete_transactions()
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        uploaded = load_bundle(bundle_id)
        authority = validate_promotion_authority(
            uploaded["path"], uploaded["release"], deployment_class="app-only",
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
        tx = STATUS / "app-only" / tx_id
        bundle, classification = classify_bundle(bundle_id, tx)
        validate_promotion_authority(
            bundle["path"], bundle["release"], deployment_class="app-only",
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
            discard_unstarted_transaction(tx, "app-only")
            raise AppOnlyError("uploaded release differs from the owner-approved authority")
        require_root_file(LIVE_RELEASE, "live release")
        require_root_file(LIVE_ENV, "live production environment")
        atomic_bytes(tx / "prior-release.json", LIVE_RELEASE.read_bytes(), 0o400)
        atomic_bytes(tx / "prior-production.env", LIVE_ENV.read_bytes(), 0o400)
        candidate_env = tx / "production.env"
        candidate_release = tx / "release.json"
        os.replace(candidate_env, tx / "candidate-production.env")
        os.replace(candidate_release, tx / "candidate-release.json")
        app = inspect_container("menhir-prod-app")
        database = inspect_container("menhir-prod-neo4j")
        ingress = inspect_cloudflared()
        if ingress.get("Id") != expected_ingress_container_id:
            raise AppOnlyError("Cloudflared changed since the approved staging preflight")
        transaction = {
            "schema": 1,
            "kind": "menhir-app-only-transaction",
            "runner_sha256": runner_sha256,
            "transaction_id": tx_id,
            "transaction_root": str(tx),
            "bundle_id": bundle_id,
            "prior_release_id": classification["live_release_id"],
            "candidate_release_id": classification["candidate_release_id"],
            "prior_release_sha256": sha256(tx / "prior-release.json"),
            "candidate_release_sha256": classification["candidate_release_sha256"],
            "prior_image": classification["prior_image"],
            "candidate_image": classification["candidate_image"],
            "prior_app_container_id": app.get("Id"),
            "database_container_id": database.get("Id"),
            "ingress_container_id": ingress.get("Id"),
            **authority,
            "started_utc": now_iso(),
        }
        write_stage(transaction, "classified")
        upload = UPLOAD_ROOT / f"app-{bundle_id}"
        image_ref = bundle["env"]["MENHIR_IMAGE"]
        docker_pull(image_ref, upload / "docker-config.json", tx / "docker-config")
        write_stage(transaction, "pulled")
        write_stage(transaction, "replacing")
        replace_app(
            tx / "candidate-production.env", classification["candidate_image"],
            classification["candidate_release_id"], str(database.get("Id")),
        )
        write_stage(transaction, "running_candidate")
        accept_production(
            bundle["env"]["MENHIR_PUBLIC_BASE_URL"], classification["candidate_release_id"],
            classification["candidate_image"], str(database.get("Id")),
        )
        accepted_app = inspect_container("menhir-prod-app")
        accepted_database = inspect_container("menhir-prod-neo4j")
        accepted_ingress = inspect_cloudflared()
        if accepted_database.get("Id") != transaction["database_container_id"]:
            raise AppOnlyError("Neo4j changed during app-only transaction")
        if accepted_ingress.get("Id") != transaction["ingress_container_id"]:
            raise AppOnlyError("Cloudflared changed during app-only transaction")
        write_stage(
            transaction, "accepted", accepted_utc=now_iso(),
            candidate_app_container_id=accepted_app.get("Id"),
            database_container_id_after=accepted_database.get("Id"),
            ingress_container_id_after=accepted_ingress.get("Id"),
        )
        restore_authority(transaction, True)
        write_stage(transaction, "complete", completed_utc=now_iso(), result="passed")
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        finalize_transaction(transaction)
        return transaction
    except Exception as exc:
        if transaction is not None and transaction.get("stage") not in {"accepted", "complete"}:
            try:
                rollback(transaction)
            except Exception as rollback_exc:
                raise AppOnlyError(f"deployment failed ({exc}); automatic rollback failed ({rollback_exc})") from rollback_exc
        elif transaction is not None and transaction.get("stage") == "accepted":
            try:
                rollforward(transaction)
            except Exception as recovery_exc:
                raise AppOnlyError(f"accepted deployment could not commit or recover: {recovery_exc}") from recovery_exc
        if isinstance(exc, AppOnlyError):
            raise
        raise AppOnlyError(str(exc)) from exc
    finally:
        lock.close()


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
    runner_sha256 = require_runner_sha256(expected_runner_sha256)
    lock = acquire_lock()
    try:
        require_no_incomplete_transactions()
        transaction = last_receipt()
        validate_transaction_authority(
            transaction,
            lane="app-only",
            expected_kind="menhir-app-only-transaction",
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
        assert_app_only_live_equality(transaction)
        return transaction
    finally:
        lock.close()


def recover() -> dict[str, Any]:
    if not ACTIVE.exists():
        raise AppOnlyError("there is no incomplete app-only transaction")
    lock = acquire_lock()
    try:
        require_root_file(ACTIVE, "active app-only transaction")
        transaction = strict_load(ACTIVE)
        if transaction.get("kind") != "menhir-app-only-transaction":
            raise AppOnlyError("active app-only transaction schema mismatch")
        runner_sha256 = require_runner_sha256(transaction.get("runner_sha256"))
        validate_active_transaction_authority(
            transaction,
            lane="app-only",
            expected_kind="menhir-app-only-transaction",
            expected_runner_sha256=runner_sha256,
        )
        if transaction.get("stage") == "complete":
            finalize_transaction(transaction)
        elif transaction.get("stage") == "accepted":
            rollforward(transaction)
        else:
            rollback(transaction)
        return transaction
    finally:
        lock.close()


def check_live() -> dict[str, Any]:
    lock = acquire_lock()
    try:
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        return accept_current()
    finally:
        lock.close()


def accept_current() -> dict[str, Any]:
    """Accept the current release while a maintenance stage journal is active."""

    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    release = strict_load(LIVE_RELEASE)
    environment = parse_env(LIVE_ENV)
    database_id = str(inspect_container("menhir-prod-neo4j").get("Id"))
    accept_production(
        environment["MENHIR_PUBLIC_BASE_URL"], release["release_id"],
        release["images"]["menhir"], database_id,
    )
    return {
        "status": "accepted",
        "release_id": release["release_id"],
        "menhir_image": release["images"]["menhir"],
        "checked_utc": now_iso(),
    }


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("REFUSED: app-only authority must run as root", file=sys.stderr)
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
        elif args.command == "live":
            value = live_info()
        elif args.command == "check":
            value = check_live()
        elif args.command == "receipt":
            value = last_receipt()
        else:
            value = accept_current()
    except AppOnlyError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
