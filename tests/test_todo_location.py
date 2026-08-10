"""Unit tests for code_ref -> TodoLocation normalization."""

from __future__ import annotations

import pytest

from menhir.domain.todo_location import (
    LOCATION_SCHEMA_VERSION,
    parse_code_ref,
)

KNOWN = frozenset({"menhir", "yawn.market", "yawn.vps", "archolith-context", "yawn.frontend"})


# ---------------------------------------------------------------------------
# Cardinality -- the reason TodoLocation exists
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multi_path_ref_yields_one_location_per_path() -> None:
    locs = parse_code_ref("src/pages/sealed.astro, src/pages/graded.astro")

    assert [l.path for l in locs] == ["src/pages/sealed.astro", "src/pages/graded.astro"]
    assert [l.ordinal for l in locs] == [0, 1]


@pytest.mark.unit
def test_semicolon_and_plus_also_separate_segments() -> None:
    assert len(parse_code_ref("a/one.py; b/two.py")) == 2
    assert len(parse_code_ref("a/one.py + b/two.py")) == 2


@pytest.mark.unit
def test_ordinal_preserves_author_ordering() -> None:
    locs = parse_code_ref("z/last.py, a/first.py")
    assert [l.path for l in locs] == ["z/last.py", "a/first.py"]


@pytest.mark.unit
def test_exact_duplicate_locations_collapse() -> None:
    locs = parse_code_ref("src/a.py, src/a.py")
    assert len(locs) == 1


@pytest.mark.unit
def test_partially_resolvable_ref_keeps_both_outcomes() -> None:
    """One good segment and one prose segment: both are retained."""
    locs = parse_code_ref("src/real.py, needs a PUT endpoint (not a path)")

    assert len(locs) == 2
    assert locs[0].resolution_status == "resolved"
    assert locs[1].resolution_status == "unresolved"
    assert locs[1].unresolved_reason == "not_a_path"


@pytest.mark.unit
def test_empty_and_none_yield_no_locations() -> None:
    assert parse_code_ref(None) == []
    assert parse_code_ref("   ") == []


# ---------------------------------------------------------------------------
# Project extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_workspace_relative_path_splits_project_and_path() -> None:
    loc = parse_code_ref("projects/archolith/menhir/src/menhir/api/oauth.py")[0]
    assert loc.project == "menhir"
    assert loc.path == "src/menhir/api/oauth.py"


@pytest.mark.unit
def test_bare_project_prefix_resolves_only_when_known() -> None:
    known = parse_code_ref("archolith-context/archolith_proxy/proxy/locks.py", known_projects=KNOWN)[0]
    assert known.project == "archolith-context"
    assert known.path == "archolith_proxy/proxy/locks.py"

    unknown = parse_code_ref("archolith-context/archolith_proxy/proxy/locks.py")[0]
    assert unknown.project is None
    assert unknown.path == "archolith-context/archolith_proxy/proxy/locks.py"


@pytest.mark.unit
def test_project_relative_path_has_no_project() -> None:
    loc = parse_code_ref("src/menhir/infrastructure/neo4j.py")[0]
    assert loc.project is None
    assert loc.path == "src/menhir/infrastructure/neo4j.py"


@pytest.mark.unit
def test_explicit_structure_project_wins() -> None:
    loc = parse_code_ref("src/menhir/api/routes.py", structure_project="menhir")[0]
    assert loc.project == "menhir"


@pytest.mark.unit
def test_conflicting_explicit_and_parsed_project_is_unresolved() -> None:
    """Never silently normalize to one side."""
    loc = parse_code_ref(
        "projects/archolith/menhir/src/menhir/api/oauth.py",
        structure_project="yawn.market",
    )[0]
    assert loc.resolution_status == "unresolved"
    assert loc.unresolved_reason == "project_conflict"


# ---------------------------------------------------------------------------
# Line / symbol suffixes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trailing_line_number() -> None:
    loc = parse_code_ref("vps/core.py:137")[0]
    assert (loc.path, loc.line_start, loc.line_end) == ("vps/core.py", 137, None)


@pytest.mark.unit
def test_trailing_line_range() -> None:
    loc = parse_code_ref("archolith_proxy/openai/chat.py:2303-2330")[0]
    assert (loc.line_start, loc.line_end) == (2303, 2330)


@pytest.mark.unit
def test_double_colon_symbol() -> None:
    loc = parse_code_ref("tests/test_x.py::test_name")[0]
    assert loc.symbol == "test_name"
    assert loc.kind == "symbol"


@pytest.mark.unit
def test_single_colon_non_numeric_is_a_symbol_not_a_line() -> None:
    loc = parse_code_ref("archolith_proxy/openai/chat.py:skipped_inflation guard")[0]
    assert loc.path == "archolith_proxy/openai/chat.py"
    assert loc.symbol == "skipped_inflation guard"


# ---------------------------------------------------------------------------
# Normalization safety
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backslashes_normalize_to_forward_slashes() -> None:
    loc = parse_code_ref(r"src\menhir\api\routes.py")[0]
    assert loc.path == "src/menhir/api/routes.py"


@pytest.mark.unit
def test_path_escaping_repository_root_is_rejected() -> None:
    loc = parse_code_ref("../../etc/passwd")[0]
    assert loc.resolution_status == "unresolved"
    assert loc.unresolved_reason == "path_escapes_root"


@pytest.mark.unit
def test_interior_dotdot_collapses_without_escaping() -> None:
    loc = parse_code_ref("src/menhir/../menhir/api/routes.py")[0]
    assert loc.path == "src/menhir/api/routes.py"
    assert loc.resolution_status == "resolved"


@pytest.mark.unit
def test_absolute_windows_path_is_trimmed_to_workspace_relative() -> None:
    loc = parse_code_ref(r"C:\Users\dev\IdeaProjects\projects\archolith\menhir\src\menhir\api\routes.py")[0]
    assert loc.project == "menhir"
    assert loc.path == "src/menhir/api/routes.py"


@pytest.mark.unit
def test_absolute_path_outside_workspace_is_unresolved() -> None:
    loc = parse_code_ref(r"D:\somewhere\else\file.py")[0]
    assert loc.resolution_status == "unresolved"
    assert loc.unresolved_reason == "absolute_path_outside_workspace"


@pytest.mark.unit
def test_prose_is_not_forced_into_a_path() -> None:
    loc = parse_code_ref("AdminController.java @PostMapping(/products/contents) - needs PUT")[0]
    assert loc.resolution_status == "unresolved"
    assert loc.unresolved_reason == "not_a_path"
    assert loc.path is None


@pytest.mark.unit
def test_directory_kind_for_trailing_slash() -> None:
    assert parse_code_ref("projects/yawn/yawn.dashboard/")[0].kind == "directory"


@pytest.mark.unit
def test_raw_segment_and_schema_version_are_retained() -> None:
    loc = parse_code_ref("src/a.py:12")[0]
    assert loc.raw_segment == "src/a.py:12"
    assert loc.schema_version == LOCATION_SCHEMA_VERSION


@pytest.mark.unit
def test_as_properties_exposes_the_full_node_shape() -> None:
    props = parse_code_ref("src/a.py:12")[0].as_properties()
    assert set(props) == {
        "ordinal", "raw_segment", "project", "path", "kind",
        "line_start", "line_end", "symbol",
        "resolution_status", "unresolved_reason", "schema_version",
    }
