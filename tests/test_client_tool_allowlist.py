"""Server-side per-client tool allowlist (MENHIR_CLIENT_TOOLS).

An allowlisted client sees ONLY its named tools in tools/list and is refused if
it tries to invoke any other tool. This is the tool-surface analogue of the
namespace pin: a small-model client (e.g. a public game-chat bot) is handed a
tiny, purpose-built toolset instead of the full catalog it cannot navigate.
Unlisted clients must be completely unaffected.

tools/list visibility is covered in ``test_mcp_remote.py``; this file covers the
parser and the call-time invocation gate (defense in depth).
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.config.settings import parse_client_tools
from menhir.mcp import contracts
from menhir.mcp.contracts import BaseTextTool


@pytest.mark.unit
def test_parse_client_tools_pairs_and_junk() -> None:
    assert parse_client_tools("tiny-agent-rp=recall_memories|add_memory") == {
        "tiny-agent-rp": frozenset({"recall_memories", "add_memory"})
    }
    # Multiple entries, whitespace, case-folded client names, '|'-split tools.
    assert parse_client_tools(" A=x|y , B=z ") == {
        "a": frozenset({"x", "y"}),
        "b": frozenset({"z"}),
    }
    # Malformed entries skipped: no '=', empty client, empty tool list all drop.
    assert parse_client_tools("noequals,=notools,key=,ok=fine") == {
        "ok": frozenset({"fine"})
    }
    # A repeated client name accumulates rather than clobbering.
    assert parse_client_tools("a=x,a=y") == {"a": frozenset({"x", "y"})}
    assert parse_client_tools("") == {}


class _Tool(BaseTextTool):
    name = "recall_memories"
    description = "test double"
    required_tier = "agent"

    async def endpoint(self, text: str = "") -> str:  # noqa: D102
        return "ran"


@pytest.mark.unit
def test_unrestricted_client_can_call_any_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty allowlist = no restriction (the default for every client).
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    assert asyncio.run(_Tool().execute()) == "ran"


@pytest.mark.unit
def test_allowlisted_tool_is_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        contracts, "get_client_tool_allowlist", lambda: frozenset({"recall_memories"})
    )
    assert asyncio.run(_Tool().execute()) == "ran"


@pytest.mark.unit
def test_tool_outside_allowlist_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client is allowed add_memory only; recall_memories must be refused even
    # though it exists and the tier would otherwise permit it.
    monkeypatch.setattr(
        contracts, "get_client_tool_allowlist", lambda: frozenset({"add_memory"})
    )
    output = asyncio.run(_Tool().execute())
    assert "PermissionError" in output
    assert "allowlist" in output
