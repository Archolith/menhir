from __future__ import annotations

import pytest

from menhir.api import mcp_remote


def test_create_remote_mcp_registers_tools_and_resources(monkeypatch):
    created = {}
    tools_registered = []
    resources_registered = []

    class FakeFastMCP:
        def __init__(self, name: str, instructions: str) -> None:
            created["name"] = name
            created["instructions"] = instructions

        def sse_app(self, *, mount_path: str):
            return {"mount_path": mount_path}

    def fake_register_all_tools(mcp) -> None:
        tools_registered.append(mcp)

    def fake_register_memory_resources(mcp) -> None:
        resources_registered.append(mcp)

    # The remote surface is built from TierFilteredFastMCP (a FastMCP subclass
    # that scopes tools/list to the caller's auth tier), so patch that symbol.
    monkeypatch.setattr(mcp_remote, "TierFilteredFastMCP", FakeFastMCP)
    monkeypatch.setattr(mcp_remote, "register_all_tools", fake_register_all_tools)
    monkeypatch.setattr(mcp_remote, "register_memory_resources", fake_register_memory_resources)

    remote_mcp = mcp_remote.create_remote_tool_only_mcp()

    assert created["name"] == "menhir"
    assert "Long-term graph memory" in created["instructions"]
    assert tools_registered == [remote_mcp]
    assert resources_registered == [remote_mcp]


def test_create_mcp_sse_app_uses_tool_only_builder(monkeypatch):
    built = []

    class FakeRemoteMcp:
        def sse_app(self, *, mount_path: str):
            built.append(mount_path)
            return {"mount_path": mount_path}

    monkeypatch.setattr(mcp_remote, "create_remote_tool_only_mcp", lambda: FakeRemoteMcp())

    app = mcp_remote.create_mcp_sse_app()

    assert app == {"mount_path": "/mcp"}
    assert built == ["/mcp"]


# --- tools/list is scoped to the caller's auth tier -------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


async def _list_as(
    monkeypatch, tier: str, allowlist: frozenset[str] = frozenset()
) -> list[str]:
    """Run TierFilteredFastMCP.list_tools() as *tier* over a fixed catalog.

    *allowlist* mirrors MENHIR_CLIENT_TOOLS: empty = unrestricted (the default),
    otherwise the returned catalog is narrowed to those tool names.
    """
    catalog = [
        _FakeTool("recall_memories"),   # readonly
        _FakeTool("add_memory"),        # agent
        _FakeTool("delete_namespace"),  # operator
        _FakeTool("unmapped_tool"),     # not in registry -> defaults to "agent"
    ]

    monkeypatch.setattr(
        mcp_remote,
        "_required_tier_by_name",
        lambda: {
            "recall_memories": "readonly",
            "add_memory": "agent",
            "delete_namespace": "operator",
        },
    )
    monkeypatch.setattr(mcp_remote, "get_request_tier", lambda: tier)
    monkeypatch.setattr(mcp_remote, "get_client_tool_allowlist", lambda: allowlist)

    async def fake_super_list(self):
        return catalog

    monkeypatch.setattr(mcp_remote.FastMCP, "list_tools", fake_super_list, raising=False)

    mcp = mcp_remote.TierFilteredFastMCP.__new__(mcp_remote.TierFilteredFastMCP)
    return [t.name for t in await mcp_remote.TierFilteredFastMCP.list_tools(mcp)]


@pytest.mark.anyio
async def test_readonly_tier_sees_only_readonly_tools(monkeypatch):
    names = await _list_as(monkeypatch, "readonly")
    assert names == ["recall_memories"]


@pytest.mark.anyio
async def test_agent_tier_hides_operator_tools(monkeypatch):
    names = await _list_as(monkeypatch, "agent")
    assert "add_memory" in names
    assert "recall_memories" in names
    assert "delete_namespace" not in names
    # unmapped tools default to "agent" (same default as the invocation gate)
    assert "unmapped_tool" in names


@pytest.mark.anyio
async def test_operator_tier_sees_everything(monkeypatch):
    names = await _list_as(monkeypatch, "operator")
    assert "delete_namespace" in names
    assert len(names) == 4


@pytest.mark.anyio
async def test_empty_tier_is_not_filtered(monkeypatch):
    """Empty tier = local stdio trust / no-auth loopback (CT-002).

    The invocation gate skips filtering there, so the catalog must too — otherwise
    the local stdio surface would silently lose its operator tools.
    """
    names = await _list_as(monkeypatch, "")
    assert len(names) == 4


# --- tools/list is also scoped to the per-client allowlist ------------------


@pytest.mark.anyio
async def test_empty_allowlist_is_unrestricted(monkeypatch):
    """No configured allowlist -> the full (tier-filtered) catalog, unchanged."""
    names = await _list_as(monkeypatch, "operator", frozenset())
    assert len(names) == 4


@pytest.mark.anyio
async def test_allowlist_narrows_catalog_within_tier(monkeypatch):
    """An allowlisted client (e.g. Chip) sees only its named tools."""
    names = await _list_as(
        monkeypatch, "operator", frozenset({"recall_memories", "add_memory"})
    )
    assert names == ["recall_memories", "add_memory"]


@pytest.mark.anyio
async def test_allowlist_composes_with_tier_gate(monkeypatch):
    """A tool must survive BOTH gates: allowlisting an operator tool does not
    lift the tier gate for an agent-tier caller."""
    names = await _list_as(
        monkeypatch, "agent", frozenset({"add_memory", "delete_namespace"})
    )
    assert names == ["add_memory"]  # delete_namespace still hidden by tier


@pytest.mark.anyio
async def test_allowlist_applies_regardless_of_tier(monkeypatch):
    """The allowlist is checked before the empty-tier early return, so a bound
    client restriction narrows the catalog even on the untiered local surface."""
    names = await _list_as(
        monkeypatch, "", frozenset({"recall_memories"})
    )
    assert names == ["recall_memories"]
