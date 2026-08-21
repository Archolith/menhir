"""Normalization of a todo's author-declared ``code_ref`` into locations.

A ``code_ref`` is free text an author typed. Real values are not uniform: some
are workspace-relative (``projects/archolith/menhir/src/...``), some are
project-relative (``src/menhir/...``), some carry a line or a symbol, some name
several files in one string, and some are prose that is not a location at all.

Multi-path refs are genuine cardinality, not ambiguity, so this module returns a
*list* of locations preserving the author's ordering. Each location resolves or
fails independently -- one segment may normalize cleanly while another stays
unresolved, and both are retained.

Nothing here guesses. A segment that cannot be normalized is returned with
``resolution_status='unresolved'`` and a reason, never with a fabricated path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from menhir.domain.namespace import DEFAULT_NAMESPACE

#: Bumped when the normalization rules change, so a re-run can tell already
#: migrated locations from ones produced by older rules.
LOCATION_SCHEMA_VERSION = 1

#: Shared todo silo. Lives here rather than in the repository so consumers that
#: read todos structurally (blast radius) can apply the same visibility rule
#: without importing the repository and creating a cycle.
#: Alias of the canonical namespace constant (CF-76). Kept so the public name
#: "DEFAULT_TODO_NAMESPACE" still resolves as this module's public surface; the
#: value is single-sourced in domain.namespace.
DEFAULT_TODO_NAMESPACE = DEFAULT_NAMESPACE

#: Author-declared segments are separated by comma, semicolon, or " + ".
_SEGMENT_SPLIT = re.compile(r"\s*[;,]\s*|\s+\+\s+")

#: Workspace layout is projects/<org>/<project>/<path>.
_WORKSPACE_PREFIX = re.compile(r"^projects/[^/]+/([^/]+)/(.+)$")

#: A drive-letter absolute Windows path.
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")

#: Trailing ":123", ":123-456", ":name", or "::name".
_LINE_RANGE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)-(?P<end>\d+)$")
_LINE_ONLY = re.compile(r"^(?P<path>.+?):(?P<start>\d+)$")
_DOUBLE_COLON_SYMBOL = re.compile(r"^(?P<path>.+?)::(?P<symbol>[^:]+)$")
_SINGLE_COLON_SYMBOL = re.compile(r"^(?P<path>.+?\.[A-Za-z0-9]{1,6}):(?P<symbol>\D.*)$")

#: A path segment looks like a file when it carries an extension.
_HAS_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,6}$")

#: Prose markers -- a declaration containing these is commentary, not a location.
_PROSE_MARKERS = (" - ", " needs ", "(", ")", "@")


@dataclass(frozen=True)
class ParsedLocation:
    """One normalized location declared by a todo author."""

    ordinal: int
    raw_segment: str
    project: str | None = None
    path: str | None = None
    kind: str = "unknown"          # file | directory | symbol | unknown
    line_start: int | None = None
    line_end: int | None = None
    symbol: str | None = None
    resolution_status: str = "unresolved"   # resolved | unresolved
    unresolved_reason: str | None = None
    schema_version: int = LOCATION_SCHEMA_VERSION

    def as_properties(self) -> dict[str, object]:
        """Flat property map for the :TodoLocation node."""
        return {
            "ordinal": self.ordinal,
            "raw_segment": self.raw_segment,
            "project": self.project,
            "path": self.path,
            "kind": self.kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol,
            "resolution_status": self.resolution_status,
            "unresolved_reason": self.unresolved_reason,
            "schema_version": self.schema_version,
        }


def _normalize_separators(raw: str) -> str:
    return raw.replace("\\", "/")


def _escapes_root(path: str) -> bool:
    """True when '..' segments walk above the repository root."""
    depth = 0
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def _collapse(path: str) -> str:
    out: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def _split_trailing(segment: str) -> tuple[str, int | None, int | None, str | None]:
    """Peel a trailing line number, line range, or symbol off a path."""
    m = _LINE_RANGE.match(segment)
    if m:
        return m.group("path"), int(m.group("start")), int(m.group("end")), None
    m = _LINE_ONLY.match(segment)
    if m:
        return m.group("path"), int(m.group("start")), None, None
    m = _DOUBLE_COLON_SYMBOL.match(segment)
    if m:
        return m.group("path"), None, None, m.group("symbol").strip()
    m = _SINGLE_COLON_SYMBOL.match(segment)
    if m:
        return m.group("path"), None, None, m.group("symbol").strip()
    return segment, None, None, None


def _strip_project(
    path: str, known_projects: frozenset[str] | None
) -> tuple[str | None, str]:
    """Split a leading project identifier off a path, if one is present.

    Handles the workspace layout (``projects/<org>/<project>/<path>``) and the
    bare form (``<project>/<path>``) when the leading segment is a project the
    structure graph actually knows about. Guessing is deliberately avoided: an
    unrecognized leading segment is left as part of the path.
    """
    m = _WORKSPACE_PREFIX.match(path)
    if m:
        return m.group(1), m.group(2)
    if known_projects:
        head, _, rest = path.partition("/")
        if rest and head in known_projects:
            return head, rest
    return None, path


def _looks_like_prose(segment: str) -> bool:
    if any(marker in segment for marker in _PROSE_MARKERS):
        return True
    # A bare sentence: several words and no path separator or extension.
    return "/" not in segment and not _HAS_EXTENSION.search(segment) and segment.count(" ") >= 2


def _parse_segment(
    raw_segment: str,
    ordinal: int,
    *,
    explicit_project: str | None,
    known_projects: frozenset[str] | None,
) -> ParsedLocation:
    segment = raw_segment.strip()
    if not segment:
        return ParsedLocation(ordinal, raw_segment, unresolved_reason="empty")

    if _looks_like_prose(segment):
        return ParsedLocation(ordinal, raw_segment, unresolved_reason="not_a_path")

    text = _normalize_separators(segment)

    if _WINDOWS_ABSOLUTE.match(text):
        # Keep only the part below the workspace root; anything else is
        # machine-specific and not portable into the graph.
        marker = "/IdeaProjects/"
        idx = text.find(marker)
        if idx == -1:
            return ParsedLocation(ordinal, raw_segment, unresolved_reason="absolute_path_outside_workspace")
        text = text[idx + len(marker):]

    path_part, line_start, line_end, symbol = _split_trailing(text)

    if _escapes_root(path_part):
        return ParsedLocation(ordinal, raw_segment, unresolved_reason="path_escapes_root")

    path_part = _collapse(path_part)
    if not path_part:
        return ParsedLocation(ordinal, raw_segment, unresolved_reason="empty_after_normalization")

    parsed_project, path = _strip_project(path_part, known_projects)

    # A3 precedence: explicit structure_project wins; a conflict is unresolved,
    # never silently normalized to one side.
    if explicit_project and parsed_project and explicit_project != parsed_project:
        return ParsedLocation(
            ordinal,
            raw_segment,
            project=None,
            path=path,
            unresolved_reason="project_conflict",
        )
    project = explicit_project or parsed_project

    trailing_slash = segment.endswith("/") or segment.endswith("\\")
    if symbol:
        kind = "symbol"
    elif trailing_slash or not _HAS_EXTENSION.search(path):
        kind = "directory"
    else:
        kind = "file"

    return ParsedLocation(
        ordinal=ordinal,
        raw_segment=raw_segment,
        project=project,
        path=path,
        kind=kind,
        line_start=line_start,
        line_end=line_end,
        symbol=symbol,
        resolution_status="resolved",
        unresolved_reason=None,
    )


def parse_code_ref(
    code_ref: str | None,
    *,
    structure_project: str | None = None,
    known_projects: frozenset[str] | None = None,
) -> list[ParsedLocation]:
    """Normalize a ``code_ref`` into zero or more locations.

    ``structure_project`` is the author's explicit declaration and takes
    precedence over any project parsed out of the path. ``known_projects``
    lets a bare ``<project>/<path>`` form be recognized without guessing.

    Ordering is the author's; ``ordinal`` preserves it. Exact duplicate
    normalized locations collapse to the first occurrence.
    """
    if not code_ref or not code_ref.strip():
        return []

    segments = [s for s in _SEGMENT_SPLIT.split(code_ref) if s and s.strip()]
    locations: list[ParsedLocation] = []
    seen: set[tuple] = set()

    for raw in segments:
        loc = _parse_segment(
            raw,
            len(locations),
            explicit_project=structure_project,
            known_projects=known_projects,
        )
        key = (loc.project, loc.path, loc.line_start, loc.line_end, loc.symbol, loc.resolution_status)
        if loc.resolution_status == "resolved" and key in seen:
            continue
        seen.add(key)
        locations.append(loc)

    return locations


def code_ref_file_predicate(file_alias: str, path_expr: str) -> str:
    """Return the anchored Cypher predicate matching a structural file to a code_ref path.

    Single source of truth for the ``code_ref`` -> structural file resolver, so the
    write path and every reader cannot drift apart. The anchor is what keeps a
    basename match on a real path boundary: the bare ``ENDS WITH`` form this replaces
    matched any path suffix, so a ref of ``utils.py`` also matched ``my_utils.py`` and
    ``test_utils.py``. Shared verbatim, like ``_INELIGIBLE_ROLE_PREDICATE`` in
    ``correlation_queries.py``, precisely so the two sites cannot drift apart.
    """
    return (
        "(\n"
        f"    {file_alias}.structure_path = {path_expr}\n"
        f"    OR (NOT {path_expr} CONTAINS '/' AND "
        f"{file_alias}.structure_path ENDS WITH ('/' + {path_expr}))\n"
        ")"
    )
