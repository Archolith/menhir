"""Tests for the ChatGPT-facing tool metadata contract.

Covers validate_tool_metadata startup refusal on every omission/mismatch, and
inspects the actual FastMCP tool descriptors after registration for title,
description, inputSchema, annotations, and securitySchemes _meta.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastmcp import Client, FastMCP

from menhir.mcp.contracts import BaseTool, validate_tool_metadata
from menhir.mcp.tools import ALL_TOOLS, register_all_tools


def _make_tool_cls(**overrides: Any) -> type[BaseTool]:
    fields: dict[str, Any] = {
        "name": "fake_tool",
        "title": "Fake Tool",
        "description": "A fake tool for contract tests.",
        "scope": "namespaced",
        "required_tier": "agent",
        "oauth_scopes": ("menhir:write",),
        "read_only_hint": False,
        "destructive_hint": False,
        "open_world_hint": True,
    }
    fields.update(overrides)

    async def endpoint(self: BaseTool, namespace: str = "default") -> str:
        """Run the fake tool."""
        return "{}"

    return type("FakeTool", (BaseTool,), {**fields, "endpoint": endpoint})


@pytest.mark.unit
class TestValidateToolMetadataRefusals:
    def test_valid_tool_passes(self) -> None:
        validate_tool_metadata([_make_tool_cls()])

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_missing_text_field_refused(self, field: str) -> None:
        cls = _make_tool_cls()
        delattr(cls, field)
        with pytest.raises(RuntimeError, match=field):
            validate_tool_metadata([cls])

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_empty_text_field_refused(self, field: str) -> None:
        with pytest.raises(RuntimeError, match=field):
            validate_tool_metadata([_make_tool_cls(**{field: "   "})])

    @pytest.mark.parametrize("field", ["read_only_hint", "destructive_hint", "open_world_hint"])
    def test_missing_safety_hint_refused(self, field: str) -> None:
        cls = _make_tool_cls()
        delattr(cls, field)
        with pytest.raises(RuntimeError, match=field):
            validate_tool_metadata([cls])

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("read_only_hint", 1),
            ("destructive_hint", "no"),
            ("open_world_hint", None),
        ],
    )
    def test_non_bool_safety_hint_refused(self, field: str, value: Any) -> None:
        with pytest.raises(RuntimeError, match=field):
            validate_tool_metadata([_make_tool_cls(**{field: value})])

    def test_missing_oauth_scopes_refused(self) -> None:
        cls = _make_tool_cls()
        delattr(cls, "oauth_scopes")
        with pytest.raises(RuntimeError, match="oauth_scopes"):
            validate_tool_metadata([cls])

    @pytest.mark.parametrize(
        "scopes",
        [(), ("menhir:write", "extra"), "menhir:write", (42,)],
    )
    def test_invalid_oauth_scopes_refused(self, scopes: Any) -> None:
        with pytest.raises(RuntimeError, match="oauth_scopes"):
            validate_tool_metadata([_make_tool_cls(oauth_scopes=scopes)])

    @pytest.mark.parametrize(
        ("tier", "expected_scopes"),
        [
            ("readonly", ("menhir:read",)),
            ("agent", ("menhir:write",)),
            ("operator", ("menhir:admin",)),
        ],
    )
    def test_scope_tier_mismatch_refused(self, tier: str, expected_scopes: tuple[str, ...]) -> None:
        wrong_scopes = {"readonly": ("menhir:admin",), "agent": ("menhir:read",), "operator": ("menhir:write",)}[tier]
        with pytest.raises(RuntimeError, match="contradict their required_tier"):
            validate_tool_metadata([_make_tool_cls(required_tier=tier, oauth_scopes=wrong_scopes)])
        # And the coherent declaration passes.
        validate_tool_metadata([_make_tool_cls(required_tier=tier, oauth_scopes=expected_scopes)])

    def test_all_problems_aggregated_across_tools(self) -> None:
        missing_title = _make_tool_cls(name="tool_a")
        delattr(missing_title, "title")
        bad_scopes = _make_tool_cls(name="tool_b", required_tier="operator")
        with pytest.raises(RuntimeError) as excinfo:
            validate_tool_metadata([missing_title, bad_scopes])
        assert "tool_a" in str(excinfo.value)
        assert "tool_b" in str(excinfo.value)


@pytest.mark.unit
class TestRegisteredDescriptors:
    async def test_descriptor_fields_after_registration(self) -> None:
        mcp = FastMCP("contract-test")
        tool = _make_tool_cls()()
        tool.register(mcp)

        async with Client(mcp) as client:
            tools = await client.list_tools()

        assert len(tools) == 1
        registered = tools[0]
        assert registered.name == "fake_tool"
        assert registered.title == "Fake Tool"
        assert "A fake tool for contract tests." in registered.description
        # Input schema derived from the endpoint signature is preserved.
        params = set(registered.inputSchema.get("properties", {}))
        assert "namespace" in params
        annotations = registered.annotations
        assert annotations is not None
        assert annotations.readOnlyHint is False
        assert annotations.destructiveHint is False
        assert annotations.openWorldHint is True
        meta = registered.meta or {}
        schemes = meta.get("securitySchemes")
        assert schemes == [{"type": "oauth2", "scopes": ["menhir:write"]}]

    async def test_readonly_tool_descriptor_hints_and_scopes(self) -> None:
        mcp = FastMCP("contract-test-readonly")
        tool = _make_tool_cls(
            name="fake_readonly",
            title="Fake Readonly",
            scope="namespaced",
            required_tier="readonly",
            oauth_scopes=("menhir:read",),
            read_only_hint=True,
            destructive_hint=False,
            open_world_hint=False,
        )()
        tool.register(mcp)

        async with Client(mcp) as client:
            tools = await client.list_tools()

        registered = tools[0]
        assert registered.annotations.readOnlyHint is True
        assert registered.annotations.openWorldHint is False
        assert registered.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["menhir:read"]}]


@pytest.mark.unit
async def test_complete_visible_tool_catalog_has_chatgpt_metadata() -> None:
    validate_tool_metadata(ALL_TOOLS)
    mcp = FastMCP("complete-chatgpt-catalog")
    register_all_tools(mcp)

    async with Client(mcp) as client:
        registered_tools = await client.list_tools()

    assert len(registered_tools) == len(ALL_TOOLS)
    for tool in registered_tools:
        assert tool.title
        assert tool.description
        assert tool.inputSchema.get("type") == "object"
        assert tool.annotations is not None
        assert isinstance(tool.annotations.readOnlyHint, bool)
        assert isinstance(tool.annotations.destructiveHint, bool)
        assert isinstance(tool.annotations.openWorldHint, bool)
        schemes = (tool.meta or {}).get("securitySchemes")
        assert isinstance(schemes, list) and len(schemes) == 1
        assert schemes[0]["type"] == "oauth2"
        assert schemes[0]["scopes"] in (
            ["menhir:read"],
            ["menhir:write"],
            ["menhir:admin"],
        )
