"""Strict Streamable-HTTP MCP acceptance probe used by candidate-accept.sh."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


PROBE_CLIENT_ID = "menhir-deploy-probe"
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _decode(body: bytes, content_type: str) -> dict:
    if not body:
        return {}
    text = body.decode("utf-8")
    if "text/event-stream" in content_type:
        payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if not payloads:
            raise RuntimeError("MCP SSE response contained no data event")
        return json.loads(payloads[-1])
    return json.loads(text)


def _post(url: str, token: str, payload: dict, session: str = "") -> tuple[int, dict, str]:
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(
        url, data=json.dumps(payload, separators=(",", ":")).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return (
                response.status,
                _decode(response.read(), response.headers.get("Content-Type", "")),
                response.headers.get("Mcp-Session-Id", session),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        parsed = {}
        if body:
            try:
                parsed = _decode(body, exc.headers.get("Content-Type", ""))
            except Exception:
                parsed = {"body": body.decode("utf-8", "replace")[:500]}
        return exc.code, parsed, session


def _require_success(status: int, response: dict, label: str) -> None:
    if status // 100 != 2 or response.get("error"):
        raise RuntimeError(f"{label} failed: HTTP {status}: {response}")


def _run(command: list[str], *, input_bytes: bytes | None = None) -> str:
    result = subprocess.run(
        command, input=input_bytes, capture_output=True, check=False, timeout=20,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-1000:].strip()
        raise RuntimeError(f"command failed ({result.returncode}): {command[0]}: {stderr}")
    return result.stdout.decode("utf-8", "replace").strip()


def _load_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return value


def _inspect_container(name: str) -> dict:
    value = json.loads(_run(["docker", "inspect", name]))
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError(f"Docker inspection is ambiguous: {name}")
    return value[0]


def _get_json(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "Menhir-Release-Accept/1"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"HTTP acceptance returned non-object JSON: {url}")
    return value


def _require_probe_policy(policy: dict) -> None:
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
        raise RuntimeError("production policy lacks the exact read-only deploy-probe identity")
    denied = probe.get("denied_tools")
    if not isinstance(denied, list) or not denied or "recall_memories" in denied:
        raise RuntimeError("deploy-probe deny boundary is invalid")


def _mint_probe_token() -> str:
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
    token = _run(
        ["docker", "exec", "-i", "menhir-prod-app", "python", "-"],
        input_bytes=script.encode("ascii"),
    )
    if token.count(".") != 2 or any(character.isspace() for character in token):
        raise RuntimeError("deploy-probe token mint returned malformed output")
    return token


def _candidate_accept(base_url: str, token: str) -> None:
    endpoint = base_url.rstrip("/") + "/mcp-http"
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "menhir-candidate-accept", "version": "1"}},
    })
    _require_success(status, response, "initialize")
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {}
    }, session)
    if status // 100 != 2:
        raise RuntimeError(f"initialized notification failed: HTTP {status}")
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    }, session)
    _require_success(status, response, "tools/list")
    tools = {item.get("name") for item in response.get("result", {}).get("tools", [])}
    if "recall_memories" not in tools:
        raise RuntimeError("recall_memories is absent from the accepted client tool catalog")
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recall_memories", "arguments": {
            "query": "Menhir production migration acceptance", "limit": 1}},
    }, session)
    _require_success(status, response, "recall_memories")
    if response.get("result", {}).get("isError") is True:
        raise RuntimeError("recall_memories returned a tool error")

    # A real write invocation must be refused by the candidate mutation fence.
    # Only an explicit refusal is accepted: an HTTP 503 carrying the fenced
    # contract, or a JSON-RPC error, or an isError tool result. A genuine
    # success (HTTP 2xx, no error, no isError) means the fence is NOT active and
    # the probe must fail.
    status, response, _ = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "add_memory", "arguments": {
            "text": "candidate acceptance probe -- must never persist",
            "source": "menhir-candidate-accept"}},
    }, session)
    is_http_fence = (
        status == 503
        and isinstance(response, dict)
        and response.get("error") == "temporarily_unavailable"
    )
    is_jsonrpc_error = bool(response.get("error"))
    is_tool_error = bool(response.get("result", {}).get("isError"))
    if not (is_http_fence or (status // 100 == 2 and (is_jsonrpc_error or is_tool_error))):
        raise RuntimeError(
            f"candidate mutation was not explicitly refused: HTTP {status}: {response}")
    print("MCP recall succeeded and mutation was refused")


def _production_accept(base_url: str, release_path: Path, policy_path: Path) -> None:
    release = _load_object(release_path, "release authority")
    policy = _load_object(policy_path, "client policy")
    release_id = release.get("release_id")
    image_digest = release.get("images", {}).get("menhir")
    if not isinstance(release_id, str) or not isinstance(image_digest, str) \
            or not IMAGE_DIGEST.fullmatch(image_digest):
        raise RuntimeError("release acceptance identity is invalid")
    _require_probe_policy(policy)

    base = base_url.rstrip("/")
    ready = _get_json(base + "/readyz")
    if ready.get("status") != "ready" or ready.get("mode") != "production" \
            or ready.get("mutation_fence") is not False:
        raise RuntimeError("public readiness is not writable production mode")
    _get_json(base + "/livez")
    _get_json(base + "/.well-known/jwks.json")

    database_before = _inspect_container("menhir-prod-neo4j")
    app = _inspect_container("menhir-prod-app")
    app_environment = app.get("Config", {}).get("Env", []) or []
    if app.get("State", {}).get("Health", {}).get("Status") != "healthy" \
            or not str(app.get("Config", {}).get("Image", "")).endswith("@" + image_digest) \
            or f"MENHIR_RELEASE_ID={release_id}" not in app_environment:
        raise RuntimeError("production runtime does not match the release authority")
    if database_before.get("State", {}).get("Health", {}).get("Status") != "healthy":
        raise RuntimeError("production database is not healthy")

    token = _mint_probe_token()
    endpoint = base + "/mcp-http"
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "menhir-release-accept", "version": "1"}},
    })
    _require_success(status, response, "initialize")
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
    }, session)
    if status // 100 != 2:
        raise RuntimeError(f"initialized notification failed: HTTP {status}")
    status, response, session = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }, session)
    _require_success(status, response, "tools/list")
    tools = {item.get("name") for item in response.get("result", {}).get("tools", [])}
    if "recall_memories" not in tools:
        raise RuntimeError("deploy-probe cannot see recall_memories")
    status, response, _ = _post(endpoint, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recall_memories", "arguments": {
            "query": "Menhir production release acceptance", "limit": 1}},
    }, session)
    _require_success(status, response, "recall_memories")
    if response.get("result", {}).get("isError") is True:
        raise RuntimeError("recall_memories returned a tool error")
    if _inspect_container("menhir-prod-neo4j").get("Id") != database_before.get("Id"):
        raise RuntimeError("production database changed during acceptance")
    print(f"production acceptance succeeded: {release_id}")


def main() -> int:
    if len(sys.argv) == 3:
        _candidate_accept(sys.argv[1], sys.argv[2])
        return 0
    if len(sys.argv) == 5 and sys.argv[1] == "production":
        _production_accept(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))
        return 0
    print(
        "usage: mcp_acceptance_probe.py <base-url> <bearer-token> | "
        "production <base-url> <release-json> <policy-json>",
        file=sys.stderr,
    )
    return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
