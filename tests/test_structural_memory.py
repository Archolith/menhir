"""Tests for current and legacy structural-memory compatibility detection."""

from __future__ import annotations

import pytest

from menhir.domain.structural_memory import (
    infer_legacy_structure_role,
    is_structural_memory_row,
    legacy_structural_memory_cypher,
    non_structural_memory_cypher,
)


@pytest.mark.unit
@pytest.mark.parametrize("prefix", ["Directory:", "File:", "Project:"])
def test_legacy_project_scan_shape_is_structural(prefix: str) -> None:
    row = {
        "structure_role": None,
        "source": "codex,project-scan,claude-code",
        "content": f"  {prefix} src/example  ",
    }

    assert is_structural_memory_row(row) is True
    assert infer_legacy_structure_role(row) == prefix.removesuffix(":").casefold()


@pytest.mark.unit
def test_semantic_project_scan_row_remains_eligible() -> None:
    row = {
        "structure_role": None,
        "source": "project-scan",
        "content": "The project uses a bounded queue and a durable scheduler lease.",
    }

    assert is_structural_memory_row(row) is False


@pytest.mark.unit
def test_prefix_without_project_scan_provenance_remains_eligible() -> None:
    row = {
        "structure_role": None,
        "source": "manual",
        "content": "Project: remember this user-authored project note.",
    }

    assert is_structural_memory_row(row) is False


@pytest.mark.unit
@pytest.mark.parametrize("prefix", ["Directory:", "File:", "Project:"])
def test_a_merged_structure_row_is_still_recognised_at_the_python_boundary(prefix: str) -> None:
    """Remediation plan Phase 5, test 9 -- the PYTHON half.

    Merge derives `source` as the LOWEST-tier contributor, so a legacy structure row that absorbed a
    `claude-code` duplicate no longer says `project-scan` there; the label survives only in `sources`.
    The Cypher predicate was taught to read that list, this check was not, and the two then disagreed:
    the database excluded the row from memory listings while this boundary check called it a memory.
    """
    row = {
        "structure_role": None,
        "source": "claude-code",                     # what a merge leaves behind
        "sources": ["project-scan", "claude-code"],  # where the provenance actually lives now
        "content": f"  {prefix} src/example  ",
    }

    assert is_structural_memory_row(row) is True
    assert infer_legacy_structure_role(row) == prefix.removesuffix(":").casefold()


@pytest.mark.unit
def test_sources_without_project_scan_does_not_make_a_row_structural() -> None:
    """The list widens recognition, it does not weaken it."""
    row = {
        "structure_role": None,
        "source": "claude-code",
        "sources": ["claude-code", "codex"],
        "content": "Project: remember this user-authored project note.",
    }

    assert is_structural_memory_row(row) is False


@pytest.mark.unit
@pytest.mark.parametrize("sources", [None, [], "project-scan", 42])
def test_a_missing_or_malformed_sources_falls_back_to_source(sources) -> None:
    """Rows predating the list, and anything that is not a list, must still work off `source`."""
    row = {
        "structure_role": None,
        "source": "codex,project-scan,claude-code",
        "sources": sources,
        "content": "Directory: src/example",
    }

    assert is_structural_memory_row(row) is True


@pytest.mark.unit
def test_the_return_projection_carries_the_field_the_check_reads() -> None:
    """`infer_legacy_structure_role` runs over rows built from MEMORY_RETURN_FIELDS. A projection
    that omits `sources` makes the check read None and silently regress to the `source`-only
    behavior -- passing every test above while failing on real data."""
    from menhir.infrastructure.cypher import MEMORY_RETURN_FIELDS

    assert "n.sources AS sources" in MEMORY_RETURN_FIELDS
    assert "n.source AS source" in MEMORY_RETURN_FIELDS


@pytest.mark.unit
def test_cypher_predicate_matches_python_compatibility_contract() -> None:
    predicate = non_structural_memory_cypher("memory")
    positive = legacy_structural_memory_cypher("memory")

    assert "memory.structure_role IS NULL" in predicate
    assert "coalesce(memory.source, '') CONTAINS 'project-scan'" in predicate
    assert "'directory:'" in predicate
    assert "'file:'" in predicate
    assert "'project:'" in predicate
    assert positive in predicate
