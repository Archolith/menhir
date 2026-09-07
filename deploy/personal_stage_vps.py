#!/usr/bin/env python3
"""Run Menhir's disposable production-equivalent staging suite on a Docker host.

The runner is intentionally Linux/VPS-only.  It consumes an uploaded immutable
install bundle, creates isolated credentials and data beneath
``/srv/menhir/staging``, runs the exact release images behind an isolated Caddy
ingress, exercises OAuth/MCP/write/deny/restart/rollback behavior, writes one
receipt outside the disposable tree, and removes all staging resources.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGING_ROOT = Path("/srv/menhir/staging")
STAGING_HOST = "memory.ctharvey.me"
STAGING_BASE = f"https://{STAGING_HOST}"
STAGING_CLIENT = "menhir-staging-probe"
STAGING_SUBJECT = "menhir-admin"
STAGING_NAMESPACE = "menhir-staging"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
DEPLOYMENT_CLASSES = frozenset({"app-only", "security-config", "maintenance"})
PREFLIGHT_SCHEMA = 1
PRODUCTION_ROOT = Path("/srv/menhir/production")
PRODUCTION_RELEASE = PRODUCTION_ROOT / "release/release.json"
PRODUCTION_STATUS_ROOT = Path("/var/lib/menhir-production")
RELEASE_JOURNAL = PRODUCTION_STATUS_ROOT / "release-run.json"
ROUTE_PHASE_JOURNAL = Path("/var/lib/yawn/caddy-release.phase")
ROUTE_CANDIDATE = Path("/srv/yawn/releases/menhir-route-candidate")
ROUTE_ENV = Path("/srv/yawn/projects/yawn.deploy/.env")
LIVE_ROUTE_RUNNER = PRODUCTION_ROOT / "bin/caddy-release.sh"
MIN_STAGING_DISK_BYTES = 8 * 1024**3
# The disposable Menhir and Neo4j containers retain production's 2/4 GiB hard
# limits, plus a small proxy/fake-provider allowance. Requiring 7 GiB proved
# brittle on the 12.5 GB host (a normal 32 MB fluctuation blocked rehearsal),
# while 6.5 GiB preserves the complete limit envelope and a 512 MiB allowance.
MIN_STAGING_MEMORY_BYTES = 13 * 1024**3 // 2
PRODUCTION_MEMORY_LIMITS = {
    "menhir": 2 * 1024**3,
    "neo4j": 4 * 1024**3,
}
ROUTE_ASSETS = {
    "Caddyfile": "rootfs/srv/yawn/projects/yawn.deploy/Caddyfile",
    "docker-compose.yml": "rootfs/srv/yawn/projects/yawn.deploy/docker-compose.yml",
    "releases.json": "rootfs/srv/yawn/projects/yawn.deploy/releases.json",
}
TLS_ROUTE_KEYS = (
    "memory_tls_cert",
    "memory_tls_key",
    "origin_ca_trust",
    "aop_trust",
    "aop_client_cert",
    "aop_client_key",
)
CHECKS = (
    "artifact_identity",
    "production_memory_limits",
    "production_network_shape",
    "oauth_policy_shape",
    "ingress_request_handling",
    "isolated_disposable_data",
    "non_production_credentials",
    "production_authority_absent",
    "oauth_discovery",
    "oauth_authorization_code_pkce",
    "mcp_initialize",
    "mcp_tools_list",
    "mcp_recall",
    "synthetic_write_allowed",
    "denied_operation_refused",
    "restart_persistence",
    "automatic_rollback",
)


class StageError(RuntimeError):
    pass


def _chown(path: Path, uid: int, gid: int) -> None:
    """Apply Linux ownership; keep pure data-generation helpers testable on Windows."""
    if hasattr(os, "chown"):
        os.chown(path, uid, gid)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StageError("bundle must be a non-symlink directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise StageError(f"bundle contains symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise StageError(f"bundle contains special entry: {relative}")
        digest.update(f"{relative}\0{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _safe_file(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise StageError(f"{label} must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise StageError(f"missing {label}: {path}") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StageError(f"{label} must be a regular non-symlink file")
    return path


def _load_json(path: Path, label: str) -> dict[str, Any]:
    _safe_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise StageError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise StageError(f"{label} must be a JSON object")
    return value


def _run(
    *args: str,
    check: bool = True,
    timeout: int = 300,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode:
        raise StageError(
            f"command failed ({result.returncode}): {args[0]}\n{result.stdout[-2000:].strip()}"
        )
    return result


def _write(path: Path, value: str, mode: int, uid: int = 0, gid: int = 0) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            pass
    _chown(path, uid, gid)
    os.chmod(path, mode)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _chown(temp, 0, 0)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _canonical_json_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _seal_preflight(report: dict[str, Any]) -> dict[str, Any]:
    """Add a digest over the allowlisted report fields, never over raw host data."""
    sealed = dict(report)
    sealed.pop("canonical_sha256", None)
    sealed["canonical_sha256"] = _canonical_json_sha256(sealed)
    return sealed


def _parse_env(path: Path) -> dict[str, str]:
    _safe_file(path, "bundled production environment")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None:
            raise StageError("bundled production environment is not simple KEY=value data")
        if any(character in value for character in "\r\n\x00"):
            raise StageError("bundled production environment contains unsafe data")
        result[key] = value.strip("'\"")
    return result


def _validate_bundle(
    bundle: Path,
    expected_bundle: str,
    expected_release_id: str,
    expected_release_sha: str,
) -> tuple[dict[str, Any], dict[str, str], Path]:
    if _tree_sha256(bundle) != expected_bundle:
        raise StageError("uploaded bundle tree digest mismatch")
    manifest = _load_json(bundle / "bundle-manifest.json", "bundle manifest")
    if manifest.get("schema") != 1 \
            or manifest.get("kind") != "menhir-release-install-bundle" \
            or manifest.get("release_id") != expected_release_id \
            or manifest.get("release_sha256") != expected_release_sha:
        raise StageError("bundle manifest release binding mismatch")
    rows = manifest.get("files")
    if not isinstance(rows, dict) or not rows:
        raise StageError("bundle manifest file authority is invalid")
    for destination, row in rows.items():
        if not isinstance(destination, str) or not destination.startswith("/") \
                or not isinstance(row, dict) or set(row) != {"mode", "sha256"}:
            raise StageError("bundle manifest contains an invalid row")
        source = bundle / "rootfs" / destination.lstrip("/")
        _safe_file(source.resolve(), f"bundle payload {destination}")
        if _sha256(source) != row.get("sha256"):
            raise StageError(f"bundle payload digest mismatch: {destination}")
    release_path = bundle / "rootfs/srv/menhir/production/release/release.json"
    if _sha256(release_path) != expected_release_sha:
        raise StageError("bundled release authority digest mismatch")
    release = _load_json(release_path.resolve(), "release authority")
    if release.get("release_id") != expected_release_id:
        raise StageError("release authority identity mismatch")
    images = release.get("images")
    if not isinstance(images, dict) or any(
        not isinstance(images.get(name), str) or IMAGE_RE.fullmatch(images[name]) is None
        for name in ("menhir", "neo4j", "caddy")
    ):
        raise StageError("release image authority is invalid")
    env_path = bundle / "rootfs/srv/menhir/production/release/production.env"
    environment = _parse_env(env_path.resolve())
    for name, key in (("menhir", "MENHIR_IMAGE"), ("neo4j", "NEO4J_IMAGE")):
        image = environment.get(key, "")
        if not image.endswith("@" + images[name]):
            raise StageError(f"bundled {name} image reference differs from release authority")
    return release, environment, env_path


def _canonical_policy(policy: dict[str, Any]) -> str:
    payload = dict(policy)
    payload.pop("canonical_digest", None)
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")).hexdigest()


def _stage_policy(source: Path, destination: Path) -> str:
    policy = _load_json(source.resolve(), "bundled client policy")
    if policy.get("version") != 2 or not isinstance(policy.get("clients"), dict):
        raise StageError("staging requires a version-2 production policy")
    contract = policy.get("access_contract")
    if not isinstance(contract, dict):
        raise StageError("production policy has no access contract")
    contract["primary_endpoint"] = STAGING_BASE + "/mcp-http"
    products = contract.get("products")
    if not isinstance(products, dict):
        raise StageError("production policy products are invalid")
    source_client_id = None
    chatgpt = products.get("chatgpt")
    if isinstance(chatgpt, dict):
        ids = chatgpt.get("client_ids")
        if isinstance(ids, list):
            source_client_id = next((value for value in ids if value in policy["clients"]), None)
    if not isinstance(source_client_id, str):
        raise StageError("production policy has no cloneable ChatGPT operator identity")
    probe = json.loads(json.dumps(policy["clients"][source_client_id]))
    probe["label"] = STAGING_CLIENT
    probe["namespace"] = STAGING_NAMESPACE
    probe["registration"] = {
        "client_name": STAGING_CLIENT,
        "redirect_uris": ["https://client.staging.invalid/callback"],
        "token_endpoint_auth_method": "none",
    }
    policy["clients"][STAGING_CLIENT] = probe
    policy["canonical_digest"] = _canonical_policy(policy)
    _write(
        destination,
        json.dumps(policy, indent=2, sort_keys=True) + "\n",
        0o440,
        0,
        10001,
    )
    return policy["canonical_digest"]


def _mkdir(path: Path, mode: int, uid: int = 0, gid: int = 0) -> None:
    path.mkdir(parents=True, exist_ok=False)
    _chown(path, uid, gid)
    os.chmod(path, mode)


def _select_subnet() -> str:
    used = _run("docker", "network", "inspect", *_run(
        "docker", "network", "ls", "-q"
    ).stdout.split(), check=False).stdout
    for second in range(24, 30):
        for third in range(240, 250):
            candidate = f"172.{second}.{third}.0/24"
            if candidate not in used:
                return candidate
    raise StageError("no isolated staging subnet is available")


def _prepare_tree(root: Path, bundle: Path, release: dict[str, Any], environment: dict[str, str]) -> tuple[str, str]:
    _mkdir(root, 0o700)
    for path, mode, uid, gid in (
        (root / "state", 0o700, 10001, 10001),
        (root / "state/oauth", 0o700, 10001, 10001),
        (root / "state/telemetry", 0o700, 10001, 10001),
        (root / "state/neo4j", 0o700, 7474, 7474),
        (root / "state/neo4j/data", 0o700, 7474, 7474),
        (root / "state/neo4j/logs", 0o700, 7474, 7474),
        (root / "secrets", 0o711, 0, 0),
        (root / "secrets/neo4j", 0o750, 0, 7474),
        (root / "secrets/menhir", 0o750, 0, 10001),
        # The exact image creates a fresh staging signing key as UID 10001.
        # This directory is narrowed to root:10001 0750 immediately afterward.
        (root / "secrets/oauth", 0o700, 10001, 10001),
        (root / "policy", 0o750, 0, 10001),
        (root / "caddy-data", 0o700, 1000, 1000),
        (root / "caddy-config", 0o700, 1000, 1000),
    ):
        _mkdir(path, mode, uid, gid)

    password = secrets.token_urlsafe(32)
    operator_key = secrets.token_urlsafe(48)
    _write(root / "secrets/neo4j/neo4j-auth", f"neo4j/{password}\n", 0o440, 0, 7474)
    _write(root / "secrets/menhir/neo4j-password", password + "\n", 0o440, 0, 10001)
    _write(root / "secrets/menhir/operator-key", operator_key + "\n", 0o440, 0, 10001)
    _write(root / "secrets/menhir/local-llm-api-key", "staging-not-secret\n", 0o440, 0, 10001)
    _write(root / "secrets/oauth/oauth-consent-secret", secrets.token_urlsafe(48) + "\n", 0o440, 0, 10001)
    keyring = {
        "version": 1,
        "current_key_id": "staging-v1",
        "keys": {
            "staging-v1": base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
        },
    }
    _write(
        root / "secrets/oauth/retry-response-keyring.json",
        json.dumps(keyring, separators=(",", ":")) + "\n",
        0o440,
        0,
        10001,
    )
    _run(
        "docker", "run", "--rm", "--entrypoint", "python", "--user", "10001:10001",
        "--mount", f"type=bind,source={root / 'secrets/oauth'},target=/keys",
        environment["MENHIR_IMAGE"], "-c",
        "from pathlib import Path; from archolith_oauth import SigningKeyStore; "
        "SigningKeyStore(Path('/keys/oauth_signing_key.json')).load_or_create()",
    )
    _chown(root / "secrets/oauth/oauth_signing_key.json", 0, 10001)
    os.chmod(root / "secrets/oauth/oauth_signing_key.json", 0o440)
    _chown(root / "secrets/oauth", 0, 10001)
    os.chmod(root / "secrets/oauth", 0o750)

    policy_source = bundle / "rootfs/srv/menhir/production/policy/client-policy.json"
    policy_digest = _stage_policy(policy_source, root / "policy/client-policy.json")
    fake_server = '''#!/usr/bin/env python3
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.reply({"data": [{"id": "staging-chat", "object": "model"}, {"id": "text-embedding-3-small", "object": "model"}], "object": "list"})
    def do_POST(self):
        length=int(self.headers.get("content-length", "0")); body=self.rfile.read(length)
        try: request=json.loads(body or b"{}")
        except Exception: request={}
        if self.path.endswith("/embeddings"):
            inputs=request.get("input", [""])
            if not isinstance(inputs, list): inputs=[inputs]
            self.reply({"object":"list","data":[{"object":"embedding","index":i,"embedding":[0.0]*1536} for i,_ in enumerate(inputs)],"model":"staging-embed","usage":{"prompt_tokens":1,"total_tokens":1}})
        else:
            self.reply({"id":"staging-chat","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"{}"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}})
    def reply(self, value):
        payload=json.dumps(value,separators=(",", ":")).encode(); self.send_response(200); self.send_header("content-type","application/json"); self.send_header("content-length",str(len(payload))); self.end_headers(); self.wfile.write(payload)
ThreadingHTTPServer(("0.0.0.0",8080),Handler).serve_forever()
'''
    _write(root / "fake_openai.py", fake_server, 0o444)
    caddyfile = f'''{{
    admin off
}}
https://{STAGING_HOST} {{
    tls internal
    reverse_proxy menhir-staging-app:8099
}}
'''
    _write(root / "Caddyfile", caddyfile, 0o444)
    return policy_digest, operator_key


def _compose_files(
    root: Path,
    bundle: Path,
    release: dict[str, Any],
    source_environment: dict[str, str],
    subnet: str,
) -> tuple[Path, Path]:
    base = bundle / "rootfs/srv/menhir/production/deploy/docker-compose.production.yml"
    override = root / "docker-compose.staging.yml"
    network = f"menhir-stage-{root.name}"
    value = f'''services:
  menhir:
    environment:
      MENHIR_TRUSTED_PROXY_PEERS: "{subnet}"
      LOCAL_LLM_EMBED_BASE_URL: "http://fake-llm:8080/v1"
      LOCAL_LLM_EMBED_MODEL: "text-embedding-3-small"
      SSL_CERT_FILE: "/run/staging-ca.crt"
    depends_on:
      fake-llm:
        condition: service_started
    volumes:
      - type: bind
        source: {root / 'staging-ca.crt'}
        target: /run/staging-ca.crt
        read_only: true
  fake-llm:
    image: "{source_environment['MENHIR_IMAGE']}"
    container_name: menhir-stage-{root.name}-fake-llm
    entrypoint: ["python", "/probe/fake_openai.py"]
    user: "10001:10001"
    read_only: true
    mem_limit: 256m
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    networks: [internal]
    volumes:
      - type: bind
        source: {root / 'fake_openai.py'}
        target: /probe/fake_openai.py
        read_only: true
  staging-proxy:
    image: "{release['images']['caddy']}"
    container_name: menhir-stage-{root.name}-proxy
    restart: "no"
    mem_limit: 256m
    ports:
      - "127.0.0.1::443"
    networks:
      menhir-proxy:
        ipv4_address: {subnet.removesuffix('0/24')}2
        aliases:
          - {STAGING_HOST}
    volumes:
      - type: bind
        source: {root / 'Caddyfile'}
        target: /etc/caddy/Caddyfile
        read_only: true
      - type: bind
        source: {root / 'caddy-data'}
        target: /data
      - type: bind
        source: {root / 'caddy-config'}
        target: /config
networks:
  menhir-proxy:
    external: false
    name: {network}
    ipam:
      config:
        - subnet: {subnet}
'''
    _write(override, value, 0o600)
    return base, override


def _stage_environment(
    root: Path,
    source: dict[str, str],
    policy_digest: str,
    project: str,
    subnet: str,
) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "MENHIR_IMAGE": source["MENHIR_IMAGE"],
        "NEO4J_IMAGE": source["NEO4J_IMAGE"],
        "MENHIR_RELEASE_COMMIT": source["MENHIR_RELEASE_COMMIT"],
        "MENHIR_RELEASE_ID": source["MENHIR_RELEASE_ID"],
        "MENHIR_RUNTIME_MODE": "production",
        "MENHIR_INSTANCE_ID": project,
        "MENHIR_PUBLIC_BASE_URL": STAGING_BASE,
        "MENHIR_CLIENT_POLICY_DIGEST": policy_digest,
        "MENHIR_COMPOSE_PROJECT": project,
        "MENHIR_STATE_ROOT": str(root / "state"),
        "MENHIR_TELEMETRY_ROOT": str(root / "state/telemetry"),
        "MENHIR_PROD_SECRETS_DIR": str(root / "secrets"),
        "MENHIR_PROD_POLICY_DIR": str(root / "policy"),
        "MENHIR_AUTHORITIES_READ_ONLY": "false",
        "MENHIR_APP_CONTAINER": f"menhir-stage-{root.name}-app",
        "MENHIR_NEO4J_CONTAINER": f"menhir-stage-{root.name}-neo4j",
        "MENHIR_PROXY_IPV4": subnet.removesuffix("0/24") + "3",
        "MENHIR_PROXY_ALIAS": "menhir-staging-app",
        "LLM_CHAT_PROVIDER": "local",
        "GRAPHITI_LLM_PROVIDER": "local",
        "GRAPHITI_EMBED_PROVIDER": "local",
        "GRAPHITI_RERANKER_PROVIDER": "local",
        "LOCAL_LLM_BASE_URL": "http://fake-llm:8080/v1",
        "LOCAL_LLM_CHAT_MODEL": "staging-chat",
        "MENHIR_CANONICAL_SELF_BINDING_MODE": "enforce",
        "SCHEDULER_TRACE_DISABLED": "1",
    })
    return env


def _compose(base: Path, override: Path, project: str, environment: dict[str, str], *args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    command = (
        "docker", "compose", "--project-name", project,
        "--file", str(base), "--file", str(override), *args,
    )
    result = subprocess.run(
        command, env=environment, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False, timeout=timeout,
    )
    if check and result.returncode:
        raise StageError(f"compose failed ({result.returncode}): {' '.join(args)}\n{result.stdout[-3000:]}")
    return result


def _bootstrap_blank_schema(
    base: Path,
    override: Path,
    project: str,
    environment: dict[str, str],
) -> None:
    """Prime a blank disposable graph, then wait for its indexes to become ONLINE.

    Production normally upgrades an existing schema. A genuinely empty Neo4j
    instance can still have freshly-created indexes in POPULATING state when
    Menhir performs its immediate post-DDL readiness check, so staging creates
    and waits for that same schema before starting the long-running app.
    """
    program = r'''
import time
from pathlib import Path
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.neo4j import Neo4jRepository
password=Path("/run/secrets/menhir/neo4j-password").read_text().strip()
repo=Neo4jRepository(uri="bolt://neo4j:7687", database="neo4j", user="neo4j", password=password)
try:
    # Graphiti normally creates these immediately before Menhir's phase-one
    # bootstrap. Staging primes them explicitly because the long-running app's
    # fail-closed post-DDL check is intentionally too early for a blank graph.
    repo.execute("CREATE INDEX episode_uuid IF NOT EXISTS FOR (n:Episodic) ON (n.uuid)")
    repo.execute("CREATE INDEX episode_group_id IF NOT EXISTS FOR (n:Episodic) ON (n.group_id)")
    repo.execute("CREATE FULLTEXT INDEX episode_content IF NOT EXISTS FOR (n:Episodic) ON EACH [n.content, n.source, n.source_description, n.group_id]")
    result=MemoryGraphAdapter(repo).bootstrap_phase_one()
    if result.failures:
        raise SystemExit("schema bootstrap failures: " + "; ".join(result.failures))
    deadline=time.monotonic()+120
    while time.monotonic() < deadline:
        if MemoryGraphAdapter(repo).phase_one_schema_ready():
            print("blank staging schema is online")
            break
        time.sleep(2)
    else:
        raise SystemExit("blank staging schema did not become online")
finally:
    repo.close()
'''
    _compose(
        base, override, project, environment,
        "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
        "menhir", "-c", program,
        timeout=180,
    )


def _inspect(name: str) -> dict[str, Any]:
    value = json.loads(_run("docker", "inspect", name).stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise StageError(f"ambiguous Docker container: {name}")
    return value[0]


def _wait_healthy(names: tuple[str, ...], timeout_s: int = 240) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        statuses = []
        for name in names:
            try:
                inspected = _inspect(name)
                state = inspected.get("State", {})
                status = state.get("Health", {}).get("Status")
                statuses.append(status)
                if state.get("Status") in {"dead", "exited"} or status == "unhealthy":
                    logs = _run("docker", "logs", "--tail", "120", name, check=False).stdout
                    raise StageError(
                        f"staging container failed health: {name}: "
                        f"state={state.get('Status')} health={status}\n{logs[-5000:]}"
                    )
            except (StageError, json.JSONDecodeError):
                raise
        if all(value == "healthy" for value in statuses):
            return
        time.sleep(3)
    evidence = []
    for name in names:
        inspected = _inspect(name)
        state = inspected.get("State", {})
        logs = _run("docker", "logs", "--tail", "80", name, check=False).stdout
        evidence.append(
            f"{name}: state={state.get('Status')} "
            f"health={state.get('Health', {}).get('Status')}\n{logs[-3000:]}"
        )
    raise StageError("staging containers did not become healthy\n" + "\n".join(evidence))


def _proxy_port(proxy: str) -> int:
    output = _run("docker", "port", proxy, "443/tcp").stdout.strip()
    match = re.search(r":([0-9]+)$", output)
    if match is None:
        raise StageError("staging ingress has no loopback TLS port")
    return int(match.group(1))


def _wait_file(path: Path, timeout_s: int = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
            return
        time.sleep(1)
    raise StageError(f"staging ingress did not create required file: {path}")


def _prime_ingress_certificate(port: int) -> None:
    """Make Caddy provision its on-demand local CA before the app mounts it."""
    last_error: Exception | None = None
    for _ in range(15):
        try:
            # The upstream app is intentionally not running yet.  A 502 response
            # is sufficient: completing TLS causes Caddy to create the local CA.
            _http(port, "GET", "/")
            return
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(2)
    raise StageError(f"staging ingress could not provision its certificate: {last_error}")


def _http(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request_headers = {
        "Host": STAGING_HOST,
        "User-Agent": "Menhir-Personal-Staging/1",
        "Accept": "application/json",
    }
    request_headers.update(headers or {})
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    class LoopbackHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            raw = socket.create_connection(("127.0.0.1", port), self.timeout)
            self.sock = context.wrap_socket(raw, server_hostname=STAGING_HOST)

    connection = LoopbackHTTPSConnection(STAGING_HOST, port, timeout=20, context=context)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read(), {key.lower(): value for key, value in response.getheaders()}
    finally:
        connection.close()


def _json_response(port: int, path: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(15):
        try:
            status, body, _ = _http(port, "GET", path)
            break
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            time.sleep(2)
    else:
        raise StageError(f"staging GET could not reach ingress: {path}: {last_error}")
    if status // 100 != 2:
        raise StageError(f"staging GET failed: {path}: HTTP {status}")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise StageError(f"staging GET returned non-object JSON: {path}")
    return value


def _production_snapshot() -> dict[str, str]:
    snapshot: dict[str, str] = {}
    authority = PRODUCTION_RELEASE
    if authority.is_file() and not authority.is_symlink():
        snapshot["release_sha256"] = _sha256(authority)
    for name in ("menhir-prod-app", "menhir-prod-neo4j"):
        result = _run("docker", "inspect", "--format", "{{.Id}}", name, check=False)
        if result.returncode == 0 and result.stdout.strip():
            snapshot[name] = result.stdout.strip()
    return snapshot


def _available_memory_bytes() -> int:
    try:
        rows = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StageError("production preflight cannot read available memory") from exc
    for row in rows:
        match = re.fullmatch(r"MemAvailable:\s+([0-9]+)\s+kB", row)
        if match is not None:
            return int(match.group(1)) * 1024
    raise StageError("production preflight cannot determine available memory")


def _network_inspect(name: str) -> dict[str, Any]:
    result = _run("docker", "network", "inspect", name, check=False)
    if result.returncode != 0:
        raise StageError(f"production preflight network is absent: {name}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StageError("production preflight network inspection is invalid") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise StageError("production preflight network inspection is ambiguous")
    return value[0]


def _container_digest(container: dict[str, Any], role: str) -> str:
    reference = container.get("Config", {}).get("Image")
    if not isinstance(reference, str):
        raise StageError(f"production {role} image identity is absent")
    match = re.search(r"@(sha256:[0-9a-f]{64})$", reference)
    if match is None:
        raise StageError(f"production {role} image is not digest-pinned")
    return match.group(1)


def _container_environment(container: dict[str, Any]) -> dict[str, str]:
    """Select environment fields for validation without retaining raw secrets."""
    rows = container.get("Config", {}).get("Env", [])
    if not isinstance(rows, list):
        raise StageError("production Menhir environment inspection is invalid")
    selected: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, str):
            raise StageError("production Menhir environment inspection is invalid")
        key, separator, value = row.partition("=")
        if separator and key in {"MENHIR_RELEASE_ID", "MENHIR_RUNTIME_MODE"}:
            selected[key] = value
    return selected


def _healthy_container(name: str, role: str) -> dict[str, Any]:
    container = _inspect(name)
    state = container.get("State", {})
    if state.get("Running") is not True or state.get("Status") != "running" \
            or state.get("Health", {}).get("Status") != "healthy":
        raise StageError(f"production {role} container is not running and healthy")
    expected_memory = PRODUCTION_MEMORY_LIMITS[role]
    if container.get("HostConfig", {}).get("Memory") != expected_memory:
        raise StageError(f"production {role} memory limit differs from its role contract")
    return container


def _ipv4(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise StageError(f"{label} is invalid")
    address = value.split("/", 1)[0]
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise StageError(f"{label} is invalid") from exc
    if parsed.version != 4:
        raise StageError(f"{label} must be IPv4")
    return str(parsed)


def _network_containers(network: dict[str, Any]) -> list[dict[str, str]]:
    value = network.get("Containers", {})
    if not isinstance(value, dict):
        raise StageError("production proxy network container census is invalid")
    rows: list[dict[str, str]] = []
    for container_id, details in value.items():
        if not isinstance(container_id, str) or not isinstance(details, dict):
            raise StageError("production proxy network container census is invalid")
        name = details.get("Name")
        if not isinstance(name, str) or not name:
            raise StageError("production proxy network container identity is invalid")
        rows.append({
            "id": container_id,
            "name": name,
            "ipv4": _ipv4(details.get("IPv4Address"), "production proxy peer address"),
        })
    return rows


def _release_journal_status(candidate_release_id: str) -> dict[str, Any]:
    if not RELEASE_JOURNAL.exists() and not RELEASE_JOURNAL.is_symlink():
        return {"state": "absent"}
    journal = _load_json(RELEASE_JOURNAL, "production release journal")
    release_id = journal.get("release_id")
    stage = journal.get("stage")
    if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None \
            or stage not in {
                "start", "backup", "staged", "rehearsal", "candidate",
                "accepted", "routed", "promoted", "complete",
            }:
        raise StageError("production release journal identity or stage is invalid")
    if stage != "complete" and release_id != candidate_release_id:
        raise StageError("an unfinished conflicting production release journal exists")
    return {
        "state": "complete" if stage == "complete" else "same-release-resumable",
        "release_id": release_id,
        "stage": stage,
    }


def _regular_executable(path: Path, label: str) -> Path:
    value = _safe_file(path, label)
    if os.name != "nt" and value.stat().st_mode & 0o111 == 0:
        raise StageError(f"{label} is not executable")
    return value


def _safe_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise StageError(f"{label} must be absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise StageError(f"missing {label}") from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StageError(f"{label} must be a non-symlink directory")
    return path


def _candidate_fixed_ips(compose: Path) -> list[tuple[str, str]]:
    """Read only literal service IPv4 assignments from the reviewed route YAML."""
    service = ""
    rows: list[tuple[str, str]] = []
    for line in compose.read_text(encoding="utf-8").splitlines():
        service_match = re.fullmatch(r"  ([A-Za-z0-9_.-]+):\s*", line)
        if service_match is not None:
            service = service_match.group(1)
            continue
        address_match = re.fullmatch(
            r"\s{6,}ipv4_address:\s*[\"']?([^\"'\s#]+)[\"']?\s*(?:#.*)?", line,
        )
        if address_match is not None:
            if not service:
                raise StageError("candidate route fixed IP has no service role")
            rows.append((service, _ipv4(address_match.group(1), "candidate route fixed IP")))
    return rows


def _maintenance_route_preflight(
    bundle: Path,
    release: dict[str, Any],
    network: dict[str, Any],
) -> dict[str, Any]:
    bundled_runner = bundle / "rootfs/srv/menhir/production/bin/caddy-release.sh"
    # The install manifest carries the destination mode. The uploaded bundle
    # may have crossed a filesystem that cannot represent Unix execute bits.
    _safe_file(bundled_runner, "bundled route transaction runner")
    _regular_executable(LIVE_ROUTE_RUNNER, "live route transaction runner")
    _safe_file(ROUTE_ENV, "live route transaction environment")
    _safe_directory(ROUTE_CANDIDATE.parent, "live route candidate parent")
    candidate_target_state = "absent"
    if ROUTE_CANDIDATE.exists() or ROUTE_CANDIDATE.is_symlink():
        _safe_directory(ROUTE_CANDIDATE, "live route candidate target")
        candidate_target_state = "replaceable-directory"

    bundled_assets: dict[str, Path] = {}
    for name, relative in ROUTE_ASSETS.items():
        bundled = _safe_file(bundle / relative, f"bundled route asset {name}")
        bundled_assets[name] = bundled

    if ROUTE_PHASE_JOURNAL.exists() or ROUTE_PHASE_JOURNAL.is_symlink():
        raise StageError("an unfinished route transaction journal exists")

    registry = _load_json(bundled_assets["releases.json"], "bundled route registry")
    proxy = registry.get("proxy")
    if not isinstance(proxy, dict):
        raise StageError("bundled route registry has no proxy contract")
    release_network = release.get("network")
    if not isinstance(release_network, dict) \
            or proxy.get("menhir_network") != release_network.get("external_network"):
        raise StageError("candidate route network differs from release authority")

    for key in TLS_ROUTE_KEYS:
        raw = proxy.get(key)
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise StageError(f"candidate route TLS prerequisite is invalid: {key}")
        # _safe_file distinguishes directories and symlinks without reading any
        # certificate/key bytes into process output or the readiness report.
        _safe_file(Path(raw), f"candidate route TLS prerequisite {key}")

    occupants = {row["ipv4"]: row for row in _network_containers(network)}
    fixed = _candidate_fixed_ips(bundled_assets["docker-compose.yml"])
    if not fixed:
        raise StageError("candidate route design has no fixed proxy IP")
    for service, address in fixed:
        occupied = occupants.get(address)
        if occupied is None:
            continue
        live = _inspect(occupied["name"])
        labels = live.get("Config", {}).get("Labels", {})
        if not isinstance(labels, dict) \
                or labels.get("com.docker.compose.project") != "yawndeploy" \
                or labels.get("com.docker.compose.service") != service:
            raise StageError(
                f"candidate route fixed IP is occupied by another role: {address}"
            )

    return {
        "applicable": True,
        "bundle_assets": sorted(["transaction-runner", *bundled_assets]),
        "live_prerequisites": [
            "transaction-runner", "environment", "candidate-parent",
        ],
        "candidate_target_state": candidate_target_state,
        "tls_files": len(TLS_ROUTE_KEYS),
        "fixed_ip_roles": [service for service, _ in fixed],
        "fixed_ip_collisions": 0,
    }


def _cloudflared_ingress_preflight(
    network: dict[str, Any], expected_peers: set[str],
) -> dict[str, Any]:
    occupants = {
        row["ipv4"]: row for row in _network_containers(network)
        if row["ipv4"] in expected_peers
    }
    if set(occupants) != expected_peers:
        raise StageError("production Cloudflared ingress role is absent")
    containers: list[str] = []
    for address in sorted(expected_peers):
        row = occupants[address]
        value = _inspect(row["name"])
        labels = value.get("Config", {}).get("Labels", {})
        state = value.get("State", {})
        if not isinstance(labels, dict) \
                or labels.get("com.docker.compose.service") != "cloudflared" \
                or state.get("Running") is not True or state.get("Status") != "running":
            raise StageError("production ingress peer is not the running Cloudflared role")
        containers.append(row["name"])
    return {
        "mode": "cloudflared",
        "containers": containers,
        "peer_count": len(containers),
    }


def _production_preflight(
    bundle: Path,
    release: dict[str, Any],
    deployment_class: str,
) -> dict[str, Any]:
    """Inspect production read-only and return a sanitized, digest-bound report."""
    if deployment_class not in DEPLOYMENT_CLASSES:
        raise StageError("invalid deployment class")
    ingress_mode = release.get("ingress_mode")
    if ingress_mode != "cloudflared":
        raise StageError("candidate ingress mode is invalid")
    candidate_release_id = release.get("release_id")
    if not isinstance(candidate_release_id, str) \
            or RELEASE_ID_RE.fullmatch(candidate_release_id) is None:
        raise StageError("candidate release identity is invalid")
    journal = _release_journal_status(candidate_release_id)
    live_release = _load_json(PRODUCTION_RELEASE, "live production release authority")
    live_release_id = live_release.get("release_id")
    live_images = live_release.get("images")
    live_network = live_release.get("network")
    if not isinstance(live_release_id, str) \
            or RELEASE_ID_RE.fullmatch(live_release_id) is None \
            or not isinstance(live_images, dict) or not isinstance(live_network, dict):
        raise StageError("live production release authority is invalid")

    app = _healthy_container("menhir-prod-app", "menhir")
    neo4j = _healthy_container("menhir-prod-neo4j", "neo4j")
    app_digest = _container_digest(app, "menhir")
    neo4j_digest = _container_digest(neo4j, "neo4j")
    if app_digest != live_images.get("menhir") or neo4j_digest != live_images.get("neo4j"):
        raise StageError("live production container images differ from release authority")
    app_environment = _container_environment(app)
    if app_environment != {
        "MENHIR_RELEASE_ID": live_release_id,
        "MENHIR_RUNTIME_MODE": "production",
    }:
        raise StageError("live production Menhir runtime identity is invalid")

    external_network = live_network.get("external_network")
    alias = live_network.get("alias")
    peers = live_network.get("peers")
    if not isinstance(external_network, str) or not external_network \
            or not isinstance(alias, str) or not alias \
            or not isinstance(peers, list) or not peers:
        raise StageError("live production network authority is invalid")
    expected_peers = {_ipv4(value, "trusted ingress peer") for value in peers}
    network = _network_inspect(external_network)
    network_rows = _network_containers(network)
    occupied = {row["ipv4"] for row in network_rows}
    if not expected_peers.issubset(occupied):
        raise StageError("production ingress peer role is absent from the proxy network")
    ingress = _cloudflared_ingress_preflight(network, expected_peers)

    app_networks = app.get("NetworkSettings", {}).get("Networks", {})
    neo4j_networks = neo4j.get("NetworkSettings", {}).get("Networks", {})
    if not isinstance(app_networks, dict) or not isinstance(neo4j_networks, dict):
        raise StageError("production container network inspection is invalid")
    app_proxy = app_networks.get(external_network)
    if not isinstance(app_proxy, dict) or alias not in app_proxy.get("Aliases", []):
        raise StageError("production application proxy role is invalid")
    app_ipv4 = _ipv4(app_proxy.get("IPAddress"), "production application proxy address")
    if app_ipv4 in expected_peers:
        raise StageError("production application and ingress roles share an address")
    internal_networks = (set(app_networks) & set(neo4j_networks)) - {external_network}
    if not internal_networks or external_network in neo4j_networks:
        raise StageError("production database network role is invalid")

    disk_free = shutil.disk_usage(PRODUCTION_ROOT).free
    memory_available = _available_memory_bytes()
    if disk_free < MIN_STAGING_DISK_BYTES:
        raise StageError("insufficient disk headroom for isolated staging")
    if memory_available < MIN_STAGING_MEMORY_BYTES:
        raise StageError("insufficient memory headroom for isolated staging")

    maintenance = {"applicable": False}
    if deployment_class == "maintenance":
        maintenance = {
            "applicable": True,
            "ingress_mode": "cloudflared",
            "route_mutation": False,
            "reason": "Cloudflared ingress is retained by the maintenance transaction",
        }

    report = {
        "schema": PREFLIGHT_SCHEMA,
        "kind": "menhir-production-readiness-preflight",
        "result": "passed",
        "observed_utc": _now(),
        "deployment_class": deployment_class,
        "candidate_release_id": candidate_release_id,
        "ingress_mode": ingress_mode,
        "checks": {
            "live_services": {
                "release_id": live_release_id,
                "menhir": {
                    "container": "menhir-prod-app",
                    "image_digest": app_digest,
                    "health": "healthy",
                    "memory_limit_bytes": PRODUCTION_MEMORY_LIMITS["menhir"],
                },
                "neo4j": {
                    "container": "menhir-prod-neo4j",
                    "image_digest": neo4j_digest,
                    "health": "healthy",
                    "memory_limit_bytes": PRODUCTION_MEMORY_LIMITS["neo4j"],
                },
            },
            "network_roles": {
                "external_network": external_network,
                "application_alias": alias,
                "application_ipv4": app_ipv4,
                "internal_network_count": len(internal_networks),
                "ingress_peer_count": len(expected_peers),
                "ingress": ingress,
            },
            "release_journal": journal,
            "headroom": {
                "disk_free_bytes": disk_free,
                "disk_required_bytes": MIN_STAGING_DISK_BYTES,
                "memory_available_bytes": memory_available,
                "memory_required_bytes": MIN_STAGING_MEMORY_BYTES,
            },
            "maintenance_route": maintenance,
        },
    }
    return _seal_preflight(report)


def _oauth_token(port: int, operator_key: str) -> str:
    verifier = "stage-" + secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = "https://client.staging.invalid/callback"
    scope = "menhir:read menhir:write menhir:admin"
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": STAGING_CLIENT,
        "redirect_uri": redirect,
        "scope": scope,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "staging-state",
    })
    status, body, headers = _http(port, "GET", "/oauth/authorize?" + query, headers={"Accept": "text/html"})
    if status != 200:
        raise StageError(f"OAuth authorize GET failed: HTTP {status}: {body[:300]!r}")
    text = body.decode("utf-8")
    fields = {
        match.group(1): html.unescape(match.group(2))
        for match in re.finditer(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', text)
    }
    if "consent_token" not in fields:
        raise StageError("OAuth authorize response omitted consent token")
    fields.update({"decision": "approve", "admin_secret": operator_key})
    encoded = urllib.parse.urlencode(fields).encode("ascii")
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html",
    }
    if "set-cookie" in headers:
        request_headers["Cookie"] = headers["set-cookie"].split(";", 1)[0]
    status, body, headers = _http(port, "POST", "/oauth/authorize", body=encoded, headers=request_headers)
    if status not in {302, 303}:
        raise StageError(f"OAuth approval failed: HTTP {status}: {body[:300]!r}")
    location = headers.get("location", "")
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    code = values.get("code", [""])[0]
    if not code or values.get("state", [""])[0] != "staging-state":
        raise StageError("OAuth approval redirect omitted the bound code/state")
    token_form = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect,
        "client_id": STAGING_CLIENT,
        "code_verifier": verifier,
        "resource": STAGING_BASE + "/mcp-http",
    }).encode("ascii")
    status, body, _ = _http(
        port, "POST", "/oauth/token", body=token_form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        raise StageError(f"OAuth token exchange failed: HTTP {status}: {body[:300]!r}")
    token = json.loads(body).get("access_token")
    if not isinstance(token, str) or not token:
        raise StageError("OAuth token exchange returned no access token")
    return token


def _decode_mcp(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8")
    if "text/event-stream" in content_type:
        rows = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not rows:
            raise StageError("MCP event stream had no data")
        value = json.loads(rows[-1])
    else:
        value = json.loads(text) if text else {}
    if not isinstance(value, dict):
        raise StageError("MCP returned non-object JSON")
    return value


def _mcp(port: int, token: str, payload: dict[str, Any], session: str = "") -> tuple[int, dict[str, Any], str]:
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    status, body, response_headers = _http(
        port, "POST", "/mcp-http",
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
    )
    return status, _decode_mcp(body, response_headers.get("content-type", "")), response_headers.get("mcp-session-id", session)


def _mcp_success(status: int, value: dict[str, Any], label: str) -> None:
    if status // 100 != 2 or value.get("error") or value.get("result", {}).get("isError") is True:
        raise StageError(f"{label} failed: HTTP {status}: {value}")


def _is_exact_allowlist_denial(status: int, value: dict[str, Any], tool: str) -> bool:
    """Recognize Menhir's fail-closed BaseJsonTool permission response exactly."""
    if status // 100 != 2:
        return False
    content = value.get("result", {}).get("content", [])
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
        return False
    if content[0].get("type") != "text" or not isinstance(content[0].get("text"), str):
        return False
    try:
        payload = json.loads(content[0]["text"])
    except json.JSONDecodeError:
        return False
    expected = {
        "ok": False,
        "tool": tool,
        "error": {
            "message": (
                f"PermissionError: Client is not permitted to invoke `{tool}` "
                "(restricted by MENHIR_CLIENT_TOOLS allowlist)"
            )
        },
    }
    return payload == expected


def _exercise(port: int, token: str, unique: str) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": STAGING_CLIENT, "version": "1"}},
    })
    _mcp_success(status, value, "MCP initialize")
    checks["mcp_initialize"] = True
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, session)
    if status // 100 != 2:
        raise StageError("MCP initialized notification failed")
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, session)
    _mcp_success(status, value, "MCP tools/list")
    tools = {row.get("name") for row in value.get("result", {}).get("tools", [])}
    allowed_probes = {"add_todo", "list_todos", "recall_memories"}
    if not allowed_probes.issubset(tools):
        raise StageError(
            "MCP tool catalog omitted allowed staging probes: "
            + ", ".join(sorted(allowed_probes - tools))
        )
    checks["mcp_tools_list"] = True
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "add_todo", "arguments": {"text": unique, "namespace": STAGING_NAMESPACE}},
    }, session)
    _mcp_success(status, value, "staging synthetic write")
    checks["synthetic_write_allowed"] = True
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "recall_memories", "arguments": {"query": unique, "limit": 1, "namespace": STAGING_NAMESPACE}},
    }, session)
    _mcp_success(status, value, "staging recall")
    checks["mcp_recall"] = True
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "delete_namespace", "arguments": {"namespace": STAGING_NAMESPACE, "confirm": True}},
    }, session)
    denied = _is_exact_allowlist_denial(status, value, "delete_namespace")
    if not denied:
        raise StageError(f"policy-denied operation was not refused exactly: {value}")
    checks["denied_operation_refused"] = True
    return checks


def _todo_persists(port: int, token: str, unique: str) -> None:
    status, value, session = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 11, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": STAGING_CLIENT, "version": "1"}},
    })
    _mcp_success(status, value, "post-restart MCP initialize")
    _mcp(port, token, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session)
    status, value, _ = _mcp(port, token, {
        "jsonrpc": "2.0", "id": 12, "method": "tools/call",
        "params": {"name": "list_todos", "arguments": {"namespace": STAGING_NAMESPACE}},
    }, session)
    _mcp_success(status, value, "post-restart list_todos")
    if unique not in json.dumps(value, sort_keys=True):
        raise StageError("synthetic write did not persist across restart")


def _runtime_contract(root: Path, runtime_images: dict[str, str], subnet: str) -> None:
    app = _inspect(f"menhir-stage-{root.name}-app")
    neo4j = _inspect(f"menhir-stage-{root.name}-neo4j")
    if app.get("Image") != runtime_images["menhir"] \
            or neo4j.get("Image") != runtime_images["neo4j"]:
        raise StageError("staging containers differ from release image authority")
    if app.get("HostConfig", {}).get("Memory") != 2 * 1024**3 \
            or neo4j.get("HostConfig", {}).get("Memory") != 4 * 1024**3:
        raise StageError("staging memory limits differ from production")
    forbidden = str(Path("/srv/menhir/production"))
    for container in (app, neo4j):
        for mount in container.get("Mounts", []):
            source = str(mount.get("Source", ""))
            if source == forbidden or source.startswith(forbidden + "/"):
                raise StageError("staging mounted production authority")
            if source.startswith(str(root)) is False:
                raise StageError(f"staging mount escaped disposable root: {source}")
    proxy_network = next((name for name in app.get("NetworkSettings", {}).get("Networks", {}) if name.startswith("menhir-stage-")), "")
    address = app.get("NetworkSettings", {}).get("Networks", {}).get(proxy_network, {}).get("IPAddress")
    if address != subnet.removesuffix("0/24") + "3":
        raise StageError("staging proxy network shape differs from its contract")


def _ensure_images(
    source_environment: dict[str, str],
    release: dict[str, Any],
) -> dict[str, str]:
    # The desktop wrapper loads the exact application image for private
    # registries. Pull immutable references only when they are not already
    # present, then verify every image before creating disposable state.
    menhir = source_environment["MENHIR_IMAGE"]
    present = _run("docker", "image", "inspect", menhir, check=False)
    if present.returncode != 0:
        transferred_tag = menhir.split("@", 1)[0]
        transferred = _run(
            "docker", "image", "inspect", transferred_tag, check=False,
        )
        if transferred.returncode == 0:
            menhir = transferred_tag
            source_environment["MENHIR_IMAGE"] = transferred_tag
        else:
            _run("docker", "pull", source_environment["MENHIR_IMAGE"])

    neo4j = source_environment["NEO4J_IMAGE"]
    if _run("docker", "image", "inspect", neo4j, check=False).returncode != 0:
        _run("docker", "pull", neo4j)

    def image_id(reference: str) -> str:
        value = json.loads(_run("docker", "image", "inspect", reference).stdout)
        if not isinstance(value, list) or len(value) != 1 \
                or not isinstance(value[0], dict):
            raise StageError(f"image inspection is invalid: {reference}")
        identity = value[0].get("Id")
        if not isinstance(identity, str) or IMAGE_RE.fullmatch(identity) is None:
            raise StageError(f"image ID is invalid: {reference}")
        return identity

    runtime_images = {
        "menhir": image_id(menhir),
        "neo4j": image_id(neo4j),
        "caddy": image_id(release["images"]["caddy"]),
    }
    menhir_value = json.loads(_run("docker", "image", "inspect", menhir).stdout)[0]
    labels = menhir_value.get("Config", {}).get("Labels", {})
    expected_labels = {
        "org.opencontainers.image.revision": release["repos"]["menhir"],
        "org.archolith.menhir.wheel-manifest.sha256": release[
            "dockerfile_wheel_manifest_sha256"
        ],
        "org.archolith.oauth.wheel.sha256": release["oauth_wheel_sha256"],
    }
    if not isinstance(labels, dict) or any(
        labels.get(name) != value for name, value in expected_labels.items()
    ):
        raise StageError("transferred Menhir image labels differ from release authority")
    return runtime_images


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise StageError("VPS staging runner must run as root")
    for value, pattern, label in (
        (args.expected_bundle_sha256, SHA256_RE, "bundle digest"),
        (args.expected_release_sha256, SHA256_RE, "release digest"),
        (args.runner_sha256, SHA256_RE, "runner digest"),
        (args.expected_release_id, RELEASE_ID_RE, "release ID"),
        (
            args.expected_menhir_image_archive_sha256,
            SHA256_RE,
            "Menhir image archive digest",
        ),
    ):
        if pattern.fullmatch(value) is None:
            raise StageError(f"invalid expected {label}")
    if args.deployment_class not in DEPLOYMENT_CLASSES:
        raise StageError("invalid deployment class")
    bundle = args.bundle.resolve()
    receipt = args.receipt.resolve()
    _safe_file(bundle / "bundle-manifest.json", "bundle manifest")
    if receipt.exists() or receipt.is_symlink():
        raise StageError("staging receipt already exists")
    release, source_environment, _ = _validate_bundle(
        bundle,
        args.expected_bundle_sha256,
        args.expected_release_id,
        args.expected_release_sha256,
    )
    production_preflight = _production_preflight(
        bundle, release, args.deployment_class,
    )
    started = _now()
    archive = args.menhir_image_archive.resolve()
    _safe_file(archive, "transferred Menhir image archive")
    if _sha256(archive) != args.expected_menhir_image_archive_sha256:
        raise StageError("transferred Menhir image archive digest mismatch")
    runtime_images = _ensure_images(source_environment, release)

    production_before = _production_snapshot()
    run_id = secrets.token_hex(6)
    root = STAGING_ROOT / run_id
    project = f"menhir-stage-{run_id}"
    subnet = _select_subnet()
    base: Path | None = None
    override: Path | None = None
    environment: dict[str, str] | None = None
    checks = {name: False for name in CHECKS}
    try:
        STAGING_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chown(STAGING_ROOT, 0, 0)
        policy_digest, operator_key = _prepare_tree(root, bundle, release, source_environment)
        base, override = _compose_files(
            root, bundle, release, source_environment, subnet,
        )
        environment = _stage_environment(root, source_environment, policy_digest, project, subnet)
        _compose(base, override, project, environment, "config", "--quiet")
        app = f"menhir-stage-{run_id}-app"
        neo4j = f"menhir-stage-{run_id}-neo4j"
        proxy = f"menhir-stage-{run_id}-proxy"
        _compose(
            base, override, project, environment,
            "up", "-d", "--remove-orphans", "neo4j", "fake-llm", "staging-proxy",
        )
        _wait_healthy((neo4j,))
        port = _proxy_port(proxy)
        _prime_ingress_certificate(port)
        caddy_ca = root / "caddy-data/caddy/pki/authorities/local/root.crt"
        _wait_file(caddy_ca)
        _write(root / "staging-ca.crt", caddy_ca.read_text(encoding="utf-8"), 0o440, 0, 10001)
        _bootstrap_blank_schema(base, override, project, environment)
        _compose(base, override, project, environment, "up", "-d", "--remove-orphans")
        _wait_healthy((app, neo4j))
        _runtime_contract(root, runtime_images, subnet)
        checks.update({
            "artifact_identity": True,
            "production_memory_limits": True,
            "production_network_shape": True,
            "oauth_policy_shape": True,
            "isolated_disposable_data": True,
            "non_production_credentials": True,
            "production_authority_absent": True,
        })
        ready = _json_response(port, "/readyz")
        if ready.get("status") != "ready" or ready.get("mode") != "production" \
                or ready.get("mutation_fence") is not False:
            raise StageError(f"staging ingress readiness failed: {ready}")
        checks["ingress_request_handling"] = True
        jwks = _json_response(port, "/.well-known/jwks.json")
        authorization = _json_response(port, "/.well-known/oauth-authorization-server")
        protected = _json_response(port, "/.well-known/oauth-protected-resource")
        if not jwks.get("keys") or authorization.get("issuer") != STAGING_BASE \
                or protected.get("resource") != STAGING_BASE + "/mcp-http":
            raise StageError("OAuth discovery identity mismatch")
        checks["oauth_discovery"] = True
        token = _oauth_token(port, operator_key)
        checks["oauth_authorization_code_pkce"] = True
        unique = "Menhir isolated staging write " + run_id
        checks.update(_exercise(port, token, unique))
        _compose(base, override, project, environment, "restart", "menhir")
        _wait_healthy((app, neo4j))
        _todo_persists(port, token, unique)
        checks["restart_persistence"] = True

        _compose(base, override, project, environment, "stop", "menhir")
        broken = f"menhir-stage-{run_id}-failed-candidate"
        failed = _run(
            "docker", "run", "--name", broken, "--network", "none",
            "--entrypoint", "/bin/false", source_environment["MENHIR_IMAGE"],
            check=False,
        )
        _run("docker", "rm", "-f", broken, check=False)
        if failed.returncode == 0:
            raise StageError("deliberately broken replacement unexpectedly succeeded")
        _compose(base, override, project, environment, "up", "-d", "menhir")
        _wait_healthy((app, neo4j))
        _todo_persists(port, token, unique)
        checks["automatic_rollback"] = True
        if _production_snapshot() != production_before:
            raise StageError("production authority or container identity changed during staging")
        if any(value is not True for value in checks.values()):
            raise StageError(f"staging suite incomplete: {checks}")
        result = {
            "schema": 1,
            "kind": "menhir-personal-staging",
            "result": "passed",
            "release_id": args.expected_release_id,
            "release_sha256": args.expected_release_sha256,
            "bundle_sha256": args.expected_bundle_sha256,
            "deployment_class": args.deployment_class,
            "ingress_mode": release["ingress_mode"],
            "images": {
                "menhir": release["images"]["menhir"],
                "neo4j": release["images"]["neo4j"],
            },
            "runner_sha256": args.runner_sha256,
            "started_utc": started,
            "completed_utc": _now(),
            "test_identities": {
                "oauth_client_id": STAGING_CLIENT,
                "subject": STAGING_SUBJECT,
                "namespace": STAGING_NAMESPACE,
            },
            "production_preflight": production_preflight,
            "checks": checks,
        }
        _atomic_json(receipt, result)
        return result
    finally:
        if base is not None and override is not None and environment is not None:
            _compose(base, override, project, environment, "down", "--volumes", "--remove-orphans", check=False)
        if root.exists():
            resolved = root.resolve()
            if resolved.parent != STAGING_ROOT or not resolved.name:
                raise StageError("refusing unsafe staging cleanup")
            shutil.rmtree(resolved)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--expected-bundle-sha256", required=True)
    parser.add_argument("--expected-release-id", required=True)
    parser.add_argument("--expected-release-sha256", required=True)
    parser.add_argument("--menhir-image-archive", type=Path, required=True)
    parser.add_argument("--expected-menhir-image-archive-sha256", required=True)
    parser.add_argument("--deployment-class", required=True)
    parser.add_argument("--runner-sha256", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main() -> int:
    try:
        result = run_stage(_parser().parse_args())
    except (OSError, ValueError, StageError, subprocess.TimeoutExpired) as exc:
        print(f"personal staging failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
