#!/usr/bin/env python3
"""Classify and execute bounded Menhir application-image-only releases."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - pure classifier tests run on Windows
    fcntl = None  # type: ignore[assignment]


ROOT = Path("/srv/menhir/production")
STATUS = Path("/var/lib/menhir-production")
UPLOAD_ROOT = Path("/home/thron/.menhir-app-only-upload")
COMPOSE = ROOT / "deploy/docker-compose.production.yml"
LIVE_RELEASE = ROOT / "release/release.json"
LIVE_ENV = ROOT / "release/production.env"
LIVE_POLICY = ROOT / "policy/client-policy.json"
SCHEMA = ROOT / "bin/menhir_schema.py"
SCAFFOLD = Path("/srv/menhir/scaffold/bin/menhir_scaffold.py")
LOCK = Path("/run/lock/menhir-production.lock")
ACTIVE = STATUS / "app-only-active.json"
LAST = STATUS / "app-only-last.json"
PROBE_CLIENT_ID = "menhir-deploy-probe"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,255}")
BUNDLE_ID = re.compile(r"[a-f0-9]{32}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
HEX64 = re.compile(r"[0-9a-f]{64}")
ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]*")
ALLOWED_ENV_CHANGES = {"MENHIR_IMAGE", "MENHIR_RELEASE_COMMIT", "MENHIR_RELEASE_ID"}
ALLOWED_RELEASE_SCALARS = (
    ("release_id",),
    ("repos", "menhir"),
    ("images", "menhir"),
    ("provenance_sha256",),
    ("sbom_sha256",),
    ("scan_evidence_sha256",),
    ("wheel_manifest_sha256",),
    ("dockerfile_wheel_manifest_sha256",),
    ("rendered", "production_env_sha256"),
)


class AppOnlyError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def strict_load(path: Path) -> dict[str, Any]:
    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AppOnlyError(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=hook)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AppOnlyError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AppOnlyError(f"JSON root must be an object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_root_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing: {path}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise AppOnlyError(f"{label} must be a regular non-symlink file")
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
        raise AppOnlyError(f"{label} must be root-owned and not group/other writable")


def require_upload(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AppOnlyError(f"{label} is missing") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != 1000:
        raise AppOnlyError(f"{label} must be a regular non-symlink file owned by thron")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError(f"{label} must have mode 0600 or stricter")


def run(command: list[str], timeout: int, *, input_bytes: bytes | None = None) -> str:
    try:
        result = subprocess.run(
            command, input=input_bytes, capture_output=True, check=False, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AppOnlyError(f"command failed: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-1000:].strip()
        raise AppOnlyError(f"command exited {result.returncode}: {' '.join(command)}: {stderr}")
    return result.stdout.decode("utf-8", "replace").strip()


def atomic_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, 0, 0)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("ascii"),
        0o400,
    )


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AppOnlyError(f"cannot read production environment: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line or line.startswith("#") or "=" not in line:
            raise AppOnlyError(f"production environment line {number} is not KEY=VALUE")
        key, value = line.split("=", 1)
        if not ENV_KEY.fullmatch(key) or key in result:
            raise AppOnlyError(f"production environment key is invalid or duplicated: {key}")
        if not value or any(char in value for char in "\r\n\x00"):
            raise AppOnlyError(f"production environment value is invalid: {key}")
        result[key] = value
    return result


def set_path(value: dict[str, Any], path: tuple[str, ...], replacement: Any) -> None:
    target: Any = value
    for key in path[:-1]:
        if not isinstance(target, dict) or key not in target:
            raise AppOnlyError("release is missing required field: " + "/".join(path))
        target = target[key]
    if not isinstance(target, dict) or path[-1] not in target:
        raise AppOnlyError("release is missing required field: " + "/".join(path))
    target[path[-1]] = replacement


def json_differences(left: Any, right: Any, prefix: str = "") -> list[str]:
    if type(left) is not type(right):
        return [prefix or "/"]
    if isinstance(left, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            path = f"{prefix}/{key}"
            if key not in left or key not in right:
                differences.append(path)
            else:
                differences.extend(json_differences(left[key], right[key], path))
        return differences
    if isinstance(left, list):
        return [] if left == right else [prefix or "/"]
    return [] if left == right else [prefix or "/"]


def classify_release(
    live: dict[str, Any], candidate: dict[str, Any], live_sha: str,
    live_env: dict[str, str], candidate_env: dict[str, str], candidate_env_sha: str,
) -> dict[str, Any]:
    live_id = live.get("release_id")
    candidate_id = candidate.get("release_id")
    if not isinstance(live_id, str) or not isinstance(candidate_id, str) or not ID.fullmatch(candidate_id):
        raise AppOnlyError("release identity is invalid")
    if candidate_id == live_id:
        raise AppOnlyError("changed app image must use a new immutable release_id")
    live_image = live.get("images", {}).get("menhir")
    candidate_image = candidate.get("images", {}).get("menhir")
    if not isinstance(candidate_image, str) or not DIGEST.fullmatch(candidate_image):
        raise AppOnlyError("candidate Menhir image digest is invalid")
    if candidate_image == live_image:
        raise AppOnlyError("app-only release does not change the Menhir image")

    rollback = candidate.get("rollback_anchors")
    expected_rollback = {
        "prior_release_id": live_id,
        "prior_release_sha256": live_sha,
        "prior_images": {
            "menhir": live_image,
            "neo4j": live.get("images", {}).get("neo4j"),
            "caddy": live.get("images", {}).get("caddy"),
        },
    }
    if not isinstance(rollback, dict) or any(
        rollback.get(key) != value for key, value in expected_rollback.items()
    ):
        raise AppOnlyError("candidate rollback anchors do not bind the exact live release")

    if set(live_env) != set(candidate_env):
        raise AppOnlyError("candidate production environment adds or removes keys")
    changed_env = sorted(key for key in live_env if live_env[key] != candidate_env[key])
    if not changed_env or not set(changed_env).issubset(ALLOWED_ENV_CHANGES):
        raise AppOnlyError("protected production environment differs: " + ", ".join(changed_env))
    expected_env = {
        "MENHIR_RELEASE_ID": candidate_id,
        "MENHIR_RELEASE_COMMIT": candidate.get("repos", {}).get("menhir"),
    }
    for key, value in expected_env.items():
        if candidate_env.get(key) != value:
            raise AppOnlyError(f"candidate environment {key} is not release-bound")
    image_ref = candidate_env.get("MENHIR_IMAGE", "")
    if not image_ref.endswith("@" + candidate_image):
        raise AppOnlyError("candidate MENHIR_IMAGE is not bound to the release digest")
    live_image_ref = live_env.get("MENHIR_IMAGE", "")
    if image_ref.rsplit("@", 1)[0] != live_image_ref.rsplit("@", 1)[0]:
        raise AppOnlyError("candidate changes the Menhir image repository")
    if candidate_env.get("NEO4J_IMAGE") != live_env.get("NEO4J_IMAGE"):
        raise AppOnlyError("candidate changes the Neo4j image reference")
    if candidate.get("rendered", {}).get("production_env_sha256") != candidate_env_sha:
        raise AppOnlyError("candidate production.env digest is not release-bound")

    normalized = copy.deepcopy(candidate)
    for path in ALLOWED_RELEASE_SCALARS:
        target: Any = live
        for key in path:
            target = target[key]
        set_path(normalized, path, target)
    normalized["security_review"] = copy.deepcopy(live.get("security_review"))
    normalized["rollback_anchors"] = copy.deepcopy(live.get("rollback_anchors"))

    live_artifacts = live.get("artifacts")
    candidate_artifacts = normalized.get("artifacts")
    if not isinstance(live_artifacts, dict) or not isinstance(candidate_artifacts, dict):
        raise AppOnlyError("release artifact authority is missing")
    env_artifact = "/srv/menhir/production/release/production.env"
    if env_artifact not in candidate_artifacts or env_artifact not in live_artifacts:
        raise AppOnlyError("production.env artifact authority is missing")
    candidate_artifacts[env_artifact] = copy.deepcopy(live_artifacts[env_artifact])
    for path, row in candidate_artifacts.items():
        prior = live_artifacts.get(path)
        if not isinstance(row, dict) or not isinstance(prior, dict):
            continue
        if row.get("repository") == "menhir" and prior.get("repository") == "menhir":
            without_commit = {key: value for key, value in row.items() if key != "commit"}
            prior_without_commit = {key: value for key, value in prior.items() if key != "commit"}
            if without_commit == prior_without_commit and "commit" in row and "commit" in prior:
                row["commit"] = prior["commit"]

    differences = json_differences(live, normalized)
    if differences:
        raise AppOnlyError(
            "protected release surfaces differ: " + ", ".join(differences[:12])
        )
    return {
        "classification": "app-only",
        "live_release_id": live_id,
        "candidate_release_id": candidate_id,
        "prior_image": live_image,
        "candidate_image": candidate_image,
        "changed_environment_keys": changed_env,
    }


def validate_source_manifest(
    manifest: dict[str, Any], release_sha: str, env_sha: str, candidate_id: str,
) -> None:
    if set(manifest) != {"schema", "kind", "release_id", "release_sha256", "files"}:
        raise AppOnlyError("source install-bundle manifest keys mismatch")
    if manifest.get("schema") != 1 or manifest.get("kind") != "menhir-release-install-bundle":
        raise AppOnlyError("source install-bundle manifest schema mismatch")
    if manifest.get("release_id") != candidate_id or manifest.get("release_sha256") != release_sha:
        raise AppOnlyError("source install-bundle release binding mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise AppOnlyError("source install-bundle files are missing")
    required = {
        "/srv/menhir/production/release/release.json": release_sha,
        "/srv/menhir/production/release/production.env": env_sha,
    }
    for path, digest in required.items():
        row = files.get(path)
        if not isinstance(row, dict) or row.get("sha256") != digest:
            raise AppOnlyError(f"source install-bundle does not bind {path}")


def load_bundle(bundle_id: str, destination: Path | None = None) -> dict[str, Any]:
    if not BUNDLE_ID.fullmatch(bundle_id):
        raise AppOnlyError("bundle id must be 32 lowercase hexadecimal characters")
    bundle = UPLOAD_ROOT / f"app-{bundle_id}"
    try:
        info = bundle.lstat()
    except OSError as exc:
        raise AppOnlyError("uploaded app-only bundle is missing") from exc
    if bundle.is_symlink() or not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 \
            or stat.S_IMODE(info.st_mode) & 0o077:
        raise AppOnlyError("uploaded app-only bundle must be a private directory owned by thron")
    names = (
        "app-only-manifest.json", "release.json", "production.env",
        "source-manifest.json", "classification-evidence.json",
    )
    for name in names:
        require_upload(bundle / name, name)
    manifest = strict_load(bundle / "app-only-manifest.json")
    if set(manifest) != {"schema", "kind", "source_bundle_sha256", "files"} \
            or manifest.get("schema") != 1 or manifest.get("kind") != "menhir-app-only-bundle":
        raise AppOnlyError("app-only bundle manifest schema mismatch")
    expected_files = set(names) - {"app-only-manifest.json"}
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != expected_files:
        raise AppOnlyError("app-only bundle file set mismatch")
    for name in expected_files:
        if files[name] != sha256(bundle / name):
            raise AppOnlyError(f"app-only bundle digest mismatch: {name}")
    if manifest["source_bundle_sha256"] != sha256(bundle / "source-manifest.json"):
        raise AppOnlyError("app-only source manifest digest mismatch")
    if destination is not None:
        destination.mkdir(parents=True, mode=0o700)
        os.chown(destination, 0, 0)
        for name in names:
            atomic_bytes(destination / name, (bundle / name).read_bytes(), 0o400)
        bundle = destination
    release = strict_load(bundle / "release.json")
    env = parse_env(bundle / "production.env")
    source = strict_load(bundle / "source-manifest.json")
    evidence = strict_load(bundle / "classification-evidence.json")
    release_sha = sha256(bundle / "release.json")
    env_sha = sha256(bundle / "production.env")
    validate_source_manifest(source, release_sha, env_sha, str(release.get("release_id", "")))
    if set(evidence) != {
        "schema", "kind", "live_commit", "candidate_commit", "changed_paths",
        "forbidden_matches", "generated_utc",
    } or evidence.get("schema") != 1 or evidence.get("kind") != "menhir-app-only-classification":
        raise AppOnlyError("app-only classification evidence schema mismatch")
    changed_paths = evidence.get("changed_paths")
    if not isinstance(changed_paths, list) or not changed_paths \
            or changed_paths != sorted(set(changed_paths)) \
            or any(not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/") for path in changed_paths):
        raise AppOnlyError("app-only changed-path evidence is invalid")
    if evidence.get("forbidden_matches") != []:
        raise AppOnlyError("source diff contains protected app-only paths")
    if evidence.get("live_commit") != strict_load(LIVE_RELEASE).get("repos", {}).get("menhir") \
            or evidence.get("candidate_commit") != release.get("repos", {}).get("menhir"):
        raise AppOnlyError("classification evidence is not bound to the live and candidate commits")
    return {
        "path": bundle, "release": release, "env": env,
        "release_sha": release_sha, "env_sha": env_sha, "evidence": evidence,
    }


def validate_release_file(path: Path) -> None:
    require_root_file(SCHEMA, "release schema validator")
    run(["python3", str(SCHEMA), "validate-release", str(path)], 30)


def classify_bundle(bundle_id: str, destination: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    require_root_file(LIVE_RELEASE, "live release")
    require_root_file(LIVE_ENV, "live production environment")
    bundle = load_bundle(bundle_id, destination)
    validate_release_file(bundle["path"] / "release.json")
    live = strict_load(LIVE_RELEASE)
    result = classify_release(
        live, bundle["release"], sha256(LIVE_RELEASE), parse_env(LIVE_ENV),
        bundle["env"], bundle["env_sha"],
    )
    result["candidate_release_sha256"] = bundle["release_sha"]
    return bundle, result


def inspect_container(name: str) -> dict[str, Any]:
    value = json.loads(run(["docker", "inspect", name], 20))
    if not isinstance(value, list) or len(value) != 1:
        raise AppOnlyError(f"Docker inspection is ambiguous: {name}")
    return value[0]


def wait_app(image_digest: str, release_id: str, database_id: str, deadline_seconds: int) -> None:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            app = inspect_container("menhir-prod-app")
            database = inspect_container("menhir-prod-neo4j")
            labels = app.get("Config", {}).get("Labels", {}) or {}
            environment = app.get("Config", {}).get("Env", []) or []
            if all((
                database.get("Id") == database_id,
                database.get("State", {}).get("Health", {}).get("Status") == "healthy",
                app.get("State", {}).get("Health", {}).get("Status") == "healthy",
                str(app.get("Config", {}).get("Image", "")).endswith("@" + image_digest),
                labels.get("com.docker.compose.project") == "menhir-prod",
                labels.get("com.docker.compose.service") == "menhir",
                f"MENHIR_RELEASE_ID={release_id}" in environment,
            )):
                return
        except AppOnlyError:
            pass
        time.sleep(2)
    raise AppOnlyError("replacement app did not become exact and healthy within 120 seconds")


def replace_app(env_path: Path, image_digest: str, release_id: str, database_id: str) -> None:
    run([
        "docker", "compose", "--project-name", "menhir-prod", "--env-file", str(env_path),
        "--file", str(COMPOSE), "up", "-d", "--no-deps", "--force-recreate", "menhir",
    ], 120)
    wait_app(image_digest, release_id, database_id, 120)


def request_json(url: str, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Menhir-AppOnly/1", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except Exception as exc:
        raise AppOnlyError(f"HTTP acceptance failed: {url}: {exc}") from exc
    if not isinstance(value, dict):
        raise AppOnlyError(f"HTTP acceptance returned non-object JSON: {url}")
    return value


def mcp_post(base: str, token: str, payload: dict[str, Any], session: str = "") -> tuple[dict[str, Any], str]:
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Menhir-AppOnly/1",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        base.rstrip("/") + "/mcp-http",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            next_session = response.headers.get("Mcp-Session-Id", session)
    except urllib.error.HTTPError as exc:
        raise AppOnlyError(f"MCP acceptance returned HTTP {exc.code}") from exc
    if "text/event-stream" in content_type:
        events = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not events:
            raise AppOnlyError("MCP acceptance SSE response had no data")
        body = events[-1]
    try:
        value = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise AppOnlyError("MCP acceptance returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("error"):
        raise AppOnlyError(f"MCP acceptance returned an error: {value}")
    return value, next_session


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
    script = r'''import json
import os
import secrets
import time
from menhir.api import jose_provider

client_id = "menhir-deploy-probe"
now = int(time.time())
with open(os.environ["MENHIR_OAUTH_SIGNING_KEY_PATH"], encoding="utf-8") as handle:
    key = jose_provider.load_key(json.load(handle))
public = jose_provider.serialize_key(key, private=False)
claims = {
    "iss": os.environ["MENHIR_OAUTH_ISSUER"],
    "sub": "service:menhir-deploy-probe",
    "aud": os.environ["MENHIR_OAUTH_RESOURCE"],
    "client_id": client_id,
    "client_name": client_id,
    "scope": "menhir:read",
    "tier": "readonly",
    "iat": now,
    "exp": now + 60,
    "jti": secrets.token_urlsafe(18),
}
print(jose_provider.sign_jwt(
    {"alg": "RS256", "kid": public["kid"], "typ": "JWT"}, claims, key,
))
'''
    token = run(
        ["docker", "exec", "-i", "menhir-prod-app", "python", "-"],
        15,
        input_bytes=script.encode("ascii"),
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


def write_stage(transaction: dict[str, Any], stage: str, **extra: Any) -> None:
    transaction.update(extra)
    transaction["stage"] = stage
    transaction["updated_utc"] = now_iso()
    atomic_json(ACTIVE, transaction)


def docker_pull(image_ref: str, credential: Path, root_config: Path) -> None:
    require_upload(credential, "Docker credential")
    if credential.stat().st_size > 65536:
        raise AppOnlyError("Docker credential file is unexpectedly large")
    config = strict_load(credential)
    if set(config) != {"auths"} or not isinstance(config.get("auths"), dict) \
            or "ghcr.io" not in config["auths"]:
        raise AppOnlyError("Docker credential must contain only a ghcr.io auth map")
    root_config.mkdir(parents=True, mode=0o700)
    os.chown(root_config, 0, 0)
    atomic_bytes(root_config / "config.json", credential.read_bytes(), 0o600)
    try:
        run(["docker", "--config", str(root_config), "pull", image_ref], 60)
    finally:
        try:
            (root_config / "config.json").unlink()
            root_config.rmdir()
        except OSError:
            pass


def finalize_transaction(transaction: dict[str, Any]) -> None:
    atomic_json(LAST, transaction)
    try:
        ACTIVE.unlink()
    except FileNotFoundError:
        pass


def restore_authority(transaction: dict[str, Any], candidate: bool) -> None:
    tx = Path(transaction["transaction_root"])
    release_name = "candidate-release.json" if candidate else "prior-release.json"
    env_name = "candidate-production.env" if candidate else "prior-production.env"
    require_root_file(tx / release_name, release_name)
    require_root_file(tx / env_name, env_name)
    atomic_bytes(LIVE_ENV, (tx / env_name).read_bytes(), 0o600)
    atomic_bytes(LIVE_RELEASE, (tx / release_name).read_bytes(), 0o400)


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
    write_stage(transaction, "complete", completed_utc=now_iso(), recovered=True)
    run([str(SCAFFOLD), "verify", "--app-only"], 30)
    finalize_transaction(transaction)


def acquire_lock() -> Any:
    if fcntl is None:
        raise AppOnlyError("POSIX file locking is unavailable")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise AppOnlyError("deployment lock is held") from exc
    return handle


def deploy(bundle_id: str) -> dict[str, Any]:
    if ACTIVE.exists():
        raise AppOnlyError("an incomplete app-only transaction exists; run recover")
    lock = acquire_lock()
    transaction: dict[str, Any] | None = None
    try:
        run([str(SCAFFOLD), "verify", "--app-only"], 30)
        tx_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + bundle_id
        tx = STATUS / "app-only" / tx_id
        bundle, classification = classify_bundle(bundle_id, tx)
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
        transaction = {
            "schema": 1,
            "kind": "menhir-app-only-transaction",
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
        write_stage(transaction, "accepted", accepted_utc=now_iso())
        restore_authority(transaction, True)
        write_stage(transaction, "complete", completed_utc=now_iso())
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


def recover() -> dict[str, Any]:
    if not ACTIVE.exists():
        raise AppOnlyError("there is no incomplete app-only transaction")
    lock = acquire_lock()
    try:
        require_root_file(ACTIVE, "active app-only transaction")
        transaction = strict_load(ACTIVE)
        if transaction.get("kind") != "menhir-app-only-transaction":
            raise AppOnlyError("active app-only transaction schema mismatch")
        if transaction.get("stage") in {"accepted", "complete"}:
            rollforward(transaction)
        else:
            rollback(transaction)
        return transaction
    finally:
        lock.close()


def live_info() -> dict[str, Any]:
    require_root_file(LIVE_RELEASE, "live release")
    value = strict_load(LIVE_RELEASE)
    return {
        "release_id": value.get("release_id"),
        "menhir_commit": value.get("repos", {}).get("menhir"),
        "menhir_image": value.get("images", {}).get("menhir"),
        "release_sha256": sha256(LIVE_RELEASE),
    }


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("bundle_id")
    deploy_command = commands.add_parser("deploy")
    deploy_command.add_argument("bundle_id")
    commands.add_parser("recover")
    commands.add_parser("live")
    commands.add_parser("check")
    commands.add_parser("accept-current")
    return result


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("REFUSED: app-only authority must run as root", file=sys.stderr)
        return 1
    args = parser().parse_args(argv)
    try:
        if args.command == "classify":
            _, value = classify_bundle(args.bundle_id)
        elif args.command == "deploy":
            value = deploy(args.bundle_id)
        elif args.command == "recover":
            value = recover()
        elif args.command == "live":
            value = live_info()
        elif args.command == "check":
            value = check_live()
        else:
            value = accept_current()
    except AppOnlyError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
