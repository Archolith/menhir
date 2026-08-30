"""Release dependencies must use immutable, reviewed Git commits."""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OAUTH_REPOSITORY = "https://github.com/Archolith/archolith_oauth.git"
REVIEWED_OAUTH_COMMIT = "f77e2dbc7c7c85199aa05986b8d2126b54d1b056"
MCP_FRAMEWORK_REPOSITORY = "https://github.com/Archolith/archolith-mcp-framework.git"
REVIEWED_MCP_FRAMEWORK_COMMIT = "0a7c300cf50d724a2d5a8e8c1e664c7e8a5fa2eb"


def test_oauth_dependency_is_pinned_to_reviewed_merge_commit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    oauth_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.lower().startswith("archolith-oauth ")
    ]
    assert oauth_dependencies == [
        f"archolith-oauth @ git+{OAUTH_REPOSITORY}@{REVIEWED_OAUTH_COMMIT}"
    ]

    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    oauth_packages = [
        package for package in lock["package"] if package["name"] == "archolith-oauth"
    ]
    assert len(oauth_packages) == 1
    assert oauth_packages[0]["source"] == {
        "git": (
            f"{OAUTH_REPOSITORY}?rev={REVIEWED_OAUTH_COMMIT}"
            f"#{REVIEWED_OAUTH_COMMIT}"
        )
    }


def test_mcp_framework_dependency_is_pinned_to_reviewed_commit() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    framework_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.lower().startswith("archolith-mcp-framework ")
    ]
    assert framework_dependencies == [
        "archolith-mcp-framework @ "
        f"git+{MCP_FRAMEWORK_REPOSITORY}@{REVIEWED_MCP_FRAMEWORK_COMMIT}"
    ]

    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    framework_packages = [
        package
        for package in lock["package"]
        if package["name"] == "archolith-mcp-framework"
    ]
    assert len(framework_packages) == 1
    assert framework_packages[0]["source"] == {
        "git": (
            f"{MCP_FRAMEWORK_REPOSITORY}?rev={REVIEWED_MCP_FRAMEWORK_COMMIT}"
            f"#{REVIEWED_MCP_FRAMEWORK_COMMIT}"
        )
    }
