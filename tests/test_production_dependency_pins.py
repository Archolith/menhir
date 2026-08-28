"""Release dependency pins must match the reviewed OAuth authority."""

from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OAUTH_REPOSITORY = "https://github.com/Archolith/archolith_oauth.git"
REVIEWED_OAUTH_COMMIT = "0e0601b135eef213196a9a0943d02bb44f5a8c2b"


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
