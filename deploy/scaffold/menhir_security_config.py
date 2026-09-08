#!/usr/bin/env python3
"""Execute the bounded Menhir application-and-security-configuration transaction."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any

import menhir_app_only as app


Error = app.AppOnlyError
ROOT = app.ROOT
STATUS = app.STATUS
UPLOAD_ROOT = Path("/home/thron/.menhir-security-config-upload")
ACTIVE = STATUS / "security-config-active.json"
LAST = STATUS / "security-config-last.json"
OPERATIONS_SERVICE = "menhir-oauth-operations.service"
BUNDLE_ID = re.compile(r"[a-f0-9]{32}")

TARGETS = {
    "release.json": app.LIVE_RELEASE,
    "production.env": app.LIVE_ENV,
    "client-policy.json": app.LIVE_POLICY,
    "operations-policy.json": Path("/etc/yawn-vps/menhir-oauth-policy.json"),
    "oauth-public.pem": Path("/etc/yawn-vps/menhir-oauth-public.pem"),
}
DESTINATIONS = {
    "release.json": "/srv/menhir/production/release/release.json",
    "production.env": "/srv/menhir/production/release/production.env",
    "client-policy.json": "/srv/menhir/production/policy/client-policy.json",
    "operations-policy.json": "/etc/yawn-vps/menhir-oauth-policy.json",
    "oauth-public.pem": "/etc/yawn-vps/menhir-oauth-public.pem",
}
MODES = {
    "release.json": 0o400,
    "production.env": 0o400,
    "client-policy.json": 0o644,
    "operations-policy.json": 0o644,
    "oauth-public.pem": 0o644,
}
ALLOWED_ENV_CHANGES = app.ALLOWED_ENV_CHANGES | {"MENHIR_CLIENT_POLICY_DIGEST"}
ALLOWED_RENDERED_CHANGES = {
    "production_env_sha256", "policy_sha256", "operations_policy_sha256",
}
ALLOWED_CONFIG_DESTINATIONS = set(DESTINATIONS.values()) - {
    "/srv/menhir/production/release/release.json",
    "/etc/yawn-vps/menhir-oauth-public.pem",
}


def require_candidate_file(path: Path, label: str) -> None:
    app.require_upload(path, label)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise Error(f"{label} is unexpectedly large")


def validate_policy_files(bundle: Path, release: dict[str, Any]) -> None:
    client = app.strict_load(bundle / "client-policy.json")
    if client.get("version") not in {1, 2}:
        raise Error("client-policy.json schema mismatch")
    canonical = copy.deepcopy(client)
    declared = canonical.pop("canonical_digest", None)
    calculated = app.hashlib.sha256(json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()
    if declared != calculated:
        raise Error("client-policy.json canonical digest mismatch")
    operations = app.strict_load(bundle / "operations-policy.json")
    if operations.get("schema") != 1:
        raise Error("operations-policy.json schema mismatch")
    public = (bundle / "oauth-public.pem").read_text(encoding="ascii")
    if "-----BEGIN PUBLIC KEY-----" not in public or "PRIVATE KEY" in public:
        raise Error("OAuth public key is not a public PEM key")
    expected = {
        "client-policy.json": release.get("rendered", {}).get("policy_sha256"),
        "operations-policy.json": release.get("rendered", {}).get("operations_policy_sha256"),
        "oauth-public.pem": release.get("rendered", {}).get("oauth_public_key_sha256"),
        "production.env": release.get("rendered", {}).get("production_env_sha256"),
    }
    for name, digest in expected.items():
        if digest != app.sha256(bundle / name):
            raise Error(f"candidate {name} digest is not release-bound")


def validate_source_manifest(
    source: dict[str, Any], bundle: Path, release_sha: str, release_id: str,
) -> None:
    if set(source) != {"schema", "kind", "release_id", "release_sha256", "files"} \
            or source.get("schema") != 1 \
            or source.get("kind") != "menhir-release-install-bundle" \
            or source.get("release_id") != release_id \
            or source.get("release_sha256") != release_sha:
        raise Error("source install-bundle manifest binding mismatch")
    files = source.get("files")
    if not isinstance(files, dict):
        raise Error("source install-bundle files are missing")
    for name, destination in DESTINATIONS.items():
        row = files.get(destination)
        if not isinstance(row, dict) or row.get("sha256") != app.sha256(bundle / name):
            raise Error(f"source install-bundle does not bind {destination}")


def load_bundle(bundle_id: str, destination: Path | None = None) -> dict[str, Any]:
    if not BUNDLE_ID.fullmatch(bundle_id):
        raise Error("bundle id must be 32 lowercase hexadecimal characters")
    bundle = UPLOAD_ROOT / f"security-{bundle_id}"
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise Error("uploaded security-config bundle is missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise Error("uploaded security-config bundle must be private and owned by thron")
    names = set(TARGETS) | {"source-manifest.json", "docker-config.json"}
    for name in names:
        require_candidate_file(bundle / name, name)
    require_candidate_file(bundle / "security-config-manifest.json", "security-config-manifest.json")
    manifest = app.strict_load(bundle / "security-config-manifest.json")
    if set(manifest) != {"schema", "kind", "source_bundle_sha256", "files"} \
            or manifest.get("schema") != 1 \
            or manifest.get("kind") != "menhir-security-config-bundle":
        raise Error("security-config bundle manifest schema mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != names:
        raise Error("security-config bundle file set mismatch")
    for name in names:
        if files.get(name) != app.sha256(bundle / name):
            raise Error(f"security-config bundle digest mismatch: {name}")
    if manifest["source_bundle_sha256"] != app.sha256(bundle / "source-manifest.json"):
        raise Error("security-config source manifest digest mismatch")
    if destination is not None:
        destination.mkdir(parents=True, mode=0o700)
        os.chown(destination, 0, 0)
        for name in names | {"security-config-manifest.json"}:
            app.atomic_bytes(destination / name, (bundle / name).read_bytes(), 0o400)
        bundle = destination
    release = app.strict_load(bundle / "release.json")
    release_sha = app.sha256(bundle / "release.json")
    validate_source_manifest(
        app.strict_load(bundle / "source-manifest.json"), bundle, release_sha,
        str(release.get("release_id", "")),
    )
    validate_policy_files(bundle, release)
    return {
        "path": bundle, "release": release, "release_sha": release_sha,
        "env": app.parse_env(bundle / "production.env"),
    }


def comparable_artifact(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    result = copy.deepcopy(row)
    if result.get("kind") == "git":
        result.pop("commit", None)
    return result


def classify_release(
    live: dict[str, Any], candidate: dict[str, Any], live_sha: str,
    live_env: dict[str, str], candidate_env: dict[str, str], bundle: Path,
) -> dict[str, Any]:
    candidate_id = candidate.get("release_id")
    live_id = live.get("release_id")
    if not isinstance(candidate_id, str) or not app.ID.fullmatch(candidate_id) \
            or candidate_id == live_id:
        raise Error("security-config release identity is invalid or not new")
    if candidate.get("deployment_class") != "security-config" \
            or candidate.get("ingress_mode") != "cloudflared":
        raise Error("candidate is not Cloudflared security-config authority")
    for key in ("schema", "network", "deployment", "repo_remotes", "ingress_mode"):
        if candidate.get(key) != live.get(key):
            raise Error(f"protected release field differs: {key}")
    for name in ("yawn_deploy", "yawn_vps"):
        if candidate.get("repos", {}).get(name) != live.get("repos", {}).get(name):
            raise Error(f"security-config cannot change repository: {name}")
    for name in ("neo4j", "caddy", "base"):
        if candidate.get("images", {}).get(name) != live.get("images", {}).get(name):
            raise Error(f"security-config cannot change image: {name}")
    candidate_image = candidate.get("images", {}).get("menhir")
    if not isinstance(candidate_image, str) or not app.DIGEST.fullmatch(candidate_image):
        raise Error("candidate Menhir image digest is invalid")
    expected_rollback = {
        "prior_release_id": live_id,
        "prior_release_sha256": live_sha,
        "prior_images": {
            "menhir": live.get("images", {}).get("menhir"),
            "neo4j": live.get("images", {}).get("neo4j"),
            "caddy": live.get("images", {}).get("caddy"),
        },
    }
    rollback = candidate.get("rollback_anchors")
    if not isinstance(rollback, dict) or any(rollback.get(k) != v for k, v in expected_rollback.items()):
        raise Error("candidate rollback anchors do not bind the exact live release")
    if set(live_env) != set(candidate_env):
        raise Error("candidate production environment adds or removes keys")
    changed_env = sorted(key for key in live_env if live_env[key] != candidate_env[key])
    if not changed_env or not set(changed_env).issubset(ALLOWED_ENV_CHANGES):
        raise Error("protected production environment differs: " + ", ".join(changed_env))
    expected_env = {
        "MENHIR_RELEASE_ID": candidate_id,
        "MENHIR_RELEASE_COMMIT": candidate.get("repos", {}).get("menhir"),
        "MENHIR_CLIENT_POLICY_DIGEST": app.strict_load(
            bundle / "client-policy.json"
        ).get("canonical_digest"),
    }
    for key, value in expected_env.items():
        if candidate_env.get(key) != value:
            raise Error(f"candidate environment {key} is not release-bound")
    image_ref = candidate_env.get("MENHIR_IMAGE", "")
    live_image_ref = live_env.get("MENHIR_IMAGE", "")
    if not image_ref.endswith("@" + candidate_image) \
            or image_ref.rsplit("@", 1)[0] != live_image_ref.rsplit("@", 1)[0]:
        raise Error("candidate Menhir image reference is invalid")
    if candidate_env.get("NEO4J_IMAGE") != live_env.get("NEO4J_IMAGE"):
        raise Error("candidate changes the Neo4j image reference")
    for key in set(live.get("rendered", {})) | set(candidate.get("rendered", {})):
        if key not in ALLOWED_RENDERED_CHANGES \
                and candidate.get("rendered", {}).get(key) != live.get("rendered", {}).get(key):
            raise Error(f"protected rendered authority differs: {key}")
    live_secrets = live.get("secret_version_ids", {})
    candidate_secrets = candidate.get("secret_version_ids", {})
    for key in set(live_secrets) | set(candidate_secrets):
        if key != "client-policy" and candidate_secrets.get(key) != live_secrets.get(key):
            raise Error(f"security-config cannot rotate secret: {key}")
    policy_digest = app.strict_load(bundle / "client-policy.json").get("canonical_digest")
    if candidate_secrets.get("client-policy") != "sha256-" + str(policy_digest):
        raise Error("client-policy secret version is not bound to its canonical digest")
    live_artifacts = live.get("artifacts")
    candidate_artifacts = candidate.get("artifacts")
    if not isinstance(live_artifacts, dict) or not isinstance(candidate_artifacts, dict) \
            or set(live_artifacts) != set(candidate_artifacts):
        raise Error("release artifact authority differs")
    for destination, row in candidate_artifacts.items():
        prior = live_artifacts[destination]
        if destination in ALLOWED_CONFIG_DESTINATIONS:
            name = next(name for name, value in DESTINATIONS.items() if value == destination)
            if not isinstance(row, dict) or row.get("sha256") != app.sha256(bundle / name):
                raise Error(f"candidate artifact is not bound: {destination}")
        elif comparable_artifact(row) != comparable_artifact(prior):
            raise Error(f"protected artifact differs: {destination}")
    return {
        "classification": "security-config",
        "live_release_id": live_id,
        "candidate_release_id": candidate_id,
        "prior_image": live.get("images", {}).get("menhir"),
        "candidate_image": candidate_image,
        "changed_environment_keys": changed_env,
    }


def classify_bundle(bundle_id: str, destination: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    for path, label in ((app.LIVE_RELEASE, "live release"), (app.LIVE_ENV, "live environment")):
        app.require_root_file(path, label)
    bundle = load_bundle(bundle_id, destination)
    app.validate_release_file(bundle["path"] / "release.json")
    result = classify_release(
        app.strict_load(app.LIVE_RELEASE), bundle["release"], app.sha256(app.LIVE_RELEASE),
        app.parse_env(app.LIVE_ENV), bundle["env"], bundle["path"],
    )
    result["candidate_release_sha256"] = bundle["release_sha"]
    return bundle, result


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
    # Commit policy and gateway authority before replacing the app that consumes them.
    for name in ("client-policy.json", "operations-policy.json", "oauth-public.pem",
                 "production.env", "release.json"):
        source = root / f"{prefix}-{name}"
        app.require_root_file(source, f"{prefix} {name}")
        app.atomic_bytes(TARGETS[name], source.read_bytes(), MODES[name])
    app.run(["systemctl", "restart", OPERATIONS_SERVICE], 30)
    app.run(["systemctl", "is-active", "--quiet", OPERATIONS_SERVICE], 15)


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
) -> dict[str, Any]:
    runner_sha256 = app.require_runner_sha256(expected_runner_sha256, Path(__file__))
    if app.HEX64.fullmatch(expected_release_sha256) is None:
        raise Error("expected release SHA-256 is malformed")
    if app.HEX64.fullmatch(expected_ingress_container_id) is None:
        raise Error("expected Cloudflared container ID is malformed")
    lock = app.acquire_lock()
    transaction: dict[str, Any] | None = None
    try:
        app.require_no_incomplete_transactions()
        tx_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + bundle_id
        tx = STATUS / "security-config" / tx_id
        bundle, classification = classify_bundle(bundle_id, tx)
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


def recover() -> dict[str, Any]:
    if not ACTIVE.exists():
        raise Error("there is no incomplete security-config transaction")
    lock = app.acquire_lock()
    try:
        app.require_root_file(ACTIVE, "active security-config transaction")
        transaction = app.strict_load(ACTIVE)
        if transaction.get("kind") != "menhir-security-config-transaction":
            raise Error("active security-config transaction schema mismatch")
        app.require_runner_sha256(transaction.get("runner_sha256"), Path(__file__))
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
            _, value = classify_bundle(args.bundle_id)
        elif args.command == "deploy":
            value = deploy(
                args.bundle_id,
                args.expected_runner_sha256,
                args.expected_release_sha256,
                args.expected_ingress_container_id,
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
