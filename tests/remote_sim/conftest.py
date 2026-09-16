"""Lifecycle for the local "remote Menhir" stack: bring it up, hand over a URL, tear it down.

Reusable on purpose. The expensive part of testing against a genuinely remote server is standing
one up, so that lives here once and every future test against the stack -- P2B's commit path, P3's
extraction, the `menhir sync` product flow -- asks for the `remote_menhir` fixture and gets a base
URL.

Three rules this follows, learned from the online lane next door:

**Never run by default.** It needs Docker and a build, so it sits behind `--run-remote-sim` and a
`remote_sim` marker rather than `online`. CI runs the online lane against a service container and
cannot build images, so folding these in would turn a green lane red for a reason unrelated to the
change under test.

**Skip with instructions, never fail, when the environment is missing.** A developer without
Docker gets a sentence telling them what to install, not a red suite.

**Always tear down.** The stack is torn down in a `finally`, including when the bring-up itself
fails, because a half-started stack holding ports 7476/7689/8099 is worse than no stack: the next
run fails for a reason that has nothing to do with the code.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.remote-sim.yml"
BASE_URL = "http://127.0.0.1:8099"
OPERATOR_KEY = "sim-operator-key"

#: Generous: the first run builds an image and starts Neo4j, which is minutes on a cold cache.
_READY_TIMEOUT_S = 420.0
_POLL_INTERVAL_S = 3.0

#: Readiness asks for the tool list rather than calling a tool: it needs no state and proves
#: transport, auth and registry in one request.
_READINESS_TOOL_LIST = "tools/list"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-remote-sim",
        action="store_true",
        default=False,
        help="Run tests that build and start the local remote-Menhir Docker stack.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-remote-sim"):
        return
    skip = pytest.mark.skip(
        reason="remote-sim tests are disabled by default; pass --run-remote-sim (needs Docker)"
    )
    for item in items:
        if "remote_sim" in item.keywords:
            item.add_marker(skip)


def _compose(*args: str, check: bool = True, timeout: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _wait_until_live(deadline: float) -> None:
    """Poll the MCP endpoint until it answers, or explain what it was still waiting for.

    Deliberately NOT `/livez`: the health routes belong to the production route surface and are
    served only when `MENHIR_STARTUP_SCOPE=production`, which this stack is not -- so polling them
    waits out the full timeout against a server that has been answering all along. Asking the MCP
    endpoint for its tool list is also the readiness question that matters here, since it proves
    the transport, the auth and the registry the tests are about to use.
    """
    last_error = "no attempt made"
    while time.time() < deadline:
        try:
            result = call_mcp(BASE_URL, _READINESS_TOOL_LIST, None)
            if "result" in result:
                return
            last_error = f"unexpected MCP reply: {str(result)[:120]}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"menhir did not answer MCP at {BASE_URL}: {last_error}")


@pytest.fixture(scope="session")
def remote_menhir() -> str:
    """Bring the stack up for the session and return its base URL.

    Session-scoped because the build dominates the cost; the tests using it are read-mostly and
    do not need isolation from each other beyond what the server already enforces.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH; the remote-sim stack cannot be started")
    if not COMPOSE_FILE.is_file():
        pytest.skip(f"missing {COMPOSE_FILE.name}")

    try:
        _compose("up", "-d", "--build", timeout=900.0)
    except subprocess.CalledProcessError as exc:
        _compose("down", "-v", check=False, timeout=180.0)
        pytest.skip(f"could not start the remote-sim stack: {(exc.stderr or '').strip()[-500:]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        _compose("down", "-v", check=False, timeout=180.0)
        pytest.skip(f"could not start the remote-sim stack: {exc}")

    try:
        _wait_until_live(time.time() + _READY_TIMEOUT_S)
        yield BASE_URL
    finally:
        # In a finally, not after the yield: a bring-up that half-succeeded still holds the
        # ports, and the next run would then fail for a reason unrelated to its own change.
        _compose("down", "-v", check=False, timeout=300.0)


def call_mcp(base_url: str, tool: str, arguments: dict | None) -> dict:
    """Invoke one MCP tool over HTTP as an operator, returning the parsed JSON result.

    Deliberately hand-rolled rather than driven through an MCP client library: the point is to
    cross a real socket into a process that shares nothing with this one, and a client that
    short-circuits to an in-process server would quietly test nothing.
    """
    if arguments is None:
        body = {"jsonrpc": "2.0", "id": 1, "method": tool, "params": {}}
    else:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    request = urllib.request.Request(
        f"{base_url}/mcp-http",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {OPERATOR_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
    return _parse_mcp_body(body)


def _parse_mcp_body(body: str) -> dict:
    """Accept either a plain JSON body or an SSE frame, since the transport may use either."""
    text = body.strip()
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:"):].strip()
                break
    return json.loads(text)
