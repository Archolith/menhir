"""CF-84: `add_memory`'s prompt-facing description must describe both write paths.

The one-line `description` is the primary text a model reads when choosing this tool, but
the code has a queue-bypass branch for `TEMPORAL` + `valid_at`. This suite guards the
description against drifting out of sync with the actual behavior.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.mcp.tools.ingest.add_memory import AddMemoryTool


@pytest.mark.unit
def test_description_mentions_both_write_paths() -> None:
    desc = AddMemoryTool.description

    assert "queue" in desc.lower()
    assert "temporal" in desc.lower()
    assert "valid_at" in desc


@pytest.mark.unit
def test_description_is_a_single_short_line() -> None:
    desc = AddMemoryTool.description

    assert isinstance(desc, str)
    assert desc.strip()
    assert "\n" not in desc


@pytest.mark.unit
def test_docstring_mentions_temporal_bypasses_queue() -> None:
    doc = inspect.getdoc(AddMemoryTool.endpoint)

    assert doc is not None
    assert "TEMPORAL" in doc
    assert "valid_at" in doc
    assert "bypass" in doc.lower()


@pytest.mark.unit
def test_bypass_branch_still_exists_in_source() -> None:
    source = inspect.getsource(AddMemoryTool)

    bypass_line = next(
        (line for line in source.splitlines() if "TEMPORAL" in line and "valid_at" in line),
        None,
    )
    assert bypass_line is not None, "TEMPORAL + valid_at bypass branch was removed"
    assert "valid_at" in bypass_line
