"""#669 over the stdio server's runtime: standalone fastmcp behind the archolith gateway.

The unit tests call ``execute`` and see a CallToolResult with isError set. Over the gateway,
fastmcp serialized that object into the text and sent isError=false, so a client saw a success
whose text was a JSON dump. These go through a real gateway, pinned and via ``call_tool``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from archolith_mcp_framework import create_gateway_server
from fastmcp import Client

from menhir.mcp.tools.ops.add_todo import AddTodoTool
from menhir.mcp.tools.ops.list_todos import ListTodosTool

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Todo(AddTodoTool):
    def get_backend(self):  # never reached: the priority check refuses first
        raise AssertionError("backend touched")


class _List(ListTodosTool):
    def get_backend(self):
        backend = MagicMock()

        async def list_todos(**_kwargs):
            return []

        backend.list_todos = list_todos
        return backend


def _server():
    server = create_gateway_server("t", always_visible=["add_todo"])
    _Todo().register(server)
    _List().register(server)
    return server


async def _call(client: Client, name: str, args: dict, *, proxied: bool):
    if proxied:
        return await client.call_tool(
            "call_tool", {"name": name, "arguments": args}, raise_on_error=False
        )
    return await client.call_tool(name, args, raise_on_error=False)


@pytest.mark.parametrize("proxied", [False, True], ids=["pinned", "call_tool"])
async def test_a_refusal_arrives_with_is_error_and_the_envelope(proxied) -> None:
    async with Client(_server()) as client:
        result = await _call(client, "add_todo", {"text": "t", "priority": "urgent"}, proxied=proxied)
    assert result.is_error is True
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["code"] == "invalid_argument"
    text = result.content[0].text
    assert text.startswith("Cannot add todo: invalid priority 'urgent'")
    assert '"_meta"' not in text  # not a serialized CallToolResult


async def test_a_success_is_still_plain_text_without_is_error() -> None:
    async with Client(_server()) as client:
        result = await _call(client, "list_todos", {}, proxied=True)
    assert result.is_error is False
    assert result.content[0].text.startswith("open (0)")
