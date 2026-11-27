"""Container healthcheck for the remote-sim stack: can MCP answer?

A file rather than an inline `python -c` in the compose healthcheck. The inline form needs three
levels of nested quoting -- YAML, shell, Python -- and the first attempt produced a compose file
that would not parse at all. A script has one level and can be read.

Deliberately NOT `/livez`: the health routes belong to the production route surface and exist only
when `MENHIR_STARTUP_SCOPE=production`, which this stack is not, so probing them reports a
perfectly healthy server as unhealthy forever. Asking MCP for its tool list is also the better
question -- it proves the transport, the auth and the tool registry in one request, which is
exactly what a test against this stack is about to rely on.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

DEFAULT_PORT = "8099"
DEFAULT_OPERATOR_KEY = "sim-operator-key"


def _reply(raw: str) -> object:
    """The JSON-RPC reply, from a plain JSON body or the first SSE `data:` frame."""
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise ValueError("no JSON or SSE data frame in the reply")


def _unhealthy(reason: str) -> int:
    print(f"unhealthy: {reason}", file=sys.stderr)
    return 1


def main() -> int:
    port = os.environ.get("MENHIR_API_PORT") or DEFAULT_PORT
    operator_key = os.environ.get("MENHIR_OPERATOR_KEY") or DEFAULT_OPERATOR_KEY
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp-http",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {operator_key}",
            # urllib's default `Python-urllib/x.y` is refused at the edge by a Cloudflare zone
            # with Browser Integrity Check on (403, error 1010), before the origin is reached.
            # Irrelevant while this stack is loopback-only -- which is exactly why it would be
            # missed the first time someone put it behind a hostname, and the symptom would be a
            # container that reports unhealthy forever against a perfectly healthy server.
            "User-Agent": "menhir-remote-sim-healthcheck",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 200:
                return _unhealthy(f"HTTP {response.status}")
            reply = _reply(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001 -- a healthcheck reports unhealthy for ANY failure; a
        # narrower catch here would let an unanticipated error escape as a traceback and be
        # reported as "unhealthy" anyway, only less legibly.
        return _unhealthy(f"{type(exc).__name__}: {exc}")
    if not isinstance(reply, dict) or "result" not in reply or "error" in reply:
        return _unhealthy(f"unexpected MCP reply: {str(reply)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
