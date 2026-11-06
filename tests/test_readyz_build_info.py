from __future__ import annotations

import http.server
import importlib.util
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import menhir
from menhir.api import production_routes
from menhir.api.production_routes import readyz
from menhir.mcp import served_metadata
from menhir.mcp.served_metadata import release_commit, served_mcp_metadata_digest

SCRIPT = Path(__file__).parents[1] / "scripts" / "release" / "check_served_metadata.py"
_spec = importlib.util.spec_from_file_location("check_served_metadata", SCRIPT)
check_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_script)


@pytest.fixture(autouse=True)
def _fresh_cache():
    served_metadata._reset_digest_cache()
    yield
    served_metadata._reset_digest_cache()


@pytest.fixture(autouse=True)
def _caller_has_a_token(monkeypatch):
    """These tests cover the build block's content; test_readyz_build_access covers who sees it."""

    async def _yes(_request) -> bool:
        return True

    monkeypatch.setattr(production_routes, "_caller_may_see_build", _yes)


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


def _request(*, ready: bool = True):
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="source-prod-a"),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=ready, enrichment_ready=True),
            scheduler=object(),
        ),
        oauth_signing_key=object(),
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


GOOD_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.asyncio
async def test_readyz_reports_build_block(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", GOOD_SHA)
    response = await readyz(_request())
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["build"]["version"] == menhir.__version__
    assert body["build"]["commit"] == GOOD_SHA
    assert re.fullmatch(r"[0-9a-f]{64}", body["build"]["mcp_metadata_sha256"])
    assert "instance_id" not in response.body.decode()


@pytest.mark.parametrize(
    "value",
    [None, "", "unknown", "abc", "ABCDEF1234567", "a" * 41, "abc def1234", "abcdefg"],
)
def test_release_commit_unknown_when_invalid(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("MENHIR_RELEASE_COMMIT", raising=False)
    else:
        monkeypatch.setenv("MENHIR_RELEASE_COMMIT", value)
    assert release_commit() == "unknown"


@pytest.mark.parametrize("value", ["abcdef1", "a" * 40, f"  {GOOD_SHA}\n"])
def test_release_commit_accepts_hex(monkeypatch, value) -> None:
    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", value)
    assert release_commit() == value.strip()


@pytest.mark.asyncio
async def test_build_block_does_not_change_not_ready_status(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_RELEASE_COMMIT", raising=False)
    response = await readyz(_request(ready=False))
    body = json.loads(response.body)
    assert response.status_code == 503
    assert body["failures"] == ["neo4j_unavailable"]
    assert body["build"]["commit"] == "unknown"


@pytest.mark.asyncio
async def test_digest_is_stable_hex_and_tracks_instructions(monkeypatch) -> None:
    first = await served_mcp_metadata_digest()
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    served_metadata._reset_digest_cache()
    assert await served_mcp_metadata_digest() == first
    assert await served_mcp_metadata_digest() == first  # cached

    from menhir.mcp import instructions

    monkeypatch.setattr(instructions, "SERVER_INSTRUCTIONS", instructions.SERVER_INSTRUCTIONS + " x")
    served_metadata._reset_digest_cache()
    assert await served_mcp_metadata_digest() != first


@pytest.mark.asyncio
async def test_digest_failure_reports_unavailable_without_changing_readiness(monkeypatch) -> None:
    async def boom() -> str:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(served_metadata, "_compute_digest", boom)
    response = await readyz(_request())
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "ready"
    assert body["build"]["mcp_metadata_sha256"] == "unavailable"
    assert "secret detail" not in response.body.decode()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(self.payload).encode()
        self.send_response(200 if self.path == "/readyz" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def fake_server():
    servers = []

    def start(payload: dict) -> str:
        handler = type("H", (_Handler,), {"payload": payload})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _payload(digest: str, version: str | None = None, commit: str = "unknown") -> dict:
    return {
        "build": {
            "version": version or menhir.__version__,
            "commit": commit,
            "mcp_metadata_sha256": digest,
        }
    }


@pytest.mark.asyncio
async def test_check_script_exit_codes(fake_server, capsys) -> None:
    import asyncio

    digest = await served_mcp_metadata_digest()
    ok_url = fake_server(_payload(digest, commit=GOOD_SHA))
    bad_url = fake_server(_payload("0" * 64))

    def run(*args: str) -> int:
        return check_script.main(list(args))

    # main() uses asyncio.run, so call it off the running loop.
    loop = asyncio.get_running_loop()
    assert await loop.run_in_executor(None, run, ok_url, "--expect-commit", GOOD_SHA) == 0
    assert await loop.run_in_executor(None, run, ok_url, "--expect-commit", "f" * 40) == 1
    assert await loop.run_in_executor(None, run, bad_url) == 1
    assert await loop.run_in_executor(None, run, ok_url, "--expect-version", "9.9.9") == 1
    assert await loop.run_in_executor(None, run, "http://127.0.0.1:9", "--timeout", "1") == 2
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "ok" in out
