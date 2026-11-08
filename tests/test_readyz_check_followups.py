"""Follow-ups to #474: digest caching, coverage, served object, and the check script's exit codes."""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import inspect
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import menhir
from menhir.api import production_routes
from menhir.api.production_routes import readyz
from menhir.config.snapshot_mode import SNAPSHOT_RECEIVE_MODE_ENV
from menhir.mcp import served_metadata
from menhir.mcp.served_metadata import compute_metadata, served_mcp_metadata_digest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "release" / "check_served_metadata.py"
_spec = importlib.util.spec_from_file_location("check_served_metadata_followups", SCRIPT)
check_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_script)

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fresh_cache_and_mode(monkeypatch):
    # setenv records the original state, so the mode is restored even if a test leaks it.
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "off")
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


def _state(**extra):
    return SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
            scheduler=object(),
        ),
        oauth_signing_key=object(),
        **extra,
    )


# --- digest -------------------------------------------------------------------------------


async def test_digest_is_computed_once_per_process(monkeypatch) -> None:
    calls = []
    real = served_metadata._compute_digest

    async def counting(*args):
        calls.append(args)
        return await real(*args)

    monkeypatch.setattr(served_metadata, "_compute_digest", counting)
    request = SimpleNamespace(app=SimpleNamespace(state=_state()))
    first = await asyncio.gather(*(served_mcp_metadata_digest() for _ in range(10)))
    for _ in range(3):
        response = await readyz(request)
        assert json.loads(response.body)["build"]["mcp_metadata_sha256"] == first[0]
    assert len(set(first)) == 1
    assert len(calls) == 1


async def test_a_changed_tool_description_changes_the_digest(monkeypatch) -> None:
    from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool

    before = await compute_metadata()
    monkeypatch.setattr(
        RecallMemoriesTool, "description", RecallMemoriesTool.description + " Changed."
    )
    after = await compute_metadata()
    assert after.tool_count == before.tool_count
    assert after.sha256 != before.sha256


async def test_snapshot_receive_mode_changes_the_digest(monkeypatch) -> None:
    off = await compute_metadata()
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "receive")
    receive = await compute_metadata()
    assert receive.tool_count == off.tool_count + 4
    assert receive.sha256 != off.sha256


async def test_description_indentation_does_not_change_the_digest(monkeypatch) -> None:
    """3.12 keeps docstring indentation and 3.13 strips it; the digest must not care.

    Simulates the 3.13 compiler by cleaning every endpoint docstring before the catalog is built.
    """
    from menhir.mcp.tools import registered_tools

    reference = await compute_metadata()
    changed = 0
    for tool_cls in registered_tools():
        endpoint = tool_cls.endpoint
        if endpoint.__doc__ and inspect.cleandoc(endpoint.__doc__) != endpoint.__doc__.strip():
            monkeypatch.setattr(endpoint, "__doc__", inspect.cleandoc(endpoint.__doc__))
            changed += 1
    if sys.version_info < (3, 13):
        assert changed  # the simulation must change something, or this test proves nothing
    assert (await compute_metadata()).sha256 == reference.sha256


async def test_digest_hashes_the_passed_server_and_readyz_uses_the_served_instance() -> None:
    from menhir.api.mcp_remote import create_mcp_streamable_http_app

    twin = await compute_metadata()
    _, served = create_mcp_streamable_http_app()
    assert (await compute_metadata(served)).sha256 == twin.sha256

    served._mcp_server.instructions = "different served text"
    expected = (await compute_metadata(served)).sha256
    assert expected != twin.sha256
    response = await readyz(
        SimpleNamespace(app=SimpleNamespace(state=_state(mcp_http_instance=served)))
    )
    assert json.loads(response.body)["build"]["mcp_metadata_sha256"] == expected


def test_create_app_exposes_the_served_mcp_instance(tmp_path: Path) -> None:
    from menhir.api.server import create_app
    from tests.test_production_runtime_surface import _production_settings

    wrapped = create_app(
        settings=_production_settings(
            oauth_as_dir=str(tmp_path / "oauth"),
            oauth_signing_key_path=str(tmp_path / "oauth-signing-key.test.json"),
        )
    )
    instance = wrapped.app.app.state.mcp_http_instance
    assert type(instance).__name__ == "TierFilteredFastMCP"
    assert instance.session_manager is not None


# --- check script ---------------------------------------------------------------------------


def _local_digest(mode: str = "off") -> str:
    previous = os.environ.get(SNAPSHOT_RECEIVE_MODE_ENV)
    os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = mode
    try:
        return asyncio.run(compute_metadata()).sha256
    finally:
        os.environ[SNAPSHOT_RECEIVE_MODE_ENV] = previous or "off"


@pytest.fixture(scope="module")
def local_digest() -> str:
    return _local_digest()


def _build(digest: str, *, version: str | None = None, commit: str = SHA) -> dict:
    return {
        "build": {
            "version": menhir.__version__ if version is None else version,
            "commit": commit,
            "mcp_metadata_sha256": digest,
        }
    }


@pytest.fixture
def serve():
    servers = []

    def start(payload, status: int = 200) -> str:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("token", [None, "secret-token"])
def test_check_sends_its_own_user_agent(token) -> None:
    # Cloudflare answers 403 to urllib's default "Python-urllib/x.y" agent; both openers set ours.
    seen: list[str | None] = []
    body = json.dumps({"status": "ready"}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.headers.get("User-Agent"))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert check_script.fetch_readyz(url, 2, token) == {"status": "ready"}
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [check_script.USER_AGENT]
    assert not seen[0].startswith("Python-urllib")


@pytest.fixture
def raw_peer():
    """A loopback peer that answers every connection with fixed bytes (not necessarily HTTP)."""
    sockets = []

    def start(reply: bytes) -> str:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        sockets.append(listener)

        def run() -> None:
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.recv(4096)
                conn.sendall(reply)
                conn.close()

        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    yield start
    for listener in sockets:
        listener.close()


def run(*args: str) -> int:
    return check_script.main(list(args))


def test_commit_matches_as_a_case_insensitive_prefix(serve, local_digest) -> None:
    url = serve(_build(local_digest))
    assert run(url, "--expect-commit", SHA[:7]) == 0
    assert run(url, "--expect-commit", SHA.upper()) == 0
    assert run(url, "--expect-commit", "fedcba9") == 1


def test_served_unknown_commit_is_a_mismatch_when_one_is_expected(serve, local_digest) -> None:
    url = serve(_build(local_digest, commit="unknown"))
    assert run(url) == 0
    assert run(url, "--expect-commit", SHA) == 1


@pytest.mark.parametrize(
    "args",
    [
        ("--expect-commit", ""),
        ("--expect-commit", "abc"),
        ("--expect-commit", "g" * 40),
        ("--expect-commit", "a" * 41),
        ("--expect-version", ""),
        ("--snapshot-mode", "recieve"),
    ],
)
def test_bad_arguments_are_errors_not_mismatches(serve, local_digest, args) -> None:
    assert run(serve(_build(local_digest)), *args) == 2


def test_unavailable_served_digest_is_an_error(serve, capsys) -> None:
    url = serve(_build("unavailable"))
    assert run(url) == 2
    assert "unavailable" in capsys.readouterr().out
    # A field that was compared and differs is still a real mismatch.
    assert run(serve(_build("unavailable", version="0.0.0")), "--expect-version", "0.0.1") == 1


@pytest.mark.parametrize(
    "build",
    [
        {"version": None, "commit": SHA, "mcp_metadata_sha256": "0" * 64},
        {"version": 3, "commit": SHA, "mcp_metadata_sha256": "0" * 64},
        {"version": "x", "commit": None, "mcp_metadata_sha256": "0" * 64},
        {"version": "x", "commit": SHA},
        None,
        [],
    ],
)
def test_null_or_non_string_build_fields_are_errors(serve, build) -> None:
    assert run(serve({"build": build})) == 2


@pytest.mark.parametrize(
    "payload",
    [{"status": "ready"}, b"<html>login</html>", b"\xff\xfe", b"[1]", b"[" * 100000],
    ids=["no-build-block", "html", "not-utf8", "json-list", "deeply-nested"],
)
def test_unreadable_bodies_are_errors(serve, payload) -> None:
    assert run(serve(payload)) == 2


@pytest.mark.parametrize(
    "reply",
    [
        b"not http at all\r\n\r\n",  # http.client.BadStatusLine
        b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{"build"',  # IncompleteRead
        b"",  # RemoteDisconnected
    ],
)
def test_transport_failures_are_errors(raw_peer, reply) -> None:
    assert run(raw_peer(reply), "--timeout", "5") == 2


def test_invalid_url_is_an_error() -> None:
    assert run("http://127.0.0.1:1/a b") == 2  # http.client.InvalidURL


def test_local_digest_failure_is_an_error(serve, local_digest, monkeypatch, capsys) -> None:
    async def boom(server=None):
        raise RuntimeError("local build failed")

    monkeypatch.setattr(served_metadata, "compute_metadata", boom)
    assert run(serve(_build(local_digest))) == 2
    assert "local build failed" in capsys.readouterr().out


def test_missing_local_module_is_an_error(serve, local_digest, monkeypatch) -> None:
    import menhir.mcp

    # An older installed menhir: the module is absent, so the import inside the script fails.
    monkeypatch.delattr(menhir.mcp, "served_metadata")
    monkeypatch.setitem(sys.modules, "menhir.mcp.served_metadata", None)
    assert run(serve(_build(local_digest))) == 2


def test_expect_version_other_than_the_local_package_is_an_error(serve, capsys) -> None:
    url = serve(_build("a" * 64, version="9.9.9"))
    assert run(url, "--expect-version", "9.9.9") == 2
    assert "matching local package" in capsys.readouterr().out


def test_snapshot_mode_option_builds_the_matching_catalog(serve, capsys) -> None:
    receive = _local_digest("receive")
    url = serve(_build(receive))
    assert run(url) == 1
    out = capsys.readouterr().out
    assert "snapshot_mode=off" in out and "--snapshot-mode" in out
    assert run(url, "--snapshot-mode", "receive") == 0
    assert "snapshot_mode=receive" in capsys.readouterr().out
    assert os.environ[SNAPSHOT_RECEIVE_MODE_ENV] == "off"  # restored after the local build


def test_script_runs_without_docstrings() -> None:
    result = subprocess.run(
        [sys.executable, "-OO", str(SCRIPT), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "--snapshot-mode" in result.stdout


@pytest.mark.parametrize("field", ["version", "commit", "mcp_metadata_sha256"])
@pytest.mark.parametrize(
    "value",
    ["1\nversion: ok forged", "\ud800", "x" * 65, "1 2"],
    ids=["newline", "lone-surrogate", "too-long", "space"],
)
def test_served_build_text_that_is_not_plain_is_refused_unshown(serve, capsys, field, value) -> None:
    """#501: served text is printed only in a plain shape; otherwise exit 2 with a fixed phrase."""
    build = _build("0" * 64)
    build["build"][field] = value
    assert run(serve(build)) == 2
    out = capsys.readouterr().out
    assert f"build.{field} is not plain text" in out
    assert "forged" not in out and "version: ok" not in out
