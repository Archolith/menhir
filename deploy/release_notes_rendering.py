"""Deterministic Markdown and JSON rendering of release-note fragments."""

from __future__ import annotations

import json
from typing import Any, Sequence

from release_notes_fragments import (
    CATEGORIES,
    REPOSITORIES,
    SCHEMA,
    ReleaseNoteFragment,
    _RELEASE_ID_RE,
    _fail,
    _fragment_sort_key,
)


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
