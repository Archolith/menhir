"""Tests for infrastructure.project_scanner — deterministic project scanning."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from menhir.infrastructure.project_scanner import (
    ProjectScanner,
    SCANNER_SCHEMA_VERSION,
    _cap_files,
    _classify_file_role,
    _detect_test_edges,
    _first_paragraph,
    _parse_imports,
    is_eligible_file,
    FileEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, content: str = "") -> None:
    """Create a file inside a temp directory."""
    p = root / rel.replace("/", os.sep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _make_python_project(tmp_path: Path) -> Path:
    """Scaffold a minimal Python project tree."""
    root = tmp_path / "myproject"
    root.mkdir()
    _write(root, "pyproject.toml", textwrap.dedent("""\
        [project]
        name = "myproject"
        dependencies = [
          "requests>=2.0",
          "pydantic>=2.0,<3",
        ]
    """))
    _write(root, ".gitignore", "*.pyc\n__pycache__/\nbuild/\n")
    _write(root, ".agent/README.md", "# My Project\n\nA demo project for testing.\n")
    _write(root, "src/myproject/__init__.py", "")
    _write(root, "src/myproject/__main__.py", 'if __name__ == "__main__": pass')
    _write(root, "src/myproject/services/ingest.py", textwrap.dedent("""\
        from myproject.infrastructure.db import connect
        from myproject.domain.models import Foo

        def ingest(): pass
    """))
    _write(root, "src/myproject/infrastructure/db.py", "def connect(): pass")
    _write(root, "src/myproject/domain/models.py", "class Foo: pass")
    _write(root, "src/myproject/app.py", textwrap.dedent("""\
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/health")
        def health(): return {"ok": True}

        @app.post("/ingest")
        def do_ingest(): pass
    """))
    _write(root, "tests/__init__.py", "")
    _write(root, "tests/test_ingest.py", textwrap.dedent("""\
        from myproject.services.ingest import ingest
        def test_ingest(): pass
    """))
    _write(root, "tests/test_db.py", "def test_db(): pass")
    # A file that should be skipped by .gitignore
    _write(root, "build/out.txt", "should be skipped")
    return root


# ---------------------------------------------------------------------------
# File role classification
# ---------------------------------------------------------------------------

class TestClassifyFileRole:
    def test_entrypoint_main(self):
        assert _classify_file_role("src/myapp/__main__.py") == "entrypoint"

    def test_entrypoint_app(self):
        assert _classify_file_role("app.py") == "entrypoint"

    def test_entrypoint_bot(self):
        assert _classify_file_role("bot/bot.py") == "entrypoint"

    def test_config_toml(self):
        assert _classify_file_role("pyproject.toml") == "config"

    def test_config_env(self):
        assert _classify_file_role(".env") == "config"

    def test_config_env_example(self):
        assert _classify_file_role(".env.example") == "config"

    def test_config_claude_md(self):
        assert _classify_file_role("CLAUDE.md") == "config"

    def test_config_dockerfile(self):
        assert _classify_file_role("Dockerfile") == "config"

    def test_test_in_tests_dir(self):
        assert _classify_file_role("tests/test_foo.py") == "test"

    def test_test_prefix(self):
        assert _classify_file_role("test_bar.py") == "test"

    def test_test_suffix(self):
        assert _classify_file_role("bar_test.py") == "test"

    def test_test_ts_spec(self):
        assert _classify_file_role("components/Foo.spec.tsx") == "test"

    def test_regular_file(self):
        assert _classify_file_role("src/myapp/services/foo.py") == "file"

    def test_nested_config_not_config(self):
        # JSON in a nested dir is just a file, not config
        assert _classify_file_role("src/data/schema.json") == "file"


# ---------------------------------------------------------------------------
# Scanner integration
# ---------------------------------------------------------------------------

class TestProjectScanner:
    def test_scan_basic(self, tmp_path):
        root = _make_python_project(tmp_path)
        scanner = ProjectScanner()
        result = scanner.scan(root)

        assert result.name == "myproject"
        assert result.stack == "python"
        assert "demo project" in result.description.lower()

    def test_scan_directories(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        dir_paths = {d.rel_path for d in result.directories}
        assert "src" in dir_paths
        assert "tests" in dir_paths
        assert "src/myproject" in dir_paths

    def test_scan_skips_gitignore(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        all_paths = {f.rel_path for f in result.files}
        assert "build/out.txt" not in all_paths

    def test_scan_roles(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        roles = {f.rel_path: f.role for f in result.files}
        assert roles.get("src/myproject/__main__.py") == "entrypoint"
        assert roles.get("pyproject.toml") == "config"
        assert roles.get("tests/test_ingest.py") == "test"
        assert roles.get("src/myproject/services/ingest.py") == "file"

    def test_scan_dependencies(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        assert "requests" in result.dependencies
        assert "pydantic" in result.dependencies

    def test_scan_fingerprint_stable(self, tmp_path):
        root = _make_python_project(tmp_path)
        scanner = ProjectScanner()
        r1 = scanner.scan(root)
        r2 = scanner.scan(root)
        assert r1.scan_fingerprint == r2.scan_fingerprint

    def test_scan_fingerprint_changes(self, tmp_path):
        root = _make_python_project(tmp_path)
        scanner = ProjectScanner()
        r1 = scanner.scan(root)
        _write(root, "src/myproject/new_file.py", "x = 1")
        r2 = scanner.scan(root)
        assert r1.scan_fingerprint != r2.scan_fingerprint

    def test_scan_name_override(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root, name="custom-name")
        assert result.name == "custom-name"

    def test_scan_invalid_dir(self, tmp_path):
        with pytest.raises(ValueError, match="Not a directory"):
            ProjectScanner().scan(tmp_path / "nonexistent")


# ---------------------------------------------------------------------------
# Import parsing
# ---------------------------------------------------------------------------

class TestImportParsing:
    def test_python_from_import(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        import_pairs = {(e.source_path, e.target_path) for e in result.imports}
        # ingest.py imports from infrastructure.db and domain.models
        assert any(
            "ingest.py" in src and "db.py" in tgt
            for src, tgt in import_pairs
        )

    def test_deduplication(self, tmp_path):
        """Same import appearing twice should only produce one edge."""
        root = tmp_path / "proj"
        root.mkdir()
        _write(root, "pyproject.toml", "[project]\nname='p'\n")
        _write(root, "src/a.py", "from b import x\nfrom b import y\n")
        _write(root, "src/b.py", "x = 1\ny = 2")
        result = ProjectScanner().scan(root)
        pairs = [(e.source_path, e.target_path) for e in result.imports]
        # Should have at most one edge from a.py → b.py
        a_to_b = [(s, t) for s, t in pairs if "a.py" in s and "b.py" in t]
        assert len(a_to_b) <= 1


# ---------------------------------------------------------------------------
# Test edge detection
# ---------------------------------------------------------------------------

class TestTestEdgeDetection:
    def test_test_prefix_convention(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        test_pairs = {(e.test_path, e.source_path) for e in result.test_edges}
        # test_ingest.py → ingest.py somewhere in src
        assert any("test_ingest" in tp and "ingest.py" in sp for tp, sp in test_pairs)

    def test_test_prefix_db(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        test_pairs = {(e.test_path, e.source_path) for e in result.test_edges}
        assert any("test_db" in tp and "db.py" in sp for tp, sp in test_pairs)


# ---------------------------------------------------------------------------
# Endpoint detection
# ---------------------------------------------------------------------------

class TestEndpointDetection:
    def test_fastapi_routes(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        ep_names = {ep.name for ep in result.endpoints}
        assert "GET /health" in ep_names
        assert "POST /ingest" in ep_names

    def test_mcp_tool_detection(self, tmp_path):
        root = tmp_path / "mcp_proj"
        root.mkdir()
        _write(root, "pyproject.toml", "[project]\nname='p'\n")
        _write(root, "src/tool.py", textwrap.dedent("""\
            class MyTool(BaseTextTool):
                name = "my_tool"
                description = "A tool"
        """))
        result = ProjectScanner().scan(root)
        ep_names = {ep.name for ep in result.endpoints}
        assert "my_tool" in ep_names

    def test_endpoint_kind(self, tmp_path):
        root = _make_python_project(tmp_path)
        result = ProjectScanner().scan(root)
        http_eps = [ep for ep in result.endpoints if ep.kind == "http_route"]
        assert len(http_eps) >= 2


# ---------------------------------------------------------------------------
# Cross-project refs
# ---------------------------------------------------------------------------

class TestCrossProjectRefs:
    def test_env_port_detection(self, tmp_path):
        # Create two sibling project dirs
        projects = tmp_path / "projects"
        projects.mkdir()
        proj_a = projects / "cth.mcp.memory"
        proj_a.mkdir()
        _write(proj_a, "pyproject.toml", "[project]\nname='cth.mcp.memory'\n")
        _write(proj_a, ".env", "SCHEDULER_URL=http://localhost:8082\n")

        # Create sibling so scanner can find it
        proj_b = projects / "cth.mcp.scheduler"
        proj_b.mkdir()
        _write(proj_b, "pyproject.toml", "[project]\nname='cth.mcp.scheduler'\n")

        result = ProjectScanner().scan(proj_a)
        ref_targets = {r.target_project for r in result.cross_project_refs}
        assert "cth.mcp.scheduler" in ref_targets


# ---------------------------------------------------------------------------
# Description parsing
# ---------------------------------------------------------------------------

class TestDescriptionParsing:
    def test_first_paragraph(self):
        text = "# Title\n\nThis is the first paragraph.\n\n## Section\nMore text."
        assert _first_paragraph(text) == "This is the first paragraph."

    def test_first_paragraph_multiline(self):
        text = "# Title\n\nLine one.\nLine two.\n\n## Other"
        assert _first_paragraph(text) == "Line one. Line two."

    def test_first_paragraph_empty(self):
        assert _first_paragraph("") == ""

    def test_agent_readme_preferred(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        _write(root, "pyproject.toml", "[project]\nname='p'\n")
        _write(root, ".agent/README.md", "# Agent\n\nAgent description.\n")
        _write(root, "CLAUDE.md", "# Claude\n\nClaude description.\n")
        result = ProjectScanner().scan(root)
        assert "agent description" in result.description.lower()

    def test_utf8_description_is_not_mojibake_under_any_locale(self, tmp_path):
        """UTF-8 docs must decode as UTF-8, not as the platform's locale codec.

        Reads without an explicit ``encoding=`` fall back to
        ``locale.getpreferredencoding()`` — cp1252 on Windows — so an em-dash
        (U+2014, bytes ``E2 80 94``) silently decoded as ``a-EUR"`` and was written
        into the graph. This pins the bytes, not the platform default.
        """
        root = tmp_path / "proj"
        root.mkdir()
        _write(root, "pyproject.toml", "[project]\nname='p'\n")
        readme = root / ".agent" / "README.md"
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_bytes("# Agent\n\nRoute by task — do not preload.\n".encode("utf-8"))

        result = ProjectScanner().scan(root)

        assert "—" in result.description
        assert "â" not in result.description  # no cp1252 mojibake lead byte

    def test_bom_is_stripped_from_description(self, tmp_path):
        """A UTF-8 BOM must not leak into the stored description as U+FEFF."""
        root = tmp_path / "proj"
        root.mkdir()
        _write(root, "pyproject.toml", "[project]\nname='p'\n")
        readme = root / ".agent" / "README.md"
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_bytes(b"\xef\xbb\xbf" + b"# Agent\n\nBOM-prefixed description.\n")

        result = ProjectScanner().scan(root)

        assert "﻿" not in result.description
        assert result.description.startswith("BOM-prefixed description")


# ---------------------------------------------------------------------------
# Eligibility, capping, and coverage accounting
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEligibility:
    """The deny-list is an ordered precedence: preserve wins over extension exclusion."""

    @pytest.mark.parametrize("rel_path", [
        "CLAUDE.md",                 # config by name — must survive the .md rule
        "requirements.txt",          # parsed by _parse_dependencies — must survive .txt
        "requirements-dev.txt",
        "constraints.txt",
        "uv.lock",                   # lockfile — must survive any .lock rule
        "package-lock.json",
    ])
    def test_structural_manifests_survive_extension_exclusions(self, rel_path):
        role = _classify_file_role(rel_path)
        assert is_eligible_file(rel_path, role) is True

    @pytest.mark.parametrize("rel_path", [
        ".agent/README.md", "docs/guide.rst", "notes.txt", "server.log",
        "assets/logo.png", "data/cache.sqlite3", "dist/wheel.whl",
    ])
    def test_documentation_and_binaries_excluded(self, rel_path):
        assert is_eligible_file(rel_path, _classify_file_role(rel_path)) is False

    @pytest.mark.parametrize("rel_path", [
        "src/app/templates/index.html",   # explorer templates
        "src/app/static/site.css",
        "deploy/build.sh",
        "scripts/run.ps1",
        "db/schema.sql",                  # languages absent from _CODE_EXTENSIONS
        "ui/Widget.vue", "ui/Card.svelte", "svc/Main.cs", "lib/thing.rb", "web/index.php",
    ])
    def test_structural_non_python_files_are_eligible(self, rel_path):
        """Regression: an allowlist predicate would have dropped every one of these."""
        assert is_eligible_file(rel_path, _classify_file_role(rel_path)) is True


@pytest.mark.unit
class TestCapPriority:
    def test_source_outranks_tests_when_cap_binds(self):
        files = (
            [FileEntry(rel_path=f"src/mod{i}.py", role="file", description="") for i in range(5)]
            + [FileEntry(rel_path=f"tests/test_mod{i}.py", role="test", description="") for i in range(5)]
        )
        kept = _cap_files(list(files), 5)
        assert {f.role for f in kept} == {"file"}, "tests must not displace source"

    def test_entrypoints_survive_first(self):
        files = (
            [FileEntry(rel_path=f"src/mod{i}.py", role="file", description="") for i in range(5)]
            + [FileEntry(rel_path="main.py", role="entrypoint", description="")]
        )
        kept = _cap_files(list(files), 1)
        assert kept[0].role == "entrypoint"

    def test_no_is_code_demotion_for_unlisted_languages(self):
        """SQL/Vue/etc. previously sorted below tests via `is_code`; they must not now."""
        files = [
            FileEntry(rel_path="db/schema.sql", role="file", description=""),
            FileEntry(rel_path="tests/test_a.py", role="test", description=""),
        ]
        kept = _cap_files(list(files), 1)
        assert kept[0].rel_path == "db/schema.sql"


@pytest.mark.unit
class TestCoverageAccounting:
    def test_counts_and_partial_flag_on_full_scan(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, "README.md", "# docs\n\nExcluded by the deny-list.\n")

        result = ProjectScanner().scan(root)

        assert result.files_discovered >= result.files_eligible >= result.files_indexed
        assert result.files_indexed == len(result.files)
        assert result.partial_index is False, "small project must not report partial"

    def test_partial_index_true_when_cap_binds(self, tmp_path, monkeypatch):
        root = _make_python_project(tmp_path)
        for i in range(20):
            _write(root, f"src/myproject/gen{i}.py", "x = 1\n")
        monkeypatch.setattr(
            "menhir.infrastructure.project_scanner._MAX_KEY_FILES", 5
        )

        result = ProjectScanner().scan(root)

        assert result.files_indexed == 5
        assert result.files_eligible > 5
        assert result.partial_index is True

    def test_documentation_excluded_from_eligible_count(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, "docs/a.md", "a")
        _write(root, "docs/b.md", "b")

        result = ProjectScanner().scan(root)

        indexed = {f.rel_path for f in result.files}
        assert "docs/a.md" not in indexed and "docs/b.md" not in indexed
        assert result.files_discovered > result.files_eligible


@pytest.mark.unit
class TestScannerSchemaVersionInFingerprint:
    def test_schema_version_change_invalidates_fingerprint(self, tmp_path, monkeypatch):
        """Without this, a rules change leaves every existing graph truncated forever:
        the path+mtime hash is unchanged, so ingest skips the project as 'unchanged'."""
        root = _make_python_project(tmp_path)
        before = ProjectScanner().scan(root).scan_fingerprint

        monkeypatch.setattr(
            "menhir.infrastructure.project_scanner.SCANNER_SCHEMA_VERSION",
            SCANNER_SCHEMA_VERSION + 1,
        )
        after = ProjectScanner().scan(root).scan_fingerprint

        assert before != after

    def test_fingerprint_stable_when_nothing_changes(self, tmp_path):
        root = _make_python_project(tmp_path)
        assert ProjectScanner().scan(root).scan_fingerprint == (
            ProjectScanner().scan(root).scan_fingerprint
        )


@pytest.mark.unit
class TestGitignoreAnchoredPatterns:
    """Root-anchored patterns (`/build/`) are standard gitignore syntax.

    Only the TRAILING slash was stripped, so a leading one survived into fnmatch and could
    never match a repo-relative path — the pattern was silently a no-op. Found when adding
    `/fb_profile/` and friends to the workspace .gitignore had zero effect on the scan: git
    honored them, menhir did not.
    """

    def test_anchored_directory_pattern_is_honored(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, ".gitignore", "/junk/\n")
        _write(root, "junk/a.py", "x = 1\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "junk/a.py" not in indexed

    def test_anchored_file_pattern_with_spaces_is_honored(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, ".gitignore", "/Local State\n")
        _write(root, "Local State", "{}")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "Local State" not in indexed

    def test_anchored_pattern_does_not_match_at_depth(self, tmp_path):
        """`/Default/` must ignore the ROOT one only — anchoring is the whole point."""
        root = _make_python_project(tmp_path)
        _write(root, ".gitignore", "/Default/\n")
        _write(root, "Default/junk.py", "x = 1\n")
        _write(root, "src/myproject/Default/real.py", "def f(): pass\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "Default/junk.py" not in indexed
        assert "src/myproject/Default/real.py" in indexed

    def test_unanchored_pattern_still_matches_at_any_depth(self, tmp_path):
        """Regression guard: the anchored branch must not break normal patterns."""
        root = _make_python_project(tmp_path)
        _write(root, ".gitignore", "secrets/\n")
        _write(root, "secrets/a.py", "x = 1\n")
        _write(root, "src/myproject/secrets/b.py", "x = 1\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "secrets/a.py" not in indexed
        assert "src/myproject/secrets/b.py" not in indexed


@pytest.mark.unit
class TestNestedRepoBoundary:
    """A git repository is the unit of ingestion, so nested repos are scan boundaries."""

    def test_nested_repo_is_not_descended_into(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, "vendor/sub/.git/HEAD", "ref: refs/heads/main\n")
        _write(root, "vendor/sub/lib.py", "def f(): pass\n")
        _write(root, "vendor/plain/lib.py", "def g(): pass\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "vendor/sub/lib.py" not in indexed, "nested repo must not be absorbed"
        assert "vendor/plain/lib.py" in indexed, "plain subdir must still be scanned"

    def test_git_as_a_file_also_marks_a_boundary(self, tmp_path):
        """Worktrees and submodules use a `.git` FILE, not a directory."""
        root = _make_python_project(tmp_path)
        (root / "wt").mkdir()
        (root / "wt" / ".git").write_text("gitdir: ../.git/worktrees/wt\n")
        _write(root, "wt/lib.py", "def f(): pass\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "wt/lib.py" not in indexed

    def test_nested_repos_are_recorded_not_discarded(self, tmp_path):
        """Skipping is right for files; the containment itself is a true structural fact."""
        root = _make_python_project(tmp_path)
        _write(root, "sub-a/.git/HEAD", "ref: refs/heads/main\n")
        _write(root, "sub-a/lib.py", "def f(): pass\n")
        _write(root, "sub-b/.git/HEAD", "ref: refs/heads/main\n")

        result = ProjectScanner().scan(root)

        assert [r.name for r in result.nested_repos] == ["sub-a", "sub-b"]
        assert [r.rel_path for r in result.nested_repos] == ["sub-a", "sub-b"]
        assert not any(f.rel_path.startswith("sub-a/") for f in result.files)

    def test_no_nested_repos_recorded_for_a_plain_project(self, tmp_path):
        root = _make_python_project(tmp_path)
        assert ProjectScanner().scan(root).nested_repos == []

    def test_scanning_a_repo_directly_still_works(self, tmp_path):
        """The root's own `.git` must not make the scan boundary-out immediately."""
        root = _make_python_project(tmp_path)
        _write(root, ".git/HEAD", "ref: refs/heads/main\n")

        result = ProjectScanner().scan(root)

        assert any(f.rel_path.endswith("services/ingest.py") for f in result.files)


@pytest.mark.unit
class TestRootOnlyArtifactDirs:
    def test_root_level_artifact_dirs_are_pruned(self, tmp_path):
        root = _make_python_project(tmp_path)
        _write(root, "logs/run.py", "x = 1\n")
        _write(root, "results/out.py", "x = 1\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "logs/run.py" not in indexed
        assert "results/out.py" not in indexed

    def test_nested_package_dirs_with_generic_names_survive(self, tmp_path):
        """`_ALWAYS_SKIP_DIRS` matches by basename, so a global rule would delete these."""
        root = _make_python_project(tmp_path)
        _write(root, "src/myproject/logs/writer.py", "def w(): pass\n")
        _write(root, "src/myproject/results/model.py", "class M: pass\n")

        indexed = {f.rel_path for f in ProjectScanner().scan(root).files}

        assert "src/myproject/logs/writer.py" in indexed
        assert "src/myproject/results/model.py" in indexed
