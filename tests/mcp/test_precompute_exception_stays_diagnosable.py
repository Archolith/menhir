"""Regression: a bug in call_payload()/timeout_for() must not escape execute() undiagnosed.

`BaseTool.execute()` and `BaseJsonResource.execute()` used to evaluate
`self.call_payload(...)` and `self.timeout_for(...)` as call ARGUMENTS to
`track_mcp_call(...)` -- outside its own try/except, because Python evaluates
call arguments before entering the callee. A bug in either one therefore skipped
`_diagnose_failure`, telemetry recording, and the tool/resource's own `error_mapper`
entirely, and reached the bare FastMCP/MCP-SDK fallback undiagnosed -- exactly the
"generic JSON-RPC -32603 Internal error, no way to diagnose the actual issue" failure
mode the caller was hitting.

This is not theoretical: several real `timeout_for` overrides (`force_reenrich`,
`get_enrichment_status`, `watch_enrichment`) do `int(timeout_s)` on a caller-supplied
float, which raises `OverflowError`/`ValueError` on `inf`/`nan` input -- a value a
client can send today, since the JSON Schema type is just "number".

`_safe_precompute` closes the gap: both computations are now defensive, logged with
a full traceback (so the real fault stays visible in server.log), and fall back to a
safe default so the call proceeds into the normal, protected `track_mcp_call` path
instead of raising raw past it. `timeout_for` is evaluated once, so a fixed default lets
the endpoint run normally (see the timeout_for tests below). `call_payload` is
deliberately evaluated a second time inside the protected runner (CF-118: the outer
call previews an unauthorized attempt, the inner one previews the authorized/effective
payload), so a call_payload that is *permanently* broken still fails the call on that
second attempt -- but now as a diagnosed `_diagnose_failure`/`error_mapper` message
instead of a raw, undiagnosed exception (see the call_payload tests below).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from menhir.core.request_context import bind_request_tier, reset_request_tier
from menhir.mcp import contracts as C

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# BaseTool: timeout_for() and call_payload() raising must not abort execute()
# ---------------------------------------------------------------------------


class _TimeoutForBoom(C.BaseTool):
    name = "precompute_boom_timeout"
    operation = "precompute_boom_timeout"
    required_tier = "agent"
    scope = C.ToolScope.GLOBAL
    description = "probe"

    def timeout_for(self, timeout_s: float = 300.0, **_unused: Any) -> int:
        # Mirrors the real overrides that do int(timeout_s) on caller-supplied input.
        return max(60, int(timeout_s) + 10)

    async def endpoint(self, timeout_s: float = 300.0) -> str:
        return "ran despite a broken timeout_for"


class _CallPayloadBoom(C.BaseTool):
    name = "precompute_boom_payload"
    operation = "precompute_boom_payload"
    required_tier = "agent"
    scope = C.ToolScope.GLOBAL
    description = "probe"

    def call_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("call_payload is broken")

    async def endpoint(self) -> str:
        return "ran despite a broken call_payload"


def _drive_tool(tool: C.BaseTool, **kwargs: Any) -> str:
    token = bind_request_tier("agent")
    try:
        with patch.object(C, "require_trusted_client_identity", lambda *a, **k: None), patch.object(
            C, "get_pinned_namespace", return_value=""
        ):
            return asyncio.run(tool.execute(**kwargs))
    finally:
        reset_request_tier(token)


def test_a_timeout_for_that_raises_on_inf_does_not_abort_the_call() -> None:
    """The concrete, client-reachable case: timeout_s=inf blows up int(timeout_s)."""
    result = _drive_tool(_TimeoutForBoom(), timeout_s=float("inf"))
    assert result == "ran despite a broken timeout_for"


def test_a_call_payload_that_raises_produces_a_diagnosed_error_not_a_raw_escape() -> None:
    """call_payload is evaluated twice by design (CF-118: an unauthorized-refusal preview and
    an authorized effective-payload preview, the latter published from inside _runner()). A
    permanently broken call_payload therefore still fails the call on its second, INNER
    evaluation -- but that one is inside track_mcp_call's try/except, so the failure comes
    back as a diagnosed `_diagnose_failure` message, not a raw exception that skipped
    telemetry, the error_mapper, and diagnosis entirely (the old, OUTER, unprotected call)."""
    result = _drive_tool(_CallPayloadBoom())
    assert result == "Error: RuntimeError: call_payload is broken"


# ---------------------------------------------------------------------------
# BaseJsonResource: call_payload() raising must not abort execute()
# ---------------------------------------------------------------------------


class _ResourceCallPayloadBoom(C.BaseJsonResource):
    uri = "memory://test/precompute-boom"
    name = "precompute-boom"
    description = "probe"

    def call_payload(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("call_payload is broken")

    async def endpoint(self) -> dict[str, Any]:
        return await self.build_payload()

    async def build_payload(self) -> dict[str, Any]:
        return {"ok": True, "value": 7}


def test_resource_call_payload_that_raises_produces_a_diagnosed_error_not_a_raw_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the tool case: call_payload is evaluated twice (once by execute()'s now-defended
    preview, once inside _run() for telemetry), so a permanently broken call_payload still fails
    the read -- but as a diagnosed JSON error envelope via the resource's own error_mapper, not
    as a raw exception escaping to FastMCP/MCP-SDK's generic fallback."""
    monkeypatch.setattr(C, "request_uses_query_auth", lambda: False)
    monkeypatch.setattr(C, "get_request_tier", lambda: "readonly")

    result = asyncio.run(_ResourceCallPayloadBoom().execute())
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "call_payload is broken" in payload["error"]["message"]
