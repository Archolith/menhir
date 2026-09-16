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
import sys
import urllib.request

ENDPOINT = "http://127.0.0.1:8099/mcp-http"
OPERATOR_KEY = "sim-operator-key"


def main() -> int:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {OPERATOR_KEY}",
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
            return 0 if response.status == 200 else 1
    except Exception:  # noqa: BLE001 -- a healthcheck reports unhealthy for ANY failure; a
        # narrower catch here would let an unanticipated error escape as a traceback and be
        # reported as "unhealthy" anyway, only less legibly.
        return 1


if __name__ == "__main__":
    sys.exit(main())
