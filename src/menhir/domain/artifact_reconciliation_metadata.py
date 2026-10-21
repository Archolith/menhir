"""Authored metadata.

The ``---`` frontmatter block and the H1/``Status:`` headers a document
declares about itself, parsed and validated, with every refusal reported
as an error string rather than raised.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from menhir.domain.work_artifact import ARTIFACT_TYPES, valid_statuses


# ---------------------------------------------------------------------------
# Authored metadata
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_STATUS_RE = re.compile(
    r"^\s*[-*]?\s*\*{0,2}status\*{0,2}\s*:\s*(.+?)\s*$", re.IGNORECASE
)

#: Keys the frontmatter block may carry that this module interprets. Relationship
#: keys are validated elsewhere (``normalize_declarations``) and pass through
#: untouched -- reconciliation never resolves or removes a relationship.
_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "artifact_schema",
        "artifact_uuid",
        "artifact_type",
        "artifact_status",
    }
)

#: Keys an author must never write: menhir derives them from the source itself.
#: Present in a document, they are a stale copy of a derived fact, so they are
#: rejected rather than read.
DERIVED_KEYS: frozenset[str] = frozenset(
    {
        "corpus_lane",
        "integrity",
        "integrity_algorithm",
        "version",
        "version_kind",
        "observed_commit",
        "size_bytes",
        "source_uuid",
        "resolution_status",
        "resolution_reason",
        "last_seen_at",
        "last_reconciled_at",
        "last_reconcile_basis",
        "schema_version",
    }
)


@dataclass(frozen=True)
class DocumentMetadata:
    """What a document declares about itself, plus why any of it was refused.

    ``errors`` is populated instead of raising: one malformed record must not
    abort a corpus scan, and an author needs the exact reason rather than a
    stack trace.
    """

    artifact_uuid: str | None = None
    artifact_type: str | None = None
    artifact_status: str | None = None
    schema: int | None = None
    title: str | None = None
    raw_status_header: str | None = None
    has_frontmatter: bool = False
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, tuple[str, ...]]:
    """Split a leading ``---`` block into a flat mapping, body, and errors.

    A deliberately small parser rather than a YAML dependency. The authoring
    contract allows scalars and simple inline/dash lists; anything richer is
    reported as an error instead of being guessed at, which keeps "menhir read
    my metadata differently than I wrote it" off the table.
    """
    errors: list[str] = []
    if not text.startswith("---"):
        return {}, text, ()

    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text, ()

    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text, ("frontmatter_not_terminated",)

    mapping: dict[str, Any] = {}
    #: Keys a duplicate line has refused. A duplicate is ambiguous about which binding is
    #: authoritative, and ambiguity fails closed: the value is NOT bound at all rather than picking
    #: first or last. Tracked separately from ``mapping`` so a third occurrence cannot silently
    #: re-bind the key after the first duplicate popped it.
    refused_keys: set[str] = set()
    current_key: str | None = None
    for raw_line in lines[1:end]:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- "):
            if current_key is None:
                errors.append("frontmatter_list_item_without_key")
                continue
            item = line.lstrip()[2:].strip().strip("'\"")
            existing = mapping.get(current_key)
            if isinstance(existing, list):
                existing.append(item)
            elif existing in (None, ""):
                mapping[current_key] = [item]
            else:
                mapping[current_key] = [existing, item]
            continue
        if ":" not in line:
            errors.append("frontmatter_line_without_key")
            continue
        if line[0] in " \t":
            errors.append("frontmatter_nested_mapping_unsupported")
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key in mapping or key in refused_keys:
            errors.append(f"duplicate_frontmatter_key:{key}")
            mapping.pop(key, None)
            refused_keys.add(key)
            continue
        if value.startswith("[") and value.endswith("]"):
            items = [
                part.strip().strip("'\"")
                for part in value[1:-1].split(",")
                if part.strip()
            ]
            mapping[key] = items
        elif value:
            mapping[key] = value.strip("'\"")
        else:
            mapping[key] = ""
        current_key = key

    body = "\n".join(lines[end + 1 :])
    return mapping, body, tuple(errors)


def read_document_metadata(
    text: str, *, route_type: str | None = None
) -> DocumentMetadata:
    """Read the authoring block and H1 title out of a document's text.

    ``route_type`` is only used to check a declared status against the type the
    directory asserts. A disagreement is an error, never a silent preference for
    one side: the route and the declaration are both authored, and picking a
    winner would hide the mistake.
    """
    mapping, body, errors_tuple = parse_frontmatter(text)
    errors: list[str] = list(errors_tuple)

    title: str | None = None
    raw_status: str | None = None
    for line in body.splitlines()[:40]:
        if title is None:
            match = _H1_RE.match(line)
            if match:
                title = match.group(1).strip()
                continue
        if raw_status is None:
            match = _STATUS_RE.match(line)
            if match:
                raw_status = match.group(1).strip()

    for key in mapping:
        if key in DERIVED_KEYS:
            errors.append(f"derived_key_declared:{key}")

    declared_uuid = _single_value(mapping.get("artifact_uuid"))
    if declared_uuid is not None:
        if not _UUID_RE.match(declared_uuid):
            errors.append("invalid_artifact_uuid")
            declared_uuid = None
        else:
            declared_uuid = declared_uuid.lower()

    declared_type = _single_value(mapping.get("artifact_type"))
    if declared_type is not None:
        declared_type = declared_type.strip().lower()
        if declared_type not in ARTIFACT_TYPES:
            errors.append("unknown_artifact_type")
            declared_type = None
        elif route_type is not None and declared_type != route_type:
            errors.append("route_type_disagreement")

    effective_type = declared_type or route_type
    declared_status = _single_value(mapping.get("artifact_status"))
    if declared_status is not None:
        declared_status = declared_status.strip().upper()
        if effective_type is None:
            errors.append("status_without_type")
            declared_status = None
        elif declared_status not in valid_statuses(effective_type):
            errors.append("status_invalid_for_type")
            declared_status = None

    schema_raw = _single_value(mapping.get("artifact_schema"))
    schema: int | None = None
    if schema_raw is not None:
        try:
            schema = int(schema_raw)
        except ValueError:
            errors.append("invalid_artifact_schema")

    return DocumentMetadata(
        artifact_uuid=declared_uuid,
        artifact_type=declared_type,
        artifact_status=declared_status,
        schema=schema,
        title=title,
        raw_status_header=raw_status,
        has_frontmatter=bool(mapping),
        errors=tuple(errors),
    )


def _single_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return None if not value else str(value[0])
    text = str(value).strip()
    return text or None


def sha256_bytes(payload: bytes) -> str:
    """Raw-byte digest. No normalization: a CRLF change *is* a source change.

    Normalizing line endings or Markdown before hashing would make the hash
    agree with how a document renders rather than with what the file contains,
    and integrity evidence has to answer the second question.
    """
    return hashlib.sha256(payload).hexdigest()
