"""CF-238: the two-step bootstrap handshake could never complete for a namespace-pinned client.

The flow is: record a receipt on `read_flagged_memories` / `GET /bootstrap/flagged`, then present
it on `recall_context_memories` / `POST /bootstrap/context`. Both halves of the comparison
diverged between the two sides, on BOTH transports:

  * the RECORD sides keyed the receipt on the raw `workspace` and computed the version WITH the
    namespace;
  * the CHECK sides folded the namespace into the workspace (`body.workspace or
    resolved_namespace`, `(workspace or namespace or "")`) and computed the version with NO
    namespace argument at all.

For a pinned client that omits `workspace` -- the documented shape of the flow -- the two sides
therefore looked up different slots holding different version strings. No retry, re-bootstrap or
ordering change could produce a hit, because the mismatch is by construction.

WHY THE SUITE DID NOT CATCH IT. The existing route tests pass `workspace="alpha"` on both calls,
so the folding is a no-op for them, AND their fake `fetch_flagged_memory_bootstrap_version` is an
`AsyncMock` with a constant return value -- so the version compares equal no matter which
arguments the two sides pass. A namespace-blind version mock hides exactly this defect. Every
version stub below is therefore a FUNCTION OF ITS ARGUMENTS; that is the load-bearing part of
these fixtures, not an incidental detail.

The receipt key now carries the namespace as its own component rather than relying on the version
string to carry tenancy. Both failure directions are tested below: the false refusal that this
finding is about, and the false acceptance that putting the namespace in the version was meant to
prevent.

TWO TENANTS, NOT ONE -- the distinction the first attempt at this fix got wrong. `/bootstrap/context`
resolves `body.namespace` to scope the CONTENT it reads. The RECEIPT's tenant is a different value:
`/bootstrap/flagged` accepts no namespace argument at all, so it can only ever resolve one from the
server-side pin or the header. Keying the check on `body.namespace` asks about a tenant the record
side could not have written, which broke two pre-existing route tests that pass `namespace` on the
context call only. The receipt tenant is therefore `_resolve_namespace(request, None)` on both
sides, and `body.namespace` continues to scope the content read alone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.core.runtime_support import (
    _bootstrap_receipt_key,
    _has_recent_flagged_bootstrap_read,
    _remember_flagged_bootstrap_read,
)

pytestmark = pytest.mark.unit


def _version(workspace: str | None = None, *, namespace: str | None = None) -> str:
    """A version that actually depends on the tenant, as the real implementation does.

    `memory_queries.fetch_flagged_memory_bootstrap_version` fingerprints the flagged set, and the
    flagged set differs per namespace. A constant stub cannot distinguish a fixed handshake from
    a broken one.
    """
    return f"v|ws={workspace or ''}|ns={namespace or ''}"


@pytest.fixture(autouse=True)
def _clean_receipts():
    from menhir.core.runtime_support import _state

    with _state._flagged_lock:
        _state.flagged_bootstrap_reads.clear()
    yield
    with _state._flagged_lock:
        _state.flagged_bootstrap_reads.clear()


# ---------------------------------------------------------------------------
# the receipt key itself
# ---------------------------------------------------------------------------


def test_the_receipt_key_separates_tenants() -> None:
    """THE structural fix. Two clients sharing a reader id and workspace but pinned to different
    silos must not share one receipt slot -- with a shared slot the second client's record
    overwrites the first's, and the first is told to bootstrap again forever."""
    a = _bootstrap_receipt_key("reader-1", None, "tenant-a")
    b = _bootstrap_receipt_key("reader-1", None, "tenant-b")

    assert a != b


def test_two_pinned_clients_do_not_evict_each_other() -> None:
    """The behavioural form of the test above, through the public helpers."""
    _remember_flagged_bootstrap_read("reader-1", "va", workspace=None, namespace="tenant-a")
    _remember_flagged_bootstrap_read("reader-1", "vb", workspace=None, namespace="tenant-b")

    assert _has_recent_flagged_bootstrap_read("reader-1", "va", workspace=None, namespace="tenant-a")
    assert _has_recent_flagged_bootstrap_read("reader-1", "vb", workspace=None, namespace="tenant-b")


def test_a_receipt_for_one_tenant_does_not_satisfy_another() -> None:
    """POSITIVE CONTROL against over-correcting: separating the slots must not make every
    lookup succeed. A tenant that never bootstrapped is still refused."""
    _remember_flagged_bootstrap_read("reader-1", "va", workspace=None, namespace="tenant-a")

    assert not _has_recent_flagged_bootstrap_read(
        "reader-1", "va", workspace=None, namespace="tenant-b"
    )


def test_the_workspace_component_still_separates_slots() -> None:
    """POSITIVE CONTROL: the pre-existing workspace scoping must survive the change. This is what
    `tests/test_thread_safety.py` pins, exercised here against the namespace-aware key."""
    _remember_flagged_bootstrap_read("reader-1", "a-v1", workspace="archolith")
    _remember_flagged_bootstrap_read("reader-1", "y-v1", workspace="yawn")

    assert _has_recent_flagged_bootstrap_read("reader-1", "a-v1", workspace="archolith")
    assert not _has_recent_flagged_bootstrap_read("reader-1", "y-v1", workspace="archolith")


def test_omitting_the_namespace_is_still_a_valid_slot() -> None:
    """POSITIVE CONTROL: the unpinned caller, which is every existing caller, is unaffected."""
    _remember_flagged_bootstrap_read("reader-1", "v1")

    assert _has_recent_flagged_bootstrap_read("reader-1", "v1")
    assert not _has_recent_flagged_bootstrap_read("reader-1", "v2")


# ---------------------------------------------------------------------------
# REST: GET /bootstrap/flagged -> POST /bootstrap/context
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned_to():
    """Bind the request to a server-side namespace pin, the way MENHIR_CLIENT_NAMESPACES does.

    This is what "pinned client" means and it is why the defect was invisible: the pin is
    resolved server-side on BOTH routes, so neither the header nor the request body has to carry
    it. Simulating a pin with a header on one call and a body field on the other tests a
    different (and genuinely unsupported) thing -- `/bootstrap/flagged` takes no namespace
    argument at all, so a body namespace on the context call alone can never be matched.
    """
    from contextlib import contextmanager

    from menhir.api import routes_support

    @contextmanager
    def _pin(namespace: str | None):
        with patch.object(routes_support, "get_pinned_namespace", return_value=namespace or ""):
            yield

    return _pin


@pytest.fixture
def rest_client():
    backend = SimpleNamespace()
    backend.fetch_flagged_memories = AsyncMock(
        return_value=[{"uuid": "pin-1", "content": "pin", "bootstrap_scope": "general"}]
    )
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(side_effect=_version)
    backend.fetch_recent_memories = AsyncMock(
        return_value=[{"uuid": "recent-1", "content": "recent", "user_flagged": False}]
    )
    backend.recall = AsyncMock(return_value={"results": []})

    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = SimpleNamespace(
        capabilities=SimpleNamespace(reads_ready=True), failures=[]
    )
    with patch.object(api_routes, "_get_backend", return_value=backend):
        yield TestClient(app), backend


def test_rest_pinned_client_omitting_workspace_can_complete_the_handshake(
    rest_client, pinned_to
) -> None:
    """THE FINDING, on the REST transport. A client pinned to `tenant-a` bootstraps and then asks
    for context, naming its namespace and no workspace. Before the fix this returned 409 forever:
    the record side stored under (reader, general, tenant-a) with a tenant-a version, and the
    check side looked up (reader, workspace:tenant-a, -) with a namespace-less version."""
    client, _backend = rest_client

    with pinned_to("tenant-a"):
        flagged = client.get("/api/bootstrap/flagged", params={"reader_id": "pinned-reader"})
        context = client.post(
            "/api/bootstrap/context", json={"reader_id": "pinned-reader"}
        )

    assert flagged.status_code == 200
    assert context.status_code == 200, context.json()
    assert context.json()["bootstrap_verified"] is True


def test_rest_both_routes_agree_on_the_version_they_computed(rest_client, pinned_to) -> None:
    """The version is echoed by both routes. They must report the same string for one handshake --
    if they do not, the receipt comparison is being made against two different values and any
    success is accidental."""
    client, _backend = rest_client

    with pinned_to("tenant-a"):
        flagged = client.get("/api/bootstrap/flagged", params={"reader_id": "pinned-reader"})
        context = client.post("/api/bootstrap/context", json={"reader_id": "pinned-reader"})

    assert flagged.json()["flagged_version"] == context.json()["flagged_version"]
    assert "ns=tenant-a" in flagged.json()["flagged_version"]


def test_rest_both_routes_agree_on_the_bootstrap_selection(rest_client, pinned_to) -> None:
    """Same handshake, same reported scope. The check side used to derive its selection from
    `body.workspace or resolved_namespace`, so a pinned client saw `workspace:tenant-a` from one
    route and `general` from the other."""
    client, _backend = rest_client

    with pinned_to("tenant-a"):
        flagged = client.get("/api/bootstrap/flagged", params={"reader_id": "pinned-reader"})
        context = client.post("/api/bootstrap/context", json={"reader_id": "pinned-reader"})

    assert flagged.json()["bootstrap_selection"] == context.json()["bootstrap_selection"]


def test_rest_context_without_any_receipt_is_still_refused(rest_client) -> None:
    """POSITIVE CONTROL, and the one that matters most: the gate must still close. A fix that
    made the key comparison always succeed would satisfy every test above."""
    client, _backend = rest_client

    context = client.post(
        "/api/bootstrap/context",
        json={"reader_id": "never-bootstrapped", "namespace": "tenant-a"},
    )

    assert context.status_code == 409


def test_rest_a_receipt_from_another_tenant_does_not_open_the_gate(
    rest_client, pinned_to
) -> None:
    """POSITIVE CONTROL for the tenancy half. Bootstrapping as tenant-a must not admit a context
    read for tenant-b under the same reader id."""
    client, _backend = rest_client

    with pinned_to("tenant-a"):
        client.get("/api/bootstrap/flagged", params={"reader_id": "crossing-reader"})
    with pinned_to("tenant-b"):
        context = client.post("/api/bootstrap/context", json={"reader_id": "crossing-reader"})

    assert context.status_code == 409


def test_rest_the_unpinned_workspace_flow_is_unchanged(rest_client) -> None:
    """POSITIVE CONTROL: the flow the existing suite covers -- explicit workspace on both calls,
    no namespace -- must behave exactly as before."""
    client, _backend = rest_client

    flagged = client.get(
        "/api/bootstrap/flagged", params={"reader_id": "ws-reader", "workspace": "alpha"}
    )
    context = client.post(
        "/api/bootstrap/context", json={"reader_id": "ws-reader", "workspace": "alpha"}
    )

    assert flagged.status_code == 200
    assert flagged.json()["bootstrap_selection"] == "workspace:alpha"
    assert context.status_code == 200
    assert context.json()["bootstrap_verified"] is True


# ---------------------------------------------------------------------------
# MCP: read_flagged_memories -> recall_context_memories
# ---------------------------------------------------------------------------
#
# The same defect, the same shape, on the other transport. It is tested separately rather than
# assumed to follow from the REST fix: the two pairs are four independent call sites, and three
# of the four had to change.


def _mcp_backend():
    backend = SimpleNamespace()
    backend.fetch_flagged_memories = AsyncMock(return_value=[])
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(side_effect=_version)
    backend.fetch_recent_memories = AsyncMock(return_value=[])
    backend.recall = AsyncMock(return_value={"results": []})
    backend.fetch_memory_by_uuid = AsyncMock(return_value=None)
    return backend


async def _run_mcp_handshake(*, record_ns: str, check_ns: str) -> dict[str, object]:
    import json

    from menhir.mcp.tools.recall.read_flagged_memories import ReadFlaggedMemoriesTool
    from menhir.mcp.tools.recall.recall_context_memories import RecallContextMemoriesTool

    backend = _mcp_backend()

    record = ReadFlaggedMemoriesTool()
    check = RecallContextMemoriesTool()
    with patch.object(type(record), "get_backend", return_value=backend), patch.object(
        type(check), "get_backend", return_value=backend
    ):
        await record.endpoint(reader_id="mcp-reader", namespace=record_ns)
        raw = await check.endpoint(reader_id="mcp-reader", namespace=check_ns)
    return json.loads(raw)


@pytest.mark.asyncio
async def test_mcp_pinned_client_can_complete_the_handshake() -> None:
    """THE FINDING, on the MCP transport. `recall_context_memories` folded `namespace` into the
    workspace and computed a namespace-less version, so a client passing only `namespace` was
    told to call `read_flagged_memories` it had just called."""
    payload = await _run_mcp_handshake(record_ns="tenant-a", check_ns="tenant-a")

    assert payload.get("ok") is not False, payload


@pytest.mark.asyncio
async def test_mcp_a_receipt_from_another_tenant_does_not_open_the_gate() -> None:
    """POSITIVE CONTROL: the gate still closes across tenants."""
    payload = await _run_mcp_handshake(record_ns="tenant-a", check_ns="tenant-b")

    assert payload.get("ok") is False
    assert "bootstrap" in str(payload.get("error", {}).get("message", "")).lower()


@pytest.mark.asyncio
async def test_mcp_context_without_any_receipt_is_refused() -> None:
    """POSITIVE CONTROL: no receipt at all is still a refusal."""
    import json

    from menhir.mcp.tools.recall.recall_context_memories import RecallContextMemoriesTool

    check = RecallContextMemoriesTool()
    with patch.object(type(check), "get_backend", return_value=_mcp_backend()):
        payload = json.loads(await check.endpoint(reader_id="fresh-mcp-reader", namespace="tenant-a"))

    assert payload.get("ok") is False
