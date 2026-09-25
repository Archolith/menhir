"""Container inspection, image pull, and HTTP/MCP probe transport."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from menhir_app_only_core import AppOnlyError, atomic_bytes, require_upload, run, strict_load

PROBE_TOKEN_SCRIPT = r'''import json
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


def inspect_container(name: str) -> dict[str, Any]:
    value = json.loads(run(["docker", "inspect", name], 20))
    if not isinstance(value, list) or len(value) != 1:
        raise AppOnlyError(f"Docker inspection is ambiguous: {name}")
    return value[0]


def inspect_cloudflared() -> dict[str, Any]:
    """Return the sole running Cloudflared peer on the production network."""

    network = json.loads(run(["docker", "network", "inspect", "menhir-proxy"], 20))
    if not isinstance(network, list) or len(network) != 1:
        raise AppOnlyError("production network inspection is ambiguous")
    peers: list[dict[str, Any]] = []
    for row in (network[0].get("Containers") or {}).values():
        container_id = row.get("Name")
        if not isinstance(container_id, str) or not container_id:
            continue
        inspected = inspect_container(container_id)
        labels = inspected.get("Config", {}).get("Labels", {}) or {}
        if labels.get("com.docker.compose.service") == "cloudflared" \
                and inspected.get("State", {}).get("Running") is True:
            peers.append(inspected)
    if len(peers) != 1:
        raise AppOnlyError("expected exactly one running Cloudflared ingress peer")
    return peers[0]


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
