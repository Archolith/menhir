from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "maintenance"
    / "graphiti_release_monitor.py"
)
SPEC = importlib.util.spec_from_file_location("graphiti_release_monitor", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def policy() -> dict:
    return {
        "schema": 1,
        "dependency": "graphiti-core",
        "minor_or_major_is_material": True,
        "categories": [
            {
                "id": "dedup",
                "title": "Deduplication",
                "terms": ["dedup", "resolve"],
                "menhir_paths": ["src/patch.py"],
                "test_focus": ["tests/test_patch.py"],
            }
        ],
        "baseline_validation": ["pytest tests/test_patch.py -q"],
    }


def release(version: str, body: str = "", *, prerelease: bool = False) -> dict:
    return {
        "tag_name": f"v{version}",
        "name": f"Graphiti {version}",
        "body": body,
        "html_url": f"https://github.com/getzep/graphiti/releases/tag/v{version}",
        "published_at": "2026-09-08T12:00:00Z",
        "draft": False,
        "prerelease": prerelease,
    }


def test_reads_the_exact_pyproject_pin(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["graphiti-core==0.29.3"]\n', encoding="utf-8"
    )

    assert str(monitor.read_exact_pin(pyproject, "graphiti-core")) == "0.29.3"


def test_rejects_a_range_instead_of_guessing_the_pin(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["graphiti-core>=0.29.3"]\n', encoding="utf-8"
    )

    with pytest.raises(monitor.MonitorError, match="exact"):
        monitor.read_exact_pin(pyproject, "graphiti-core")


def test_stays_quiet_when_no_newer_stable_release_exists() -> None:
    mcp_release = release("1.0.0")
    mcp_release["tag_name"] = "mcp-v1.0.0"
    releases = monitor.parse_releases(
        [release("0.29.3"), release("0.30.0", prerelease=True), mcp_release]
    )

    report = monitor.assess_releases(
        monitor.Version.parse("0.29.3"), releases, policy()
    )

    assert report["actionable"] is False
    assert report["target_version"] is None
    assert report["issue"] is None


def test_stays_quiet_for_an_unmatched_patch_release() -> None:
    releases = monitor.parse_releases(
        [release("0.29.4", "Correct a documentation typo.")]
    )

    report = monitor.assess_releases(
        monitor.Version.parse("0.29.3"), releases, policy()
    )

    assert report["actionable"] is False
    assert report["target_version"] == "0.29.4"
    assert report["issue"] is None


def test_patch_release_matching_coupling_opens_a_plan_for_latest_stable() -> None:
    releases = monitor.parse_releases(
        [
            release("0.29.4", "Improve node dedup and resolve behavior."),
            release("0.29.5", "Documentation cleanup."),
        ]
    )

    report = monitor.assess_releases(
        monitor.Version.parse("0.29.3"), releases, policy()
    )

    assert report["actionable"] is True
    assert report["target_version"] == "0.29.5"
    assert report["categories"][0]["matched_terms"] == ["dedup", "resolve"]
    assert report["issue"]["title"] == "Graphiti upgrade plan: 0.29.5"
    assert "do not change the pin automatically" in report["issue"]["body"]


def test_minor_release_is_material_even_with_sparse_notes() -> None:
    releases = monitor.parse_releases([release("0.30.0", "Maintenance release.")])

    report = monitor.assess_releases(
        monitor.Version.parse("0.29.3"), releases, policy()
    )

    assert report["actionable"] is True
    assert report["categories"] == []
    assert "release line" in report["reasons"][0]
    assert "known Menhir compatibility lane" in report["issue"]["body"]


def test_main_writes_stable_report_and_action_outputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / ".github").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["graphiti-core==0.29.3"]\n', encoding="utf-8"
    )
    (root / ".github" / "graphiti-release-monitor.json").write_text(
        json.dumps(policy()), encoding="utf-8"
    )
    releases_path = tmp_path / "releases.json"
    releases_path.write_text(
        json.dumps([release("0.29.4", "Dedup fix.")]), encoding="utf-8"
    )
    output = tmp_path / "report.json"
    github_output = tmp_path / "github-output.txt"

    result = monitor.main(
        [
            "--repository-root",
            str(root),
            "--releases-file",
            str(releases_path),
            "--output",
            str(output),
            "--github-output",
            str(github_output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["actionable"] is True
    assert "actionable=true" in github_output.read_text(encoding="utf-8")


def test_repository_policy_points_to_existing_paths() -> None:
    root = Path(__file__).parents[1]
    repository_policy = monitor.load_policy(
        root / ".github" / "graphiti-release-monitor.json"
    )

    for category in repository_policy["categories"]:
        for relative in [*category["menhir_paths"], *category["test_focus"]]:
            matches = list(root.glob(relative))
            assert matches, f"Graphiti monitor policy path does not resolve: {relative}"
