"""#681 / #680: import and test linking, stack-matched test commands, stale derived edges.

Each #681 case gets its own on-disk fixture scanned by the production scanner. The #680 prune is
checked here for the statements it emits; `test_structure_edges_c2_online.py` checks what it
deletes against a real Neo4j.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from menhir.infrastructure.project_scanner import (
    FileEntry,
    ImportEdge,
    ProjectScanner,
    ProjectScanResult,
    _detect_test_edges,
)
from menhir.infrastructure.structure_queries import (
    StructureGraphWriter,
    _command_for,
    _test_selection,
)


def _write(root: Path, rel: str, content: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _edges(result) -> set[tuple[str, str]]:
    return {(e.source_path, e.target_path) for e in result.imports}


def _tests(result) -> set[tuple[str, str]]:
    return {(e.test_path, e.source_path) for e in result.test_edges}


# --- #681: ast import parsing ---------------------------------------------------------------


def test_relative_imports_resolve_against_the_importing_package(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "src/pkg/__init__.py")
    _write(tmp_path, "src/pkg/a.py", "from . import b\nfrom .c import thing\nfrom ..top import x\n")
    _write(tmp_path, "src/pkg/b.py")
    _write(tmp_path, "src/pkg/c.py", "thing = 1\n")
    _write(tmp_path, "src/top.py", "x = 1\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert ("src/pkg/a.py", "src/pkg/b.py") in edges
    assert ("src/pkg/a.py", "src/pkg/c.py") in edges
    assert ("src/pkg/a.py", "src/top.py") in edges


def test_from_package_import_submodule_links_the_submodule(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "src/pkg/__init__.py", "VERSION = 1\n")
    _write(tmp_path, "src/pkg/sub.py")
    _write(tmp_path, "src/app.py", "from pkg import sub\n")
    _write(tmp_path, "src/other.py", "from pkg import VERSION\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert ("src/app.py", "src/pkg/sub.py") in edges
    # Importing a submodule runs the package __init__ first, so both are dependencies.
    assert ("src/app.py", "src/pkg/__init__.py") in edges
    # A name that is not a module still links to the package that defines it.
    assert ("src/other.py", "src/pkg/__init__.py") in edges


def test_every_name_in_a_multi_import_is_linked(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    _write(
        tmp_path,
        "main_mod.py",
        "import a, b\n\nfrom a import (\n    x,\n    y,\n)\n",
    )

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert {("main_mod.py", "a.py"), ("main_mod.py", "b.py")} <= edges


def test_a_file_ast_cannot_parse_still_contributes_its_imports(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "db.py")
    _write(tmp_path, "broken.py", "from db import connect\nprint 'python 2'\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert ("broken.py", "db.py") in edges


# --- #681: stack per directory --------------------------------------------------------------


def test_a_monorepo_without_a_root_signal_still_links_python(tmp_path: Path) -> None:
    """The old scanner decided the stack once at the root: no root pyproject, no imports."""
    _write(tmp_path, "services/api/pyproject.toml", "[project]\nname='api'\n")
    _write(tmp_path, "services/api/src/api/__init__.py")
    _write(tmp_path, "services/api/src/api/db.py", "def connect():\n    pass\n")
    _write(
        tmp_path,
        "services/api/src/api/handlers.py",
        "from api.db import connect\n\ndef handle():\n    connect()\n",
    )
    _write(tmp_path, "services/api/tests/test_handlers.py", "from api.handlers import handle\n")
    _write(tmp_path, "web/package.json", json.dumps({"devDependencies": {"vitest": "1"}}))
    _write(tmp_path, "web/src/app.test.ts", "")

    result = ProjectScanner().scan(tmp_path)
    by_path = {f.rel_path: f for f in result.files}

    assert result.stack == "unknown"  # the root-level summary keeps its meaning
    assert ("services/api/src/api/handlers.py", "services/api/src/api/db.py") in _edges(result)
    assert (
        "services/api/tests/test_handlers.py",
        "services/api/src/api/handlers.py",
    ) in _edges(result)
    # Call edges no longer need a root-level python signal either.
    assert any("handlers.py" in c.caller_path and "db.py" in c.callee_path for c in result.call_edges)
    assert by_path["services/api/tests/test_handlers.py"].stack_root == "services/api"
    assert by_path["services/api/tests/test_handlers.py"].test_runner == "pytest"
    assert by_path["web/src/app.test.ts"].stack_root == "web"
    assert by_path["web/src/app.test.ts"].test_runner == "npx vitest run"


def test_a_name_defined_in_two_subprojects_resolves_to_the_importers_own(tmp_path: Path) -> None:
    for svc in ("one", "two"):
        _write(tmp_path, f"{svc}/pyproject.toml", "[project]\nname='x'\n")
        _write(tmp_path, f"{svc}/src/common/__init__.py")
        _write(tmp_path, f"{svc}/src/common/util.py")
        _write(tmp_path, f"{svc}/src/app.py", "from common import util\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert ("one/src/app.py", "one/src/common/util.py") in edges
    assert ("two/src/app.py", "two/src/common/util.py") in edges
    assert ("one/src/app.py", "two/src/common/util.py") not in edges


# --- #681: test links ------------------------------------------------------------------------


def test_duplicate_basenames_pick_the_source_the_test_imports() -> None:
    files = [
        FileEntry("a/utils.py"),
        FileEntry("b/utils.py"),
        FileEntry("tests/test_utils.py", role="test"),
    ]
    imports = [ImportEdge("tests/test_utils.py", "b/utils.py")]

    assert {(e.test_path, e.source_path) for e in _detect_test_edges(files, imports)} == {
        ("tests/test_utils.py", "b/utils.py")
    }


def test_duplicate_basenames_pick_the_mirroring_directory() -> None:
    files = [
        FileEntry("src/pkg/api/models.py"),
        FileEntry("src/pkg/db/models.py"),
        FileEntry("tests/db/test_models.py", role="test"),
    ]

    assert {(e.test_path, e.source_path) for e in _detect_test_edges(files)} == {
        ("tests/db/test_models.py", "src/pkg/db/models.py")
    }


# --- #681: query side ------------------------------------------------------------------------


class _ScriptedNeo4j:
    """Returns canned rows by a substring of the query; records every call."""

    def __init__(self, script: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_kw: Any):
        self.calls.append((query, params or {}))
        for marker, rows in self.script:
            if marker in query:
                return rows
        return []


def _blast_script(importer_rows, stamped):
    return [
        ("IMPORTS*1..", importer_rows),
        ("structure_test_runner AS runner", stamped),
    ]


def test_a_test_that_imports_changed_code_is_selected_whatever_its_name() -> None:
    """Live counterexample: tests/test_cli_up.py imports cli/up.py but is not named test_up.py."""
    neo = _ScriptedNeo4j(
        _blast_script(
            [
                {
                    "importer_path": "tests/test_cli_up.py",
                    "hop_distance": 1,
                    "changed_path": "src/menhir/cli/up.py",
                    "changed_role": "file",
                    "importer_role": "test",
                },
                {
                    "importer_path": "tests/helpers.py",
                    "hop_distance": 1,
                    "changed_path": "src/menhir/cli/up.py",
                    "changed_role": "file",
                    "importer_role": "test",
                },
            ],
            [{"path": "tests/test_cli_up.py", "stack_root": ".", "runner": "pytest"}],
        )
    )
    writer = StructureGraphWriter(neo4j=neo)

    radius = writer.query_blast_radius("menhir", ["src/menhir/cli/up.py"])
    result = writer.query_affected_tests("menhir", ["src/menhir/cli/up.py"])

    assert {"test": "tests/test_cli_up.py", "covers": "src/menhir/cli/up.py", "via": "imports"} in (
        radius["affected_tests"]
    )
    # A helper under tests/ is not itself a test to run.
    assert "tests/helpers.py" not in result["test_files"]
    assert result["test_files"] == ["tests/test_cli_up.py"]
    assert result["test_command"] == "pytest tests/test_cli_up.py"


def test_conftest_selects_its_directory() -> None:
    assert _test_selection("tests/conftest.py") == "tests/"
    assert _test_selection("conftest.py") == "./"
    assert _test_selection("tests/unit/test_x.py") == "tests/unit/test_x.py"
    assert _test_selection("tests/fixtures/build.py") is None


def test_commands_match_each_tests_stack_and_run_from_its_root() -> None:
    neo = _ScriptedNeo4j(
        [
            (
                "structure_test_runner AS runner",
                [
                    {"path": "services/api/tests/test_h.py", "stack_root": "services/api", "runner": "pytest"},
                    {"path": "web/src/app.test.ts", "stack_root": "web", "runner": "npx vitest run"},
                ],
            )
        ]
    )
    writer = StructureGraphWriter(neo4j=neo)

    commands = writer._test_commands(
        "mono", ["services/api/tests/test_h.py", "web/src/app.test.ts", "legacy/test_old.py"]
    )

    assert commands == [
        # Unstamped (graph written before v12): falls back by extension, from the repo root.
        "pytest legacy/test_old.py",
        "cd services/api && pytest tests/test_h.py",
        "cd web && npx vitest run src/app.test.ts",
    ]


def test_suite_runners_do_not_take_file_arguments() -> None:
    assert _command_for(".", "cargo test", ["tests/a.rs"]) == "cargo test"
    assert _command_for("svc", "./gradlew test", ["svc/src/test/ATest.java"]) == (
        "cd svc && ./gradlew test"
    )
    assert _command_for(".", "go test", ["pkg/a_test.go", "b_test.go"]) == "go test . ./pkg"


# --- #680: the prune's statements ------------------------------------------------------------


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None, **_kw: Any):
        self.calls.append((query, params or {}))
        if "count(r)" in query:
            return [{"cnt": 1}]
        if "RETURN count(p) AS updated" in query:
            return [{"updated": 1}]
        return []


def _scan(**overrides: Any) -> ProjectScanResult:
    defaults: dict[str, Any] = {
        "name": "proj",
        "root_path": "/tmp/proj",
        "stack": "python",
        "description": "",
        "files": [
            FileEntry("a.py", file_mtime=1.0),
            FileEntry("b.py", file_mtime=1.0),
            FileEntry("lonely.py", file_mtime=1.0),
            FileEntry("tests/test_a.py", role="test", file_mtime=1.0),
        ],
        "imports": [ImportEdge("a.py", "b.py"), ImportEdge("tests/test_a.py", "a.py")],
        "project_id": "pid-1",
        "files_discovered": 4,
        "files_eligible": 4,
        "files_indexed": 4,
    }
    defaults.update(overrides)
    return ProjectScanResult(**defaults)


def _derived_prunes(neo: _Recording) -> list[tuple[str, dict[str, Any]]]:
    return [(q, p) for q, p in neo.calls if "DELETE r" in q and "$scanned" in q]


def test_every_scanned_file_is_a_prune_source_even_with_no_edges_left() -> None:
    """A file that drops its LAST import has no row in the edge list; it must still be pruned."""
    neo = _Recording()
    StructureGraphWriter(neo4j=neo).write_project(_scan(), "s", "u")

    prunes = _derived_prunes(neo)
    assert prunes
    for _q, params in prunes:
        assert "lonely.py" in params["scanned"]
        assert "edges" not in params


def test_keep_sets_are_per_relationship_type() -> None:
    """tests/test_a.py has a TESTS and an IMPORTS edge to a.py; neither may keep the other alive."""
    neo = _Recording()
    scan = _scan(imports=[ImportEdge("a.py", "b.py")])
    scan.test_edges = []
    from menhir.infrastructure.project_scanner import TestEdge

    scan.test_edges = [TestEdge("tests/test_a.py", "a.py")]
    StructureGraphWriter(neo4j=neo).write_project(scan, "s", "u")

    keeps = {}
    for query, params in _derived_prunes(neo):
        rel = "TESTS" if "[r:TESTS]" in query else "IMPORTS"
        keeps.setdefault(rel, params["keep"])
    assert keeps["TESTS"] == [["tests/test_a.py", "a.py"]]
    assert keeps["IMPORTS"] == [["a.py", "b.py"]]


def test_the_prune_is_identity_keyed_and_bounded_to_scanned_targets() -> None:
    neo = _Recording()
    StructureGraphWriter(neo4j=neo).write_project(_scan(), "s", "u")

    prunes = _derived_prunes(neo)
    # Two arms per relationship type, never an OR, never the name alone while an id is known.
    assert len(prunes) == 4
    for query, params in prunes:
        flat = " ".join(query.split())
        assert params["project_id"] == "pid-1"
        assert (
            "a.structure_project_id = $project_id" in flat
            or "a.structure_project_id IS NULL AND a.structure_project = $project" in flat
        )
        # An edge into a file this scan did not see (the cap dropped it) is never a candidate.
        assert "b.structure_path IN $scanned" in flat
        assert "DETACH" not in flat


def test_a_capped_scan_still_prunes_between_scanned_files() -> None:
    neo = _Recording()
    scan = _scan(files_eligible=10)  # partial_index: the cap bound
    assert scan.partial_index
    StructureGraphWriter(neo4j=neo).write_project(scan, "s", "u")

    assert _derived_prunes(neo)


# --- #681: runner follows the file's language, not just the nearest signal ------------------


def test_a_ts_test_under_a_python_root_is_not_a_pytest_file(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "tests/test_a.py")
    _write(tmp_path, "ui/widget.test.ts")

    by_path = {f.rel_path: f for f in ProjectScanner().scan(tmp_path).files}

    assert by_path["tests/test_a.py"].test_runner == "pytest"
    # No package.json anywhere: no JS command at all, rather than `npm test` that cannot run.
    assert by_path["ui/widget.test.ts"].test_runner == ""


def test_a_python_test_under_a_js_package_finds_the_python_root(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "tools/package.json", json.dumps({"devDependencies": {"jest": "29"}}))
    _write(tmp_path, "tools/test_build.py")
    _write(tmp_path, "tools/build.test.js")

    by_path = {f.rel_path: f for f in ProjectScanner().scan(tmp_path).files}

    assert (by_path["tools/test_build.py"].stack_root, by_path["tools/test_build.py"].test_runner) == (
        ".",
        "pytest",
    )
    assert (by_path["tools/build.test.js"].stack_root, by_path["tools/build.test.js"].test_runner) == (
        "tools",
        "npx jest",
    )


def test_a_package_json_linked_out_of_the_project_is_not_read(tmp_path: Path) -> None:
    import os

    import pytest

    outside = tmp_path / "outside"
    _write(outside, "package.json", json.dumps({"devDependencies": {"vitest": "1"}}))
    project = tmp_path / "proj"
    _write(project, "src/a.test.ts")
    try:
        os.symlink(outside / "package.json", project / "package.json")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"cannot create symlinks here: {error}")

    by_path = {f.rel_path: f for f in ProjectScanner().scan(project).files}

    assert by_path["src/a.test.ts"].test_runner == "npm test --"


def test_a_sub_project_conftest_runs_from_its_own_root() -> None:
    neo = _ScriptedNeo4j(
        [
            (
                "structure_test_runner AS runner",
                [
                    {"path": "services/api/tests/conftest.py", "stack_root": "services/api", "runner": "pytest"},
                    {"path": "services/api/tests/test_h.py", "stack_root": "services/api", "runner": "pytest"},
                ],
            )
        ]
    )
    commands = StructureGraphWriter(neo4j=neo)._test_commands(
        "mono", ["services/api/tests/", "services/api/tests/test_h.py"]
    )

    assert commands == ["cd services/api && pytest tests/ tests/test_h.py"]


def test_no_selected_tests_suggests_the_changed_files_own_suite() -> None:
    """A TS-only change with no tests: `npx vitest run` from its package, never `pytest`."""
    neo = _ScriptedNeo4j(
        [
            ("IMPORTS*1..", []),
            (
                "structure_test_runner AS runner",
                [{"path": "web/src/util.ts", "stack_root": "web", "runner": "npx vitest run"}],
            ),
        ]
    )
    result = StructureGraphWriter(neo4j=neo).query_affected_tests("mono", ["web/src/util.ts"])

    assert result["test_files"] == []
    assert result["test_command"] == (
        "cd web && npx vitest run  # no specific tests found — run full suite"
    )
    assert "pytest" not in result["test_command"]


def test_no_paths_lists_every_stack_roots_suite() -> None:
    neo = _ScriptedNeo4j(
        [
            (
                "RETURN DISTINCT t.structure_stack_root",
                [
                    {"stack_root": ".", "runner": "pytest"},
                    {"stack_root": "web", "runner": "npm test --"},
                    {"stack_root": "svc", "runner": "go test"},
                ],
            )
        ]
    )
    result = StructureGraphWriter(neo4j=neo).query_affected_tests("mono", [])

    assert result["test_commands"] == ["pytest", "cd svc && go test ./...", "cd web && npm test"]


# --- review findings on PR #695 -------------------------------------------------------------


def test_a_module_named_setup_inside_a_package_is_not_a_project_root(tmp_path: Path) -> None:
    """F1: `src/app/cli/setup.py` is the setup command, not a manifest."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "src/app/__init__.py")
    _write(tmp_path, "src/app/cli/__init__.py")
    _write(tmp_path, "src/app/cli/setup.py")
    _write(tmp_path, "src/app/cli/logging.py", "def getLogger():\n    pass\n")
    _write(tmp_path, "src/app/core.py", "import logging\n\ndef f():\n    logging.getLogger()\n")
    _write(
        tmp_path, "src/app/cli/tool.py", "from logging import getLogger\n\ndef g():\n    getLogger()\n"
    )

    result = ProjectScanner().scan(tmp_path)
    by_path = {f.rel_path: f for f in result.files}

    assert by_path["src/app/cli/setup.py"].stack_root == "."
    assert not {e for e in _edges(result) if e[1] == "src/app/cli/logging.py"}
    assert not [c for c in result.call_edges if "cli/logging.py" in c.callee_path]


def test_a_sub_project_name_is_invisible_outside_it(tmp_path: Path) -> None:
    """F1: `services/a/src/logging.py` is `logging` only to importers inside services/a."""
    _write(tmp_path, "services/a/pyproject.toml", "[project]\nname='a'\n")
    _write(tmp_path, "services/a/src/logging.py")
    _write(tmp_path, "services/a/src/app.py", "import logging\n")
    _write(tmp_path, "tools/run.py", "import logging\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert ("services/a/src/app.py", "services/a/src/logging.py") in edges
    assert not {e for e in edges if e[0] == "tools/run.py"}


def test_fallback_reads_a_parenthesised_import_spread_over_lines(tmp_path: Path) -> None:
    """F4: before, only the first physical line was read and a.py / b.py were lost."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/a.py")
    _write(tmp_path, "pkg/b.py")
    _write(tmp_path, "pkg/main_mod.py", "from . import (\n    a,\n    b,\n)\nprint 'py2'\n")

    edges = _edges(ProjectScanner().scan(tmp_path))

    assert {("pkg/main_mod.py", "pkg/a.py"), ("pkg/main_mod.py", "pkg/b.py")} <= edges


def test_relative_import_past_the_top_level_package_resolves_nothing(tmp_path: Path) -> None:
    """F12: Python refuses `from .. import x` in a top-level package; so does the scanner."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "x.py")
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/a.py", "from .. import x\n")

    assert not {e for e in _edges(ProjectScanner().scan(tmp_path)) if e[0] == "pkg/a.py"}


def test_conftest_and_go_tests_are_tests_wherever_they_sit(tmp_path: Path) -> None:
    """F11 / F18: a root conftest.py was role `file`, so its `./` selection was unreachable."""
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(tmp_path, "conftest.py")
    _write(tmp_path, "svc/go.mod", "module svc\n")
    _write(tmp_path, "svc/pkg/a_test.go")

    by_path = {f.rel_path: f for f in ProjectScanner().scan(tmp_path).files}
    go_test = by_path["svc/pkg/a_test.go"]

    assert by_path["conftest.py"].role == "test"
    assert go_test.role == "test"
    assert (go_test.stack_root, go_test.test_runner) == ("svc", "go test")


def test_a_gradle_sub_project_runs_through_the_root_wrapper(tmp_path: Path) -> None:
    """F8: `gradlew` lives at the build root, so `cd app && ./gradlew test` cannot run."""
    _write(tmp_path, "gradlew", "#!/bin/sh\n")
    _write(tmp_path, "settings.gradle", "include 'app'\n")
    _write(tmp_path, "app/build.gradle")
    _write(tmp_path, "app/src/test/java/ATest.java")

    by_path = {f.rel_path: f for f in ProjectScanner().scan(tmp_path).files}
    test = by_path["app/src/test/java/ATest.java"]

    assert (test.stack_root, test.test_runner) == (".", "./gradlew :app:test")
    assert _command_for(test.stack_root, test.test_runner, [test.rel_path]) == "./gradlew :app:test"


def test_a_gradle_project_without_a_wrapper_uses_plain_gradle(tmp_path: Path) -> None:
    _write(tmp_path, "svc/build.gradle")
    _write(tmp_path, "svc/src/test/java/ATest.java")

    test = {f.rel_path: f for f in ProjectScanner().scan(tmp_path).files}["svc/src/test/java/ATest.java"]

    assert (test.stack_root, test.test_runner) == ("svc", "gradle test")


def test_commands_quote_paths_with_spaces() -> None:
    """F9."""
    assert _command_for("my svc", "pytest", ["my svc/tests/test x.py"]) == (
        "cd 'my svc' && pytest 'tests/test x.py'"
    )


def test_only_direct_or_test_helper_importers_are_selected() -> None:
    """F2: a test reaching the change only through other source files is not selected."""
    neo = _ScriptedNeo4j(
        _blast_script(
            [
                {  # imports the change directly
                    "importer_path": "tests/test_direct.py", "hop_distance": 1,
                    "changed_path": "src/a.py", "changed_role": "file",
                    "importer_role": "test", "via_tests": True,
                },
                {  # through a test helper only
                    "importer_path": "tests/test_via_helper.py", "hop_distance": 2,
                    "changed_path": "src/a.py", "changed_role": "file",
                    "importer_role": "test", "via_tests": True,
                },
                {  # through source files: transitively affected, not selected
                    "importer_path": "tests/conftest.py", "hop_distance": 4,
                    "changed_path": "src/a.py", "changed_role": "file",
                    "importer_role": "test", "via_tests": False,
                },
            ],
            [],
        )
    )
    radius = StructureGraphWriter(neo4j=neo).query_blast_radius("p", ["src/a.py"])

    assert sorted(t["test"] for t in radius["affected_tests"]) == [
        "tests/test_direct.py",
        "tests/test_via_helper.py",
    ]
    assert "tests/conftest.py" in radius["transitively_affected"]


def test_a_changed_test_is_reported_as_changed_not_as_importing_itself() -> None:
    """F14."""
    neo = _ScriptedNeo4j(
        _blast_script(
            [
                {
                    "importer_path": None, "hop_distance": None,
                    "changed_path": "tests/test_a.py", "changed_role": "test",
                    "importer_role": None, "via_tests": None,
                }
            ],
            [],
        )
    )
    radius = StructureGraphWriter(neo4j=neo).query_blast_radius("p", ["tests/test_a.py"])

    assert radius["affected_tests"] == [
        {"test": "tests/test_a.py", "covers": "tests/test_a.py", "via": "changed"}
    ]


def test_every_suite_line_is_shown_when_no_tests_were_selected() -> None:
    """F10: the formatter indented only the first of several suite commands."""
    from menhir.mcp.tools.recall.query_structure import _format_affected_tests

    out = _format_affected_tests(
        {
            "changed_files": ["a.py"],
            "affected_source_files": [],
            "test_files": [],
            "test_command": "pytest  # x\ncd web && npm test  # x",
            "test_commands": [],
            "unindexed_paths": [],
            "coverage": {"known": True},
        }
    )

    assert "\n  pytest  # x" in out
    assert "\n  cd web && npm test  # x" in out


def test_an_unread_file_is_not_a_prune_source() -> None:
    """F13: a .py file that vanished mid-scan keeps its stored edges."""
    neo = _Recording()
    scan = _scan()
    scan.import_sources_unread = ["a.py"]
    StructureGraphWriter(neo4j=neo).write_project(scan, "s", "u")

    prunes = _derived_prunes(neo)
    assert prunes
    for _q, params in prunes:
        assert "a.py" not in params["sources"]
        assert "a.py" in params["scanned"]  # still a valid target


def test_a_file_that_vanishes_during_the_import_pass_is_recorded_unread(tmp_path: Path) -> None:
    from menhir.infrastructure.project_scanner import _analyze_python_imports

    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py")
    files = [FileEntry("a.py"), FileEntry("b.py"), FileEntry("gone.py")]
    unread: list[str] = []

    edges, _ = _analyze_python_imports(tmp_path, files, unread=unread)

    assert unread == ["gone.py"]
    assert ("a.py", "b.py") in {(e.source_path, e.target_path) for e in edges}


def test_the_call_cap_counts_distinct_callees_not_call_sites(tmp_path: Path) -> None:
    """F15: 60 calls to one helper used to exhaust the 50-call budget before `other` was seen."""
    body = "\n".join(["    helper()"] * 60 + ["    other()"])
    _write(tmp_path, "pyproject.toml", "[project]\nname='p'\n")
    _write(
        tmp_path,
        "m.py",
        "def helper():\n    pass\n\ndef other():\n    pass\n\ndef f():\n" + body + "\n",
    )

    calls = {(c.caller_path, c.callee_path) for c in ProjectScanner().scan(tmp_path).call_edges}

    assert ("m.py::f", "m.py::other") in calls
