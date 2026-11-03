#!/usr/bin/env python3
"""Prepare a release PR: bump Menhir's version or the Graphiti fork pin.

    python scripts/bump.py version X.Y.Z --summary "what changed" --pr
    python scripts/bump.py fork X.Y.Z.postN --commit <fork sha> --summary "what changed" --pr

Every edit must find the text it expects, the expected number of times, or nothing is written.
`uv lock --check` then runs, and a failure restores every file. uv.lock is edited directly because
`uv lock` from a different uv version rewrites unrelated lines. With --pr the result is committed
on a new branch and opened as a pull request; without it the edits stay in the working tree.

Tagging stays in scripts/release.py. Historical lines such as "since X.Y.Z" are left alone.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GH_REPO = "Archolith/menhir"
PACKAGE = "archolith-menhir"
FORK_PACKAGE = "archolith-graphiti-core"
FORK_FILE_STEM = "archolith_graphiti_core"
PYPI_FILE_HOST = "files.pythonhosted.org"

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
FORK_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.post[0-9]+)?")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
FORK_COMMIT_LINE_RE = re.compile(r"`([0-9a-f]{40})`; the locked PyPI wheel SHA-256 is")
GOVERNANCE_DATE_RE = re.compile(r"\*\*Verified against source:\*\* ([0-9]{4}-[0-9]{2}-[0-9]{2}),")
HEADING_RE = re.compile(r"^## .*$", re.MULTILINE)

#: Every line that states the current package version. Both sides are filled with the version.
VERSION_LINES: tuple[tuple[str, str], ...] = (
    ("pyproject.toml", '\nversion = "{v}"'),
    ("uv.lock", f'name = "{PACKAGE}"\nversion = "{{v}}"'),
    ("sbom.json", f'"name": "{PACKAGE}",\n      "type": "application",\n      "version": "{{v}}"'),
    ("README.md", "The current package version is `{v}`."),
    ("README.md", "These are known gaps in {v},"),
    ("docs/governance.md", "`archolith-menhir` {v} as the root component"),
    ("docs/post-install.md", "**Known limitations in {v}.**"),
)

#: Every file naming the fork version, and how many times. Excludes CHANGELOG.md, which is history,
#: and uv.lock, whose lines are rewritten individually.
FORK_VERSION_FILES: dict[str, int] = {
    ".agent/architecture.md": 2,
    ".github/workflows/release-image.yml": 1,
    "THIRD-PARTY-LICENSES.txt": 1,
    "docs/governance.md": 1,
    "docs/model-governance.md": 1,
    "docs/post-install.md": 1,
    "pyproject.toml": 1,
    "sbom.json": 5,
    "src/menhir/infrastructure/graphiti_client.py": 3,
    "src/menhir/infrastructure/graphiti_llm_adapter.py": 1,
    "tests/e2e/test_e2e_06_graphiti_fork.py": 1,
    "tests/e2e/test_e2e_08_isolation_adversarial.py": 1,
    "tests/test_graphiti_fork_contract.py": 4,
    "tests/test_production_dependency_pins.py": 1,
}
FORK_WHEEL_HASH_FILES = (".agent/architecture.md", "tests/e2e/test_e2e_06_graphiti_fork.py")


class BumpError(RuntimeError):
    pass


@dataclass(frozen=True)
class Edit:
    path: str
    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class FileRelease:
    url: str
    sha256: str
    size: int
    upload_time: str


@dataclass(frozen=True)
class ForkRelease:
    sdist: FileRelease
    wheel: FileRelease


def apply_edits(root: Path, edits: list[Edit], *, write: bool = True) -> dict[str, bytes]:
    """Apply all edits or none, keeping each file's line endings. Returns the original bytes."""
    original: dict[str, bytes] = {}
    pending: dict[str, bytes] = {}
    problems: list[str] = []
    for edit in edits:
        path = root / edit.path
        if edit.path not in pending:
            try:
                original[edit.path] = path.read_bytes()
            except OSError as error:
                raise BumpError(f"cannot read {edit.path}: {error}") from error
            pending[edit.path] = original[edit.path]
        data = pending[edit.path]
        newline = "\r\n" if b"\r\n" in data else "\n"
        old = edit.old.replace("\n", newline).encode("utf-8")
        new = edit.new.replace("\n", newline).encode("utf-8")
        found = data.count(old)
        if found != edit.count:
            problems.append(f"{edit.path}: expected {edit.count}, found {found}: {edit.old[:60]!r}")
        pending[edit.path] = data.replace(old, new)
    if problems:
        raise BumpError("nothing written:\n  " + "\n  ".join(problems))
    if write:
        for relative, data in pending.items():
            (root / relative).write_bytes(data)
    return original


def restore(root: Path, original: dict[str, bytes]) -> None:
    for relative, data in original.items():
        (root / relative).write_bytes(data)


def read_text(root: Path, relative: str) -> str:
    return (root / relative).read_bytes().decode("utf-8").replace("\r\n", "\n")


def _project(root: Path) -> dict:
    return tomllib.loads(read_text(root, "pyproject.toml"))["project"]


def current_version(root: Path) -> str:
    return _project(root)["version"]


def current_fork_version(root: Path) -> str:
    prefix = f"{FORK_PACKAGE}=="
    pins = [dep[len(prefix):] for dep in _project(root)["dependencies"] if dep.startswith(prefix)]
    if len(pins) != 1:
        raise BumpError(f"expected one exact {FORK_PACKAGE} pin in pyproject.toml, found {len(pins)}")
    return pins[0]


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"[0-9]+", version))


def _require_newer(current: str, new: str) -> None:
    if _version_key(new) <= _version_key(current):
        raise BumpError(f"{new} is not newer than the current {current}")


def _changelog_edit(root: Path, entry: str) -> Edit:
    first = HEADING_RE.search(read_text(root, "CHANGELOG.md"))
    if first is None:
        raise BumpError("CHANGELOG.md has no dated heading")
    return Edit("CHANGELOG.md", first.group(0), f"{entry}\n{first.group(0)}")


def version_edits(root: Path, new: str, *, summary: str, date: str) -> list[Edit]:
    if VERSION_RE.fullmatch(new) is None:
        raise BumpError(f"version must be X.Y.Z, got {new!r}")
    current = current_version(root)
    _require_newer(current, new)
    edits = [
        Edit(path, line.format(v=current), line.format(v=new)) for path, line in VERSION_LINES
    ]
    verified = GOVERNANCE_DATE_RE.search(read_text(root, "docs/model-governance.md"))
    if verified is None:
        raise BumpError("docs/model-governance.md has no verified-against-source line")
    edits.append(Edit(
        "docs/model-governance.md",
        f"{verified.group(1)}, package version `{current}`",
        f"{date}, package version `{new}`",
    ))
    files = list(dict.fromkeys(edit.path for edit in edits))
    listed = ", ".join(f"`{path}`" for path in files)
    edits.append(_changelog_edit(root, (
        f"## {date} - v{new}: {summary}\n\n- {listed}: version {new}. No dependency changes in "
        "this commit.\n"
    )))
    return edits


def _lock_line(pattern: str, lock: str, label: str) -> str:
    matches = re.findall(pattern, lock, re.MULTILINE)
    if len(matches) != 1:
        raise BumpError(f"uv.lock: expected one {label} line, found {len(matches)}")
    return matches[0]


def _lock_entry(file: FileRelease) -> str:
    return (
        f'{{ url = "{file.url}", hash = "sha256:{file.sha256}", size = {file.size}, '
        f'upload-time = "{file.upload_time}" }}'
    )


def fork_edits(
    root: Path, new: str, *, commit: str, release: ForkRelease, summary: str, date: str,
) -> list[Edit]:
    if FORK_VERSION_RE.fullmatch(new) is None:
        raise BumpError(f"fork version must be X.Y.Z or X.Y.Z.postN, got {new!r}")
    if COMMIT_RE.fullmatch(commit) is None:
        raise BumpError("fork commit must be a full 40-character SHA")
    old = current_fork_version(root)
    _require_newer(old, new)

    lock = read_text(root, "uv.lock")
    stem = re.escape(f"{FORK_FILE_STEM}-{old}")
    sdist_line = _lock_line(rf'^sdist = \{{ url = "[^"]*{stem}\.tar\.gz".*\}}$', lock, "sdist")
    wheel_entry = _lock_line(rf'(\{{ url = "[^"]*{stem}-py3-none-any\.whl".*?\}})', lock, "wheel")
    old_hash = re.search(r'hash = "sha256:([0-9a-f]{64})"', wheel_entry)
    if old_hash is None:
        raise BumpError("uv.lock: the fork wheel line has no sha256")
    commits = FORK_COMMIT_LINE_RE.findall(read_text(root, ".agent/architecture.md"))
    if len(commits) != 1:
        raise BumpError(f".agent/architecture.md: expected one fork commit, found {len(commits)}")

    edits = [
        Edit(path, old, new, count) for path, count in sorted(FORK_VERSION_FILES.items())
    ]
    edits.append(Edit(".agent/architecture.md", commits[0], commit))
    edits.extend(
        Edit(path, old_hash.group(1), release.wheel.sha256) for path in FORK_WHEEL_HASH_FILES
    )
    edits.extend([
        Edit("uv.lock", f'name = "{FORK_PACKAGE}"\nversion = "{old}"',
             f'name = "{FORK_PACKAGE}"\nversion = "{new}"'),
        Edit("uv.lock", sdist_line, f"sdist = {_lock_entry(release.sdist)}"),
        Edit("uv.lock", wheel_entry, _lock_entry(release.wheel)),
        Edit("uv.lock", f'{{ name = "{FORK_PACKAGE}", specifier = "=={old}" }}',
             f'{{ name = "{FORK_PACKAGE}", specifier = "=={new}" }}'),
    ])
    edits.append(_changelog_edit(root, (
        f"## {date} - Graphiti fork {new}: {summary}\n\n- `pyproject.toml`, `uv.lock`: "
        f"`{FORK_PACKAGE}=={new}`, fork commit `{commit[:8]}`. Pins, docs and release checks "
        "updated.\n"
    )))
    return edits


def _upload_time(value: str) -> str:
    # uv.lock stores milliseconds; PyPI reports microseconds.
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def _file_release(entry: dict) -> FileRelease:
    url = entry["url"]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != PYPI_FILE_HOST:
        raise BumpError(f"unexpected download location: {url}")
    return FileRelease(
        url=url,
        sha256=entry["digests"]["sha256"],
        size=int(entry["size"]),
        upload_time=_upload_time(entry["upload_time_iso_8601"]),
    )


def parse_fork_release(version: str, payload: dict) -> ForkRelease:
    by_name = {entry["filename"]: entry for entry in payload.get("urls", [])}
    sdist_name = f"{FORK_FILE_STEM}-{version}.tar.gz"
    wheel_name = f"{FORK_FILE_STEM}-{version}-py3-none-any.whl"
    if set(by_name) != {sdist_name, wheel_name}:
        raise BumpError(f"PyPI files for {version} are not one sdist and one wheel: {sorted(by_name)}")
    return ForkRelease(_file_release(by_name[sdist_name]), _file_release(by_name[wheel_name]))


def fetch_fork_release(version: str) -> ForkRelease:
    url = f"https://pypi.org/pypi/{FORK_PACKAGE}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return parse_fork_release(version, json.load(response))
    except urllib.error.HTTPError as error:
        raise BumpError(f"PyPI lookup of {FORK_PACKAGE} {version} returned {error.code}") from error
    except urllib.error.URLError as error:
        raise BumpError(f"PyPI lookup of {FORK_PACKAGE} {version} failed: {error}") from error


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)


def _checked(run: Runner, command: list[str]) -> str:
    result = run(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BumpError(f"{' '.join(command)} failed: {detail}")
    return result.stdout.strip()


def require_clean_main(run: Runner) -> None:
    if _checked(run, ["git", "status", "--porcelain"]):
        raise BumpError("the working tree is not clean")
    if _checked(run, ["git", "rev-parse", "--abbrev-ref", "HEAD"]) != "main":
        raise BumpError("--pr must start from main")
    _checked(run, ["git", "fetch", "origin", "main"])
    if _checked(run, ["git", "rev-parse", "HEAD"]) != _checked(
        run, ["git", "rev-parse", "origin/main"]
    ):
        raise BumpError("local main is not at origin/main")


def open_pr(run: Runner, *, branch: str, title: str, body: str, files: list[str]) -> str:
    _checked(run, ["git", "checkout", "-b", branch])
    _checked(run, ["git", "add", "--", *files])
    _checked(run, ["git", "commit", "-m", title])
    _checked(run, ["git", "push", "-u", "origin", branch])
    return _checked(run, [
        "gh", "pr", "create", "--repo", GH_REPO, "--base", "main", "--head", branch,
        "--title", title, "--body", body,
    ])


def finish(
    root: Path, edits: list[Edit], *, run: Runner, pr: bool, branch: str, title: str, body: str,
) -> None:
    original = apply_edits(root, edits)
    check = run(["uv", "lock", "--check"])
    if check.returncode != 0:
        restore(root, original)
        detail = (check.stderr or check.stdout).strip()
        raise BumpError(f"uv lock --check failed; every file was restored: {detail}")
    print(f"updated {len(original)} files")
    if pr:
        print(open_pr(run, branch=branch, title=title, body=body, files=sorted(original)))


def main(argv: list[str] | None = None, *, run: Runner = run_command) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="bump the package version")
    fork = commands.add_parser("fork", help="bump the Graphiti fork pin")
    fork.add_argument("--commit", required=True, help="the fork commit the release was built from")
    for command in (version, fork):
        command.add_argument("new", metavar="VERSION")
        command.add_argument("--summary", required=True, help="the CHANGELOG heading's description")
        command.add_argument("--pr", action="store_true", help="commit, push and open a PR")
        command.add_argument("--date", help="YYYY-MM-DD; defaults to today in UTC")
    args = parser.parse_args(argv)
    date = args.date or datetime.now(UTC).strftime("%Y-%m-%d")

    try:
        if args.pr:
            require_clean_main(run)
        if args.command == "version":
            edits = version_edits(REPO, args.new, summary=args.summary, date=date)
            branch, title = f"chore/release-{args.new}", f"chore: release {args.new}"
            body = f"Version bump for {args.new}: {args.summary}"
        else:
            release = fetch_fork_release(args.new)
            edits = fork_edits(
                REPO, args.new, commit=args.commit, release=release, summary=args.summary,
                date=date,
            )
            branch, title = f"chore/fork-{args.new}", f"chore: Graphiti fork {args.new}"
            body = f"Fork pin bump to {args.new}: {args.summary}"
        finish(REPO, edits, run=run, pr=args.pr, branch=branch, title=title, body=body)
    except BumpError as error:
        print(f"bump: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
