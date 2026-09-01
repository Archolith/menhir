"""Strict Streamable-HTTP MCP acceptance probe used by candidate-accept.sh."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


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


def _is_explicit_mutation_refusal(status: int, response: dict) -> bool:
    if status == 503 and response.get("error") == "temporarily_unavailable":
        return True
    if status // 100 != 2:
        return False
    if response.get("error") or response.get("result", {}).get("isError") is True:
        return True
    # FastMCP currently serializes a tool-raised PermissionError as text while
    # leaving isError false. Accept only the exact, explicit tier denial for
    # this probe's add_memory call; ordinary successful content cannot pass.
    content = response.get("result", {}).get("content", [])
    return any(
        isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and item["text"].startswith("Error: PermissionError:")
        and "cannot invoke `add_memory`" in item["text"]
        and "requires 'agent'" in item["text"]
        for item in content
    )


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: mcp_acceptance_probe.py <base-url> <bearer-token>", file=sys.stderr)
        return 2
    endpoint = sys.argv[1].rstrip("/") + "/mcp-http"
    token = sys.argv[2]
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
    if not _is_explicit_mutation_refusal(status, response):
        raise RuntimeError(
            f"candidate mutation was not explicitly refused: HTTP {status}: {response}")
    print("MCP recall succeeded and mutation was refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
