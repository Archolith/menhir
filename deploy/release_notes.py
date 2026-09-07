#!/usr/bin/env python3
"""Validate and render strict Menhir release-note fragments."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Sequence


SCHEMA = 1
CATEGORIES = ("added", "changed", "fixed", "security", "operations")
DEPLOYMENT_CLASSES = ("app-only", "security-config", "maintenance")
REPOSITORIES = ("menhir", "archolith_oauth", "yawn_deploy", "yawn_vps")
SECURITY_SCOPES = (
    "authentication-and-oauth-authority",
    "authorization-and-client-tool-policy",
    "backup-restore-and-rollback",
    "host-privilege-and-command-wrappers",
    "network-and-ingress-boundaries",
    "runtime-hardening-and-observability",
    "secret-handling",
    "supply-chain-and-build-evidence",
)

FRAGMENT_KEYS = frozenset({
    "schema",
    "id",
    "category",
    "deployment_class",
    "summary",
    "details",
    "operator_impact",
    "repositories",
    "security_scopes",
    "breaking",
})
MAX_FRAGMENT_BYTES = 64 * 1024
MAX_ID_LENGTH = 64
MAX_SUMMARY_LENGTH = 160
MAX_DETAILS_LENGTH = 4000
MAX_OPERATOR_IMPACT_LENGTH = 1000
MAX_COMMITS_PER_REPOSITORY = 100
MAX_SECURITY_SCOPES = len(SECURITY_SCOPES)

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^menhir-prod-[0-9]+\.[0-9]+\.[0-9]+-[0-9]+$")
_CATEGORY_ORDER = {value: index for index, value in enumerate(CATEGORIES)}


class ReleaseNoteError(ValueError):
    """Raised when a release-note fragment or output path is invalid."""


@dataclass(frozen=True, slots=True)
class ReleaseNoteFragment:
    """A validated release-note fragment."""

    schema: int
    id: str
    category: str
    deployment_class: str
    summary: str
    details: str
    operator_impact: str
    repositories: Mapping[str, tuple[str, ...]]
    security_scopes: tuple[str, ...]
    breaking: bool


def _fail(message: str) -> NoReturn:
    raise ReleaseNoteError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> NoReturn:
    _fail(f"non-finite JSON number is not allowed: {value}")


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseNoteError(f"cannot open fragment {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail(f"fragment is not a regular file: {path}")
        if metadata.st_size > MAX_FRAGMENT_BYTES:
            _fail(f"fragment exceeds {MAX_FRAGMENT_BYTES} bytes: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_FRAGMENT_BYTES + 1)
        if len(data) > MAX_FRAGMENT_BYTES:
            _fail(f"fragment exceeds {MAX_FRAGMENT_BYTES} bytes: {path}")
        return data
    finally:
        os.close(descriptor)


def _text(
    value: Any,
    field: str,
    maximum: int,
    *,
    single_line: bool = False,
) -> str:
    if not isinstance(value, str):
        _fail(f"{field} must be a string")
    if not value or not value.strip():
        _fail(f"{field} must not be empty")
    if value != value.strip():
        _fail(f"{field} must not have leading or trailing whitespace")
    if len(value) > maximum:
        _fail(f"{field} exceeds {maximum} characters")
    forbidden_controls = {chr(code) for code in range(32)} - ({"\n"} if not single_line else set())
    if any(character in value for character in forbidden_controls) or "\x7f" in value:
        _fail(f"{field} contains forbidden control characters")
    return value


def _repositories(value: Any) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, dict) or not value:
        _fail("repositories must be a nonempty object")
    unknown = set(value) - set(REPOSITORIES)
    if unknown:
        _fail(f"repositories contains unknown names: {', '.join(sorted(unknown))}")

    result: dict[str, tuple[str, ...]] = {}
    all_commits: set[str] = set()
    for name in REPOSITORIES:
        if name not in value:
            continue
        commits = value[name]
        if not isinstance(commits, list) or not commits:
            _fail(f"repositories.{name} must be a nonempty list")
        if len(commits) > MAX_COMMITS_PER_REPOSITORY:
            _fail(
                f"repositories.{name} exceeds {MAX_COMMITS_PER_REPOSITORY} commits"
            )
        normalized: list[str] = []
        for commit in commits:
            if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
                _fail(f"repositories.{name} contains an invalid commit")
            if commit in all_commits:
                _fail(f"duplicate commit: {commit}")
            all_commits.add(commit)
            normalized.append(commit)
        result[name] = tuple(normalized)
    return MappingProxyType(result)


def _security_scopes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _fail("security_scopes must be a nonempty list")
    if len(value) > MAX_SECURITY_SCOPES:
        _fail(f"security_scopes exceeds {MAX_SECURITY_SCOPES} entries")
    if any(not isinstance(item, str) for item in value):
        _fail("security_scopes entries must be strings")
    unknown = set(value) - set(SECURITY_SCOPES)
    if unknown:
        _fail(f"unknown security scope: {', '.join(sorted(unknown))}")
    if len(value) != len(set(value)):
        _fail("security_scopes must not contain duplicates")
    if value != sorted(value):
        _fail("security_scopes must be sorted")
    return tuple(value)


def load_fragment(path: str | Path) -> ReleaseNoteFragment:
    """Load and strictly validate one JSON release-note fragment."""

    fragment_path = Path(path)
    if fragment_path.is_symlink():
        _fail(f"fragment must not be a symlink: {fragment_path}")
    data = _read_regular_file(fragment_path)
    if data.startswith(b"\xef\xbb\xbf"):
        _fail(f"fragment must be UTF-8 without a BOM: {fragment_path}")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseNoteError(f"fragment is not UTF-8: {fragment_path}") from exc
    try:
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ReleaseNoteError(f"malformed JSON in {fragment_path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        _fail("fragment root must be an object")
    actual_keys = set(value)
    if actual_keys != FRAGMENT_KEYS:
        missing = sorted(FRAGMENT_KEYS - actual_keys)
        unknown = sorted(actual_keys - FRAGMENT_KEYS)
        parts = []
        if missing:
            parts.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown keys: {', '.join(unknown)}")
        _fail("; ".join(parts))
    if type(value["schema"]) is not int or value["schema"] != SCHEMA:
        _fail(f"schema must be the integer {SCHEMA}")

    fragment_id = _text(value["id"], "id", MAX_ID_LENGTH, single_line=True)
    if not _SLUG_RE.fullmatch(fragment_id):
        _fail("id must be a safe lowercase slug")
    category = value["category"]
    if not isinstance(category, str) or category not in CATEGORIES:
        _fail(f"category must be one of: {', '.join(CATEGORIES)}")
    deployment_class = value["deployment_class"]
    if (
        not isinstance(deployment_class, str)
        or deployment_class not in DEPLOYMENT_CLASSES
    ):
        _fail(
            "deployment_class must be one of: " + ", ".join(DEPLOYMENT_CLASSES)
        )
    if type(value["breaking"]) is not bool:
        _fail("breaking must be a boolean")

    return ReleaseNoteFragment(
        schema=SCHEMA,
        id=fragment_id,
        category=category,
        deployment_class=deployment_class,
        summary=_text(
            value["summary"], "summary", MAX_SUMMARY_LENGTH, single_line=True
        ),
        details=_text(value["details"], "details", MAX_DETAILS_LENGTH),
        operator_impact=_text(
            value["operator_impact"],
            "operator_impact",
            MAX_OPERATOR_IMPACT_LENGTH,
            single_line=True,
        ),
        repositories=_repositories(value["repositories"]),
        security_scopes=_security_scopes(value["security_scopes"]),
        breaking=value["breaking"],
    )


def collect_fragments(directory: str | Path) -> tuple[ReleaseNoteFragment, ...]:
    """Load every fragment in a directory and return deterministic order."""

    fragment_directory = Path(directory)
    if fragment_directory.is_symlink():
        _fail(f"fragment directory must not be a symlink: {fragment_directory}")
    try:
        entries = list(os.scandir(fragment_directory))
    except OSError as exc:
        raise ReleaseNoteError(
            f"cannot read fragment directory {fragment_directory}: {exc}"
        ) from exc
    if not entries:
        _fail(f"fragment directory is empty: {fragment_directory}")

    fragments: list[ReleaseNoteFragment] = []
    for entry in sorted(entries, key=lambda item: item.name):
        if entry.is_symlink():
            _fail(f"fragment directory contains a symlink: {entry.name}")
        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".json"):
            _fail(f"unknown entry in fragment directory: {entry.name}")
        fragments.append(load_fragment(Path(entry.path)))

    ids: set[str] = set()
    for fragment in fragments:
        if fragment.id in ids:
            _fail(f"duplicate fragment id: {fragment.id}")
        ids.add(fragment.id)
    return tuple(sorted(fragments, key=_fragment_sort_key))


def _fragment_sort_key(fragment: ReleaseNoteFragment) -> tuple[int, str]:
    return (_CATEGORY_ORDER[fragment.category], fragment.id)


def _ordered_fragments(
    fragments: Sequence[ReleaseNoteFragment],
) -> tuple[ReleaseNoteFragment, ...]:
    values = tuple(fragments)
    if any(not isinstance(value, ReleaseNoteFragment) for value in values):
        _fail("render input must contain only ReleaseNoteFragment values")
    ids = [value.id for value in values]
    if len(ids) != len(set(ids)):
        _fail("render input contains duplicate fragment ids")
    return tuple(sorted(values, key=_fragment_sort_key))


def _fragment_dict(fragment: ReleaseNoteFragment) -> dict[str, Any]:
    return {
        "schema": fragment.schema,
        "id": fragment.id,
        "category": fragment.category,
        "deployment_class": fragment.deployment_class,
        "summary": fragment.summary,
        "details": fragment.details,
        "operator_impact": fragment.operator_impact,
        "repositories": {
            name: list(fragment.repositories[name])
            for name in REPOSITORIES
            if name in fragment.repositories
        },
        "security_scopes": list(fragment.security_scopes),
        "breaking": fragment.breaking,
    }


def _release_id(value: str | None) -> str | None:
    if value is not None and not _RELEASE_ID_RE.fullmatch(value):
        _fail("release_id must match menhir-prod-<major>.<minor>.<patch>-<sequence>")
    return value


def render_markdown(
    fragments: Sequence[ReleaseNoteFragment],
    release_id: str | None = None,
) -> str:
    """Render fragments as deterministic Markdown."""

    ordered = _ordered_fragments(fragments)
    heading = f"# {release_id}" if _release_id(release_id) else "# Release notes"
    lines = [heading, ""]
    for category in CATEGORIES:
        category_fragments = [item for item in ordered if item.category == category]
        if not category_fragments:
            continue
        lines.extend((f"## {category.title()}", ""))
        for fragment in category_fragments:
            lines.extend(
                (
                    f"### {fragment.summary}",
                    "",
                    f"- Fragment: `{fragment.id}`",
                    f"- Deployment class: `{fragment.deployment_class}`",
                    f"- Breaking: `{'true' if fragment.breaking else 'false'}`",
                    "- Repositories:",
                )
            )
            for name in REPOSITORIES:
                for commit in fragment.repositories.get(name, ()):
                    lines.append(f"  - `{name}`: `{commit}`")
            lines.append("- Security scopes:")
            for scope in fragment.security_scopes:
                lines.append(f"  - `{scope}`")
            lines.extend(
                (
                    "",
                    fragment.details,
                    "",
                    f"**Operator impact:** {fragment.operator_impact}",
                    "",
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def render_json(
    fragments: Sequence[ReleaseNoteFragment],
    release_id: str | None = None,
) -> str:
    """Render fragments as deterministic, canonical aggregate JSON."""

    value = {
        "schema": SCHEMA,
        "release_id": _release_id(release_id),
        "fragments": [_fragment_dict(item) for item in _ordered_fragments(fragments)],
    }
    return json.dumps(value, ensure_ascii=True, indent=2, separators=(",", ": ")) + "\n"


def _safe_output_path(value: str) -> Path:
    if not value:
        _fail("output path must not be empty")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if windows_path.is_absolute() or windows_path.drive or posix_path.is_absolute():
        _fail("output path must be relative")
    if ".." in windows_path.parts or ".." in posix_path.parts:
        _fail("output path must not contain traversal")
    output = Path(value)
    root = Path.cwd().resolve()
    resolved = output.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        _fail("output path must remain below the working directory")
    return output


def _write_atomic(path: Path, content: str, *, overwrite: bool) -> None:
    parent = path.parent if path.parent != Path("") else Path(".")
    if not parent.is_dir():
        _fail(f"output parent is not a directory: {parent}")
    if path.is_symlink():
        _fail(f"output must not be a symlink: {path}")
    if path.exists():
        if not path.is_file():
            _fail(f"output is not a regular file: {path}")
        if not overwrite:
            _fail(f"output already exists (use --overwrite): {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                _fail(f"output already exists (use --overwrite): {path}")
            temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a fragment directory")
    validate.add_argument("directory")

    render = subparsers.add_parser("render", help="render a fragment directory")
    render.add_argument("directory")
    render.add_argument("output", nargs="?")
    render.add_argument("--output", dest="output_option")
    render.add_argument("--format", choices=("markdown", "json"))
    render.add_argument("--release-id")
    render.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the release-note command-line interface."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        fragments = collect_fragments(arguments.directory)
        if arguments.command == "validate":
            print(f"validated {len(fragments)} release-note fragment(s)")
            return 0

        outputs = [
            value for value in (arguments.output, arguments.output_option) if value
        ]
        if len(outputs) != 1:
            _fail("render requires exactly one output path")
        output = _safe_output_path(outputs[0])
        fragment_directory = Path(arguments.directory).resolve()
        try:
            output.resolve(strict=False).relative_to(fragment_directory)
        except ValueError:
            pass
        else:
            _fail("output path must not be inside the fragment directory")
        output_format = arguments.format
        if output_format is None:
            output_format = "json" if output.suffix == ".json" else "markdown"
        content = (
            render_json(fragments, arguments.release_id)
            if output_format == "json"
            else render_markdown(fragments, arguments.release_id)
        )
        _write_atomic(output, content, overwrite=arguments.overwrite)
        print(f"rendered {len(fragments)} release-note fragment(s) to {output}")
        return 0
    except (OSError, ReleaseNoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
