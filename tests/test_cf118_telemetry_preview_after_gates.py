"""CF-118: raw tool arguments were previewed into durable telemetry before the authorization gates.

`BaseTool.execute` computes the preview as an ARGUMENT to `track_mcp_call`:

    result = await track_mcp_call(
        payload=self.call_payload(*args, **kwargs),   # evaluated here, eagerly
        runner=_runner, ...)

while the tier gate, the client allowlist and the namespace pin all live INSIDE `_runner`. So a
call refused at a gate still persisted a row describing the identifiers that caller chose.

WHAT THIS IS AND IS NOT. The values are redacted (CF-37's half of this: free text becomes
`[redacted]` and only identifier-shaped values on a structural allowlist survive) and they are
caller-known, so this is not a disclosure. What it is, is attacker-controlled content in the
operator's durable audit trail -- one row per attempt, carrying a namespace and a node uuid the
server never acted on, indistinguishable from one it did.

The lineage half was already fixed: `_lineage_from_payload` is resolved through `effective_payload`,
which `_runner` publishes only after the gates, so a refused call's namespace falls back to the
pinned/default one. The preview simply did not obey the same rule. It does now.

Measured before the fix, driving a real tool at `readonly` against an `operator` requirement:

    preview   = {"memory_uuid": "victim-uuid-123", "namespace": "victim-tenant"}
    namespace = default        <- lineage was already correct
    node_uuid = None           <- lineage was already correct
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from menhir.core.request_context import bind_request_tier, reset_request_tier
from menhir.mcp import contracts as C
from menhir.mcp.telemetry import tracker as T

pytestmark = pytest.mark.unit


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, **kw: object) -> None:
        self.rows.append(kw)


class _Probe(C.BaseTool):
    name = "cf118_probe"
    operation = "cf118_probe"
    required_tier = "operator"
    scope = C.ToolScope.NAMESPACED
    description = "probe"

    async def endpoint(self, memory_uuid: str = "", namespace: str = "") -> str:
        return "ran"


class _Boom(_Probe):
    name = "cf118_boom"
    operation = "cf118_boom"

    async def endpoint(self, memory_uuid: str = "", namespace: str = "") -> str:
        raise RuntimeError("endpoint blew up after the gates")


def _run(tool: C.BaseTool, *, tier: str, pin: str = "", **kwargs: object) -> dict:
    """Drive one tool call and return the single persisted telemetry row."""
    store = _Store()
    real = T.track_mcp_call

    async def spy(**kw: object):
        kw["store"] = store
        return await real(**kw)

    token = bind_request_tier(tier)
    try:
        # `contracts` does `from ... import get_pinned_namespace` at module scope, so the name is
        # bound in ITS namespace -- patching `service_access` alone leaves `_apply_pinned_namespace`
        # on the real function and the pin silently does nothing. `_lineage_from_payload` imports it
        # lazily inside the call, so THAT one does need the source module patched. Both, therefore.
        with patch.object(C, "track_mcp_call", spy), patch.object(
            C, "require_trusted_client_identity", lambda *a, **k: None
        ), patch.object(C, "get_pinned_namespace", return_value=pin), patch(
            "menhir.mcp.service_access.get_pinned_namespace", return_value=pin
        ):
            asyncio.run(tool.execute(**kwargs))
    finally:
        reset_request_tier(token)

    assert len(store.rows) == 1, store.rows
    return store.rows[0]


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------


def test_a_refused_call_does_not_persist_the_callers_identifiers() -> None:
    """THE FINDING. A readonly caller naming an operator tool must not get its chosen namespace
    and uuid written into the operator's telemetry."""
    row = _run(_Probe(), tier="readonly", memory_uuid="victim-uuid-123", namespace="victim-tenant")

    preview = str(row["payload_preview"])
    assert "victim-uuid-123" not in preview
    assert "victim-tenant" not in preview
    assert preview == T.PREVIEW_UNAUTHORIZED


def test_a_refused_call_is_still_recorded_as_an_attempt() -> None:
    """The row must not vanish. A refusal is exactly the thing an operator wants in the audit
    trail -- what changes is that it no longer carries the caller's payload."""
    row = _run(_Probe(), tier="readonly", memory_uuid="u", namespace="n")

    assert row["success"] is False
    assert row["error"] == "PermissionError"
    assert row["operation"] == "cf118_probe"
    assert row["payload_preview"] == T.PREVIEW_UNAUTHORIZED


def test_the_size_of_a_refused_call_is_still_measured() -> None:
    """POSITIVE CONTROL for the fix's scope: sizing stays on the RAW payload. It measures what the
    caller sent, discloses nothing, and is the only quantitative signal that survives a refusal --
    an operator watching for someone hammering a tool with large bodies still sees it."""
    small = _run(_Probe(), tier="readonly", memory_uuid="u", namespace="n")
    large = _run(_Probe(), tier="readonly", memory_uuid="u" * 400, namespace="n")

    assert small["input_size"] is not None
    assert large["input_size"] > small["input_size"]


def test_the_marker_is_distinguishable_from_a_call_with_no_arguments() -> None:
    """Why a marker rather than NULL. A NULL preview already means "this caller sent nothing";
    reusing it would erase the difference between an empty call and a refused one."""
    assert T.PREVIEW_UNAUTHORIZED != ""
    assert T.PREVIEW_UNAUTHORIZED is not None
    assert "unauthorized" in T.PREVIEW_UNAUTHORIZED


# ---------------------------------------------------------------------------
# positive controls -- the authorized path must keep working
# ---------------------------------------------------------------------------


def test_an_authorized_call_still_previews_its_arguments() -> None:
    """POSITIVE CONTROL, the one that matters most: a fix that suppressed every preview would
    satisfy all four tests above and destroy the telemetry."""
    row = _run(_Probe(), tier="operator", memory_uuid="real-uuid", namespace="real-tenant")

    preview = str(row["payload_preview"])
    assert row["success"] is True
    assert "real-uuid" in preview
    assert "real-tenant" in preview


def test_an_authorized_call_previews_the_ENFORCED_namespace_not_the_requested_one() -> None:
    """The accuracy claim, proven rather than asserted. A pinned client's preview used to show the
    namespace it ASKED for; it now shows the one the server forced. This is the same class of
    defect as CF-239 -- a surface reporting the caller's claim as though it were the outcome."""
    row = _run(
        _Probe(), tier="operator", pin="server-pin", memory_uuid="u", namespace="caller-asked-for"
    )

    preview = str(row["payload_preview"])
    assert "server-pin" in preview
    assert "caller-asked-for" not in preview
    assert row["namespace"] == "server-pin"


def test_a_failure_AFTER_the_gates_still_previews_its_arguments() -> None:
    """POSITIVE CONTROL for the split. The marker means "never authorized", not "failed". An
    endpoint that raises has already passed every gate, so its row must stay debuggable -- losing
    the payload on genuine failures would be a worse outcome than the defect."""
    row = _run(_Boom(), tier="operator", memory_uuid="real-uuid", namespace="real-tenant")

    preview = str(row["payload_preview"])
    assert row["success"] is False
    assert preview != T.PREVIEW_UNAUTHORIZED
    assert "real-uuid" in preview


def test_free_text_is_still_redacted_on_the_authorized_path() -> None:
    """POSITIVE CONTROL for CF-37, which shares this path: routing the preview through the
    effective payload must not route it around the redactor."""
    from menhir.infrastructure.telemetry.helpers import _safe_preview_of

    rendered = _safe_preview_of(
        {"text": "SECRET prose about the user", "namespace": "ns", "flagged": True}
    )

    assert "SECRET" not in rendered
    assert "[redacted]" in rendered
    assert "ns" in rendered


def test_a_caller_with_no_gates_is_unaffected() -> None:
    """POSITIVE CONTROL: background/internal work passes no `effective_payload` at all, so its raw
    payload IS its effective one and must still be previewed."""
    store = _Store()

    async def runner() -> str:
        return "done"

    asyncio.run(
        T.track_mcp_call(
            kind="background",
            operation="cf118_background",
            payload={"namespace": "bg-ns", "node_uuid": "bg-uuid"},
            runner=runner,
            store=store,
        )
    )

    preview = str(store.rows[0]["payload_preview"])
    assert "bg-ns" in preview
    assert preview != T.PREVIEW_UNAUTHORIZED
