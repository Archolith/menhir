"""MCP-over-HTTP client for the P2A transport probe.

Extracted verbatim from ``p2a_transport_probe.py``: the minimal caller that classifies who said no,
plus the two envelope-unwrapping helpers it uses.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from p2a_transport_probe_model import (
    BLOCKED_HTTP,
    BLOCKED_TRANSPORT,
    DEFAULT_USER_AGENT,
    DELIVERED_OK,
    DELIVERED_REFUSED,
    PROBE_ERROR,
)


class ProbeClient:
    """Minimal MCP-over-HTTP caller.

    Hand-rolled for the same reason the remote-sim tests are: a client library that can
    short-circuit to an in-process server would measure nothing. This one only ever speaks over a
    socket, and it surfaces the difference between an HTTP error and a transport failure instead of
    flattening both into an exception.
    """

    def __init__(
        self, base_url: str, operator_key: str, timeout: float, user_agent: str = ""
    ) -> None:
        self.endpoint = f"{base_url.rstrip('/')}/mcp-http"
        self.operator_key = operator_key
        self.timeout = timeout
        # Not cosmetic. Behind a Cloudflare zone with Browser Integrity Check on, urllib's default
        # `Python-urllib/x.y` signature is refused at the edge with a 403 (error 1010) and NOTHING
        # reaches the origin -- which reads exactly like a server outage from the client side. The
        # shipping client must send an honest product agent for the same reason.
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    def _post(self, body: dict[str, Any]) -> tuple[str, str, int | None, str]:
        """Return (outcome, payload_or_detail, http_status, raw_text)."""
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.operator_key}",
                "User-Agent": self.user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return ("http_ok", "", response.status, response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # An HTTP status means something in the path answered. If it is an edge or proxy, this
            # is the limit being measured; the body is captured because 413 vs 502 vs 520
            # distinguishes a size rule from an origin failure.
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 -- the body is diagnostic only; never fail the probe
                detail = ""
            return ("http_error", detail, exc.code, "")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ("transport_error", f"{type(exc).__name__}: {exc}", None, "")

    def call(self, tool: str, arguments: dict | None) -> dict[str, Any]:
        """Call a tool and classify the result. Never raises for an expected failure mode."""
        if arguments is None:
            body = {"jsonrpc": "2.0", "id": 1, "method": tool, "params": {}}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        wire_bytes = len(json.dumps(body).encode("utf-8"))
        started = time.perf_counter()
        kind, detail, status, raw = self._post(body)
        elapsed = time.perf_counter() - started

        if kind == "http_error":
            return {
                "outcome": BLOCKED_HTTP,
                "detail": f"HTTP {status}: {detail[:200]}",
                "http_status": status,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }
        if kind == "transport_error":
            return {
                "outcome": BLOCKED_TRANSPORT,
                "detail": detail,
                "http_status": None,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }

        try:
            parsed = _parse_mcp_body(raw)
        except ValueError as exc:
            return {
                "outcome": PROBE_ERROR,
                "detail": f"unparseable MCP body: {exc}",
                "http_status": status,
                "elapsed_s": elapsed,
                "wire_bytes": wire_bytes,
            }

        payload = _tool_payload(parsed)
        server_code = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            server_code = str((payload.get("error") or {}).get("code") or "unknown")
        outcome = DELIVERED_REFUSED if server_code else DELIVERED_OK
        return {
            "outcome": outcome,
            "detail": server_code or "accepted",
            "http_status": status,
            "elapsed_s": elapsed,
            "wire_bytes": wire_bytes,
            "payload": payload,
            "server_code": server_code,
        }


def _parse_mcp_body(body: str) -> dict:
    """Accept a plain JSON body or an SSE frame; the transport may use either."""
    text = body.strip()
    if text.startswith(("event:", "data:")):
        for line in text.splitlines():
            if line.startswith("data:"):
                text = line[len("data:") :].strip()
                break
    if not text:
        raise ValueError("empty body")
    return json.loads(text)


def _tool_payload(parsed: dict) -> Any:
    """Unwrap the tool's JSON out of the MCP envelope, tolerating either shape."""
    result = parsed.get("result")
    if not isinstance(result, dict):
        return parsed
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                return json.loads(first["text"])
            except ValueError:
                return first["text"]
    return result
