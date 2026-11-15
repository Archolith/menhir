"""Follow-ups from the menhir-deploy #12 and #481 reviews (#503, #504, #507).

- #503: a refusal names the control that made it. A bound production client policy decides
  alone; MENHIR_CLIENT_TOOLS applies only without one. An empty bound policy denies every tool,
  at the invocation gate and in tools/list (the policy loader rejects one today; the gates must
  not depend on that).
- #504: review_inbox does not send a production client to the explorer without saying that
  production does not serve it.
- #507: over the stdio bridge, an unscoped review_inbox does not claim "all namespaces": the pin
  exists only server-side.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult

from menhir.api import mcp_remote
from menhir.core.backend_client import BackendClient
from menhir.core.request_context import (
    bind_request_tool_allowlist,
    reset_request_tool_allowlist,
)
from menhir.mcp import contracts
from menhir.mcp.contracts import BaseJsonTool, BaseTextTool
from menhir.mcp.tools.ops.review_inbox import ReviewInboxTool

pytestmark = pytest.mark.unit


class _Tool(BaseTextTool):
    name = "recall_memories"
    description = "test double"
    required_tier = "agent"

    async def endpoint(self, text: str = "") -> str:  # noqa: D102
        return "ran"


def _run_with_policy(tools: frozenset[str]):
    token = bind_request_tool_allowlist(tools)
    try:
        return asyncio.run(_Tool().execute())
    finally:
        reset_request_tool_allowlist(token)


def _denial_text(result) -> str:
    # #503: a refusal is an MCP error result, never ordinary output.
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert len(result.content) == 1
    return result.content[0].text


def test_policy_refusal_names_the_client_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    out = _denial_text(_run_with_policy(frozenset({"add_memory"})))
    assert "PermissionError" in out
    assert "client policy" in out
    assert "MENHIR_CLIENT_TOOLS" not in out


def test_policy_allows_its_own_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # The env allowlist would refuse; a bound policy decides alone.
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset({"add_memory"}))
    assert _run_with_policy(frozenset({"recall_memories"})) == "ran"


def test_empty_bound_policy_denies_every_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    out = _denial_text(_run_with_policy(frozenset()))
    assert "PermissionError" in out and "client policy" in out


def test_env_allowlist_refusal_still_names_menhir_client_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset({"add_memory"}))
    out = _denial_text(asyncio.run(_Tool().execute()))
    assert "MENHIR_CLIENT_TOOLS" in out


def test_policy_refusal_text_is_unchanged_for_text_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    # menhir-deploy's smoke probe accepts isError or exactly this text; keep both.
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    assert _denial_text(_run_with_policy(frozenset({"add_memory"}))) == (
        "Error: PermissionError: Client is not permitted to invoke `recall_memories` "
        "(restricted by the server's client policy)"
    )


class _JsonTool(BaseJsonTool):
    name = "recall_memories"
    description = "test double"
    required_tier = "agent"

    async def endpoint(self, text: str = "") -> str:  # noqa: D102
        return "{}"


def test_json_tool_refusal_keeps_its_envelope_and_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(contracts, "get_client_tool_allowlist", lambda: frozenset())
    token = bind_request_tool_allowlist(frozenset({"add_memory"}))
    try:
        result = asyncio.run(_JsonTool().execute())
    finally:
        reset_request_tool_allowlist(token)
    payload = json.loads(_denial_text(result))
    assert payload["ok"] is False
    assert payload["error"]["message"].startswith("PermissionError: Client is not permitted")


def test_tier_refusal_is_still_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the tool-allowlist gate changed; other gate refusals keep their earlier form.
    monkeypatch.setattr(contracts, "get_request_tier", lambda: "readonly")
    out = asyncio.run(_Tool().execute())
    assert isinstance(out, str) and "cannot invoke" in out


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


async def _list_with_policy(monkeypatch: pytest.MonkeyPatch, policy: frozenset[str]) -> list[str]:
    async def fake_super_list(self):
        return [_FakeTool("recall_memories"), _FakeTool("add_memory")]

    monkeypatch.setattr(mcp_remote.FastMCP, "list_tools", fake_super_list, raising=False)
    monkeypatch.setattr(mcp_remote, "get_request_tier", lambda: "")
    monkeypatch.setattr(mcp_remote, "get_client_tool_allowlist", lambda: policy)
    mcp = mcp_remote.TierFilteredFastMCP.__new__(mcp_remote.TierFilteredFastMCP)
    token = bind_request_tool_allowlist(policy)
    try:
        return [t.name for t in await mcp_remote.TierFilteredFastMCP.list_tools(mcp)]
    finally:
        reset_request_tool_allowlist(token)


@pytest.mark.anyio
async def test_list_tools_hides_everything_under_an_empty_bound_policy(monkeypatch) -> None:
    assert await _list_with_policy(monkeypatch, frozenset()) == []
    assert await _list_with_policy(monkeypatch, frozenset({"add_memory"})) == ["add_memory"]


def _empty_backend(backend: MagicMock) -> MagicMock:
    for name in (
        "list_conflict_groups",
        "list_episode_processing",
        "list_candidates",
        "list_todos",
        "list_artifact_questions",
    ):
        setattr(backend, name, AsyncMock(return_value=[]))
    return backend


def _inbox(backend: MagicMock) -> str:
    tool = ReviewInboxTool()
    tool.get_backend = MagicMock(return_value=backend)
    return asyncio.run(tool.endpoint())


def test_bridge_header_does_not_claim_all_namespaces() -> None:
    out = _inbox(_empty_backend(MagicMock(spec=BackendClient)))
    header = out.splitlines()[0]
    assert "all namespaces" not in header
    assert header == "Review inbox (every namespace this client can read)"


def test_in_process_header_is_unchanged() -> None:
    assert _inbox(_empty_backend(MagicMock())).splitlines()[0] == "Review inbox (all namespaces)"


def test_candidates_line_points_at_the_mcp_review_tools() -> None:
    # #431: review now exists over MCP, so the inbox points there instead of the explorer.
    backend = _empty_backend(MagicMock())
    backend.list_candidates = AsyncMock(return_value=[{"uuid": "c-1", "label": "x"}])
    act = next(line for line in _inbox(backend).splitlines() if "Act with:" in line)
    assert "review_candidate" in act
    assert "list_candidates" in act
