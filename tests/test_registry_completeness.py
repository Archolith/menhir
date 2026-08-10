"""Regression tests (SSOT-10): canonical docs/registries must not drift from the
runtime MCP tool registry (menhir.mcp.tools.ALL_TOOLS).

Deliberately avoids a PyYAML dependency for concept-ids.yaml -- it's not a
declared project dependency, and a lightweight regex scan for `mcp.tool.<name>:`
keys is sufficient and keeps this test independent of what happens to be
importable in a given environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from menhir.mcp.tools import ALL_TOOLS

_AGENT_DIR = Path(__file__).resolve().parent.parent / ".agent"


def _registered_tool_names() -> set[str]:
    return {tool_cls.name for tool_cls in ALL_TOOLS}


@pytest.mark.unit
def test_endpoints_md_documents_every_registered_tool():
    """Every tool ALL_TOOLS registers must have a `### \\`<name>\\`` section in
    .agent/endpoints.md. Previously only 31 of 42 registered tools had a
    section (SSOT-10)."""
    content = (_AGENT_DIR / "endpoints.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^### `(\w+)`$", content, re.MULTILINE))
    missing = _registered_tool_names() - documented
    assert not missing, f"endpoints.md is missing tool sections for: {sorted(missing)}"


@pytest.mark.unit
def test_concept_ids_yaml_declares_every_registered_tool():
    """Every tool ALL_TOOLS registers must have an `mcp.tool.<name>:` entry in
    .agent/concept-ids.yaml. Previously multiple active tool concept IDs (and
    model.todo) were missing, with no completeness test to catch it (SSOT-10)."""
    content = (_AGENT_DIR / "concept-ids.yaml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*mcp\.tool\.(\w+):", content, re.MULTILINE))
    missing = _registered_tool_names() - declared
    assert not missing, f"concept-ids.yaml is missing mcp.tool entries for: {sorted(missing)}"


@pytest.mark.unit
def test_concept_ids_yaml_has_no_phantom_tool_entries():
    """Guards the other direction: an `mcp.tool.<name>:` entry that doesn't
    correspond to any registered tool is stale documentation, not a real
    concept -- should be removed rather than silently kept."""
    content = (_AGENT_DIR / "concept-ids.yaml").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*mcp\.tool\.(\w+):", content, re.MULTILINE))
    phantom = declared - _registered_tool_names()
    assert not phantom, f"concept-ids.yaml declares mcp.tool entries for nonexistent tools: {sorted(phantom)}"


@pytest.mark.unit
def test_concept_ids_yaml_declares_model_todo():
    """model.todo (the persistent :Todo node schema, documented in
    data_models.md and referenced from endpoints.md) was missing from the
    concept registry entirely (SSOT-10)."""
    content = (_AGENT_DIR / "concept-ids.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*model\.todo:", content, re.MULTILINE), "concept-ids.yaml is missing model.todo"
