"""What an MCP client is told: the shared server instructions and the registered tool descriptions."""

from __future__ import annotations

import re

from menhir.mcp.instructions import SERVER_INSTRUCTIONS


def _tool_names() -> set[str]:
    from menhir.mcp.tools import ALL_TOOLS

    return {cls.name for cls in ALL_TOOLS}


def test_both_transports_send_the_shared_instructions() -> None:
    from menhir.api import mcp_remote
    from menhir.mcp import server

    assert mcp_remote._INSTRUCTIONS == SERVER_INSTRUCTIONS
    assert server.mcp.instructions == SERVER_INSTRUCTIONS


def test_instructions_say_when_to_recall_and_how_to_read_sources() -> None:
    for phrase in ("recall_memories", "get_provenance", "query_structure", "read_flagged_memories"):
        assert phrase in SERVER_INSTRUCTIONS


def test_every_tool_the_instructions_name_exists() -> None:
    # Guards against the text drifting from the tool surface (a renamed or removed tool).
    named = set(re.findall(r"\b([a-z]+(?:_[a-z]+)+)\b", SERVER_INSTRUCTIONS))
    parameters = {
        "reader_id", "file_context", "file_context_project", "include_invalidated", "node_uuid",
        "content_chars",
    }
    assert named - parameters <= _tool_names(), named - parameters - _tool_names()


def test_no_registered_description_repeats_its_lead_sentence() -> None:
    from menhir.mcp.tools import ALL_TOOLS

    repeated = []
    for cls in ALL_TOOLS:
        lead, _, rest = cls().registered_description().partition("\n\n")
        if lead and " ".join(rest.split()).lower().startswith(" ".join(lead.split()).lower()):
            repeated.append(cls.name)
    assert repeated == []


def test_a_docstring_opening_with_the_curated_line_keeps_only_the_rest() -> None:
    from menhir.mcp.contracts import BaseTool

    class _Tool(BaseTool):
        name = "t"
        description = "Curated one-liner."

        async def endpoint(self) -> str:  # type: ignore[override]
            """Curated one-liner. Returns things.

            Args:
                x: something.
            """
            return ""

    rendered = _Tool().registered_description()
    assert rendered.count("Curated one-liner.") == 1
    assert rendered.startswith("Curated one-liner.\n\nReturns things.")
    assert "Args:" in rendered


def test_stdio_agents_see_the_tools_the_instructions_rely_on() -> None:
    import asyncio

    from menhir.mcp import server

    visible = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert {"recall_memories", "get_provenance", "query_structure", "read_flagged_memories"} <= visible

