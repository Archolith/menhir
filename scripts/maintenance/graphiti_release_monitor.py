#!/usr/bin/env python3
"""Assess Graphiti releases against Menhir's exact pin and emit an upgrade-plan issue.

The script is read-only. It fetches public GitHub release metadata (or a supplied JSON fixture),
applies the repository-owned materiality policy, and writes a report for the GitHub Actions
workflow that owns issue creation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tomllib
from typing import Any
import urllib.request


DEFAULT_POLICY = Path(".github/graphiti-release-monitor.json")
DEFAULT_RELEASES_URL = (
    "https://api.github.com/repos/getzep/graphiti/releases?per_page=100"
)
_VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_CORE_RELEASE_TAG_RE = re.compile(r"v?\d+\.\d+\.\d+")


class MonitorError(ValueError):
    """Raised when monitor input cannot be assessed safely."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> Version:
        match = _VERSION_RE.search(value)
        if match is None:
            raise MonitorError(f"cannot parse a stable x.y.z version from {value!r}")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Release:
    version: Version
    tag_name: str
    name: str
    body: str
    html_url: str
    published_at: str
    draft: bool
    prerelease: bool


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot load monitor policy from {path}: {exc}") from exc
    if policy.get("schema") != 1 or not policy.get("categories"):
        raise MonitorError("monitor policy must use schema 1 and define categories")
    return policy


def read_exact_pin(pyproject_path: Path, dependency: str) -> Version:
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise MonitorError(
            f"cannot read project dependencies from {pyproject_path}: {exc}"
        ) from exc

    requirement_re = re.compile(rf"^{re.escape(dependency)}==([^\s;]+)$", re.IGNORECASE)
    matching = [
        match
        for item in project.get("dependencies", [])
        if (match := requirement_re.match(item.strip()))
    ]
    if len(matching) != 1:
        raise MonitorError(
            f"expected one exact {dependency}==x.y.z pin in {pyproject_path}"
        )
    return Version.parse(matching[0].group(1))


def _release_from_json(value: dict[str, Any]) -> Release | None:
    if value.get("draft") or value.get("prerelease"):
        return None
    tag_name = str(value.get("tag_name") or "")
    # GetZep/graphiti also publishes the separately-versioned MCP server from this repository.
    # Only plain x.y.z / vx.y.z tags represent graphiti-core releases.
    if _CORE_RELEASE_TAG_RE.fullmatch(tag_name) is None:
        return None
    try:
        version = Version.parse(tag_name)
    except MonitorError:
        return None
    return Release(
        version=version,
        tag_name=tag_name,
        name=str(value.get("name") or tag_name),
        body=str(value.get("body") or ""),
        html_url=str(value.get("html_url") or ""),
        published_at=str(value.get("published_at") or ""),
        draft=False,
        prerelease=False,
    )


def parse_releases(values: list[dict[str, Any]]) -> list[Release]:
    releases = [
        release
        for value in values
        if (release := _release_from_json(value)) is not None
    ]
    return sorted(releases, key=lambda release: release.version)


def fetch_releases(url: str, token: str | None = None) -> list[Release]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "menhir-graphiti-release-monitor",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise MonitorError(f"failed to fetch Graphiti releases: {exc}") from exc
    if not isinstance(payload, list):
        raise MonitorError("Graphiti releases response was not a JSON list")
    return parse_releases(payload)


def read_releases_fixture(path: Path) -> list[Release]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot load releases fixture from {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise MonitorError("releases fixture must contain a JSON list")
    return parse_releases(payload)


def _contains_term(text: str, term: str) -> bool:
    escaped = re.escape(term).replace(r"\ ", r"\s+")
    return re.search(rf"(?<!\w){escaped}(?!\w)", text, flags=re.IGNORECASE) is not None


def _matched_categories(
    releases: list[Release], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    text = "\n".join(f"{release.name}\n{release.body}" for release in releases)
    matches: list[dict[str, Any]] = []
    for category in policy["categories"]:
        terms = sorted(
            {term for term in category["terms"] if _contains_term(text, term)},
            key=str.casefold,
        )
        if terms:
            matches.append(
                {
                    "id": category["id"],
                    "title": category["title"],
                    "matched_terms": terms,
                    "menhir_paths": category["menhir_paths"],
                    "test_focus": category["test_focus"],
                }
            )
    return matches


def _release_line_changed(current: Version, target: Version) -> bool:
    return (current.major, current.minor) != (target.major, target.minor)


def assess_releases(
    current: Version, releases: list[Release], policy: dict[str, Any]
) -> dict[str, Any]:
    newer = [release for release in releases if release.version > current]
    if not newer:
        return {
            "schema": 1,
            "dependency": policy["dependency"],
            "current_pin": str(current),
            "target_version": None,
            "actionable": False,
            "reasons": ["No newer stable Graphiti release was found."],
            "categories": [],
            "releases": [],
            "issue": None,
        }

    target = max(newer, key=lambda release: release.version)
    considered = [release for release in newer if release.version <= target.version]
    categories = _matched_categories(considered, policy)
    line_change = bool(
        policy.get("minor_or_major_is_material")
    ) and _release_line_changed(current, target.version)
    actionable = line_change or bool(categories)
    reasons: list[str] = []
    if line_change:
        reasons.append(
            f"The target crosses Graphiti's {current.major}.{current.minor} to "
            f"{target.version.major}.{target.version.minor} release line."
        )
    if categories:
        reasons.append(
            "Release notes match Menhir coupling categories: "
            + ", ".join(item["title"] for item in categories)
            + "."
        )
    if not actionable:
        reasons.append("Only patch releases with no Menhir coupling signal were found.")

    report: dict[str, Any] = {
        "schema": 1,
        "dependency": policy["dependency"],
        "current_pin": str(current),
        "target_version": str(target.version),
        "actionable": actionable,
        "reasons": reasons,
        "categories": categories,
        "releases": [
            {
                "version": str(release.version),
                "name": release.name,
                "tag_name": release.tag_name,
                "html_url": release.html_url,
                "published_at": release.published_at,
            }
            for release in considered
        ],
        "issue": None,
    }
    if actionable:
        report["issue"] = build_issue(report, policy)
    return report


def _markdown_list(values: list[str]) -> str:
    return "\n".join(f"- `{value}`" for value in values)


def build_issue(report: dict[str, Any], policy: dict[str, Any]) -> dict[str, str]:
    target = report["target_version"]
    marker = f"<!-- graphiti-release-monitor:target={target} -->"
    release_lines = []
    for release in report["releases"]:
        date = release["published_at"][:10] or "date unavailable"
        label = f"{release['name']} ({date})"
        release_lines.append(f"- [{label}]({release['html_url']})")

    matched_by_id = {category["id"]: category for category in report["categories"]}
    category_sections = []
    for configured in policy["categories"]:
        matched = matched_by_id.get(configured["id"])
        signal = (
            "Release-note signals: "
            + ", ".join(f"`{term}`" for term in matched["matched_terms"])
            + "."
            if matched
            else "Release-note signals: none; retained because this is a known Menhir compatibility lane."
        )
        category_sections.append(
            f"### {configured['title']}\n\n"
            f"{signal}\n\n"
            "Inspect:\n\n"
            f"{_markdown_list(configured['menhir_paths'])}\n\n"
            "Likely test focus:\n\n"
            f"{_markdown_list(configured['test_focus'])}"
        )

    body = f"""{marker}
# Graphiti {target} upgrade plan

## Decision seed

- Current exact Menhir pin: `{policy["dependency"]}=={report["current_pin"]}`
- Recommended review target: `{policy["dependency"]}=={target}`
- Disposition: **review and plan; do not change the pin automatically**

{chr(10).join(f"- {reason}" for reason in report["reasons"])}

## Upstream releases considered

{chr(10).join(release_lines)}

## Menhir compatibility audit

{(chr(10) * 2).join(category_sections)}

Always inventory every local Graphiti monkey patch and private import before deciding which patches can be
removed. A matching upstream fix is evidence for a regression test and patch-retirement review, not permission
to delete the local behavior immediately.

## Upgrade work plan

1. Compare the pinned and target Graphiti source for every patched/imported symbol used by Menhir.
2. Classify each local patch as still required, upstreamed, conflicting, or needing adaptation.
3. Review entity, edge, episode, dedup/resolution, Neo4j/query, retrieval, prompt, and provider behavior affected
   by the matched categories above.
4. Update the exact `pyproject.toml` pin and generated dependency artifacts only in a dedicated review branch.
5. Run the focused tests first, then the full offline suite, then opted-in throwaway-Neo4j integration tests.
6. Record compatibility findings, migration risk, expected quality/cost benefit, and rollback before approval.
7. Merge and deploy only through Menhir's normal human-reviewed release process.

## Baseline validation

{_markdown_list(policy["baseline_validation"])}

## Guardrails

- This issue was generated from public release metadata and repository policy only.
- The monitor does not access production, change the pin, open an upgrade PR, merge, or deploy.
- Closing this issue records the owner's disposition for this target; the monitor will not reopen it.
"""
    return {"title": f"Graphiti upgrade plan: {target}", "marker": marker, "body": body}


def write_github_output(path: Path, report: dict[str, Any]) -> None:
    values = {
        "actionable": str(report["actionable"]).lower(),
        "current_pin": report["current_pin"],
        "target_version": report["target_version"] or "",
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def append_summary(path: Path, report: dict[str, Any]) -> None:
    status = "actionable" if report["actionable"] else "quiet"
    target = report["target_version"] or "none"
    lines = [
        "## Graphiti release monitor",
        "",
        f"- Result: **{status}**",
        f"- Current pin: `{report['current_pin']}`",
        f"- Latest stable target: `{target}`",
        "",
        *[f"- {reason}" for reason in report["reasons"]],
        "",
    ]
    with path.open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--releases-file", type=Path)
    parser.add_argument("--releases-url", default=DEFAULT_RELEASES_URL)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repository_root.resolve()
    policy_path = args.policy or root / DEFAULT_POLICY
    try:
        policy = load_policy(policy_path)
        current = read_exact_pin(root / "pyproject.toml", policy["dependency"])
        releases = (
            read_releases_fixture(args.releases_file)
            if args.releases_file
            else fetch_releases(args.releases_url, os.environ.get("GITHUB_TOKEN"))
        )
        report = assess_releases(current, releases, policy)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if args.github_output:
            write_github_output(args.github_output, report)
        if args.github_summary:
            append_summary(args.github_summary, report)
    except MonitorError as exc:
        print(f"Graphiti release monitor failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
