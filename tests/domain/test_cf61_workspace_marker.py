"""CF-61: the hardcoded workspace root marker in the domain layer is injectable.

An absolute Windows path is trimmed to its workspace-relative remainder by locating
the workspace root marker. That marker was a compiled-in ``/IdeaProjects/`` literal;
on a machine whose checkout lives elsewhere every absolute path resolved to
``absolute_path_outside_workspace``. The marker is now a keyword-only parameter with
the original literal as its default, so behaviour is unchanged for every existing
caller and the value is injectable.
"""

import pytest

from menhir.domain.todo_location import DEFAULT_WORKSPACE_MARKER, parse_code_ref


@pytest.mark.unit
class TestCustomWorkspaceMarker:
    def test_path_under_a_different_root_resolves(self):
        loc = parse_code_ref(
            "C:/Users/x/src/proj/a/b.py", workspace_marker="/src/"
        )[0]
        assert loc.resolution_status == "resolved"
        assert loc.path == "proj/a/b.py"

    def test_windows_style_marker_is_normalized(self):
        # The text is normalized (backslash -> forward slash) before the marker is
        # compared, so a caller passing a Windows-style marker must still match.
        loc = parse_code_ref(
            r"C:\Users\x\src\proj\a\b.py", workspace_marker="\\src\\"
        )[0]
        assert loc.resolution_status == "resolved"
        assert loc.path == "proj/a/b.py"

    def test_path_under_neither_root_stays_unresolved(self):
        loc = parse_code_ref(
            "D:/somewhere/else/file.py", workspace_marker="/src/"
        )[0]
        assert loc.resolution_status == "unresolved"
        assert loc.unresolved_reason == "absolute_path_outside_workspace"


@pytest.mark.unit
class TestDefaultMarkerUnchanged:
    def test_default_is_idea_projects(self):
        assert DEFAULT_WORKSPACE_MARKER == "/IdeaProjects/"

    def test_default_parses_exactly_as_before(self):
        loc = parse_code_ref(
            r"C:\Users\dev\IdeaProjects\projects\archolith\menhir\src\menhir\api\routes.py"
        )[0]
        assert loc.resolution_status == "resolved"
        assert loc.project == "menhir"
        assert loc.path == "src/menhir/api/routes.py"

    def test_path_outside_default_root_still_unresolved(self):
        loc = parse_code_ref(r"D:\somewhere\else\file.py")[0]
        assert loc.resolution_status == "unresolved"
        assert loc.unresolved_reason == "absolute_path_outside_workspace"


@pytest.mark.unit
class TestEmptyOrNoneMarker:
    def test_none_disables_stripping(self):
        # Chosen semantics: an empty/None marker means "do not strip a workspace
        # root", so an absolute Windows path keeps its existing
        # absolute_path_outside_workspace rejection rather than silently accepting an
        # unportable absolute path.
        loc = parse_code_ref(
            r"C:\Users\dev\IdeaProjects\projects\archolith\menhir\src\a.py",
            workspace_marker=None,
        )[0]
        assert loc.resolution_status == "unresolved"
        assert loc.unresolved_reason == "absolute_path_outside_workspace"

    def test_empty_string_disables_stripping(self):
        loc = parse_code_ref(
            "C:/Users/dev/projects/archolith/menhir/src/a.py",
            workspace_marker="",
        )[0]
        assert loc.resolution_status == "unresolved"
        assert loc.unresolved_reason == "absolute_path_outside_workspace"
