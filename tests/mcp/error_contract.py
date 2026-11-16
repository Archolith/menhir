"""Assertions for the one MCP tool error shape (#669).

Every tool failure is a ``CallToolResult`` with ``isError`` set, ``structuredContent`` of
``{"ok": False, "tool", "error": {"code", "message", "hint"?}}``, and one text content item.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult


def text_of(result: Any) -> str:
    """The text a client reads, for a success string or an error result alike."""
    if isinstance(result, CallToolResult):
        assert len(result.content) == 1, result.content
        return result.content[0].text
    assert isinstance(result, str), type(result)
    return result


def error_of(result: Any, *, code: str | None = None, tool: str | None = None) -> dict[str, Any]:
    """Assert *result* is a tool error in the #669 shape and return its ``error`` object."""
    assert isinstance(result, CallToolResult), f"not an error result: {result!r}"
    assert result.isError is True
    envelope = result.structuredContent
    assert isinstance(envelope, dict) and envelope.get("ok") is False, envelope
    error = envelope["error"]
    assert isinstance(error.get("code"), str) and error["code"], error
    assert isinstance(error.get("message"), str) and error["message"], error
    assert set(error) <= {"code", "message", "hint"}, error
    if code is not None:
        assert error["code"] == code, error
    if tool is not None:
        assert envelope["tool"] == tool, envelope
    text = text_of(result)
    if text.lstrip().startswith("{"):
        assert json.loads(text) == envelope
    else:
        assert error["message"] in text
    return error
