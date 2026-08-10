"""Central memory-content redaction for privacy mode.

Single source of truth for hiding memory *contents* (free text) wherever they
surface for a human viewer: the console dashboard's log tail and the explorer web
UI. Redaction happens at DISPLAY time only -- log files and the Neo4j graph are
never modified.

Design rules:
- Structural fields (uuid, labels, scope, session_id, timestamps, counts, kinds)
  are NEVER redacted, so the graph stays navigable. Only free text is hidden.
- Redaction is conservative for log lines: better to over-mask than to leak.
- ``reveal=True`` is a full passthrough (privacy off / explicitly revealed).
"""

from __future__ import annotations

import re

MASK = "[hidden]"

# Free-text memory fields that carry actual memory content across surfaces.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {
        "content",
        "summary",
        "preview",
        "summary_preview",
        "notes",
        "name",
        "label",
    }
)

# Structural fields that must survive redaction so the view stays usable.
STRUCTURAL_FIELDS: frozenset[str] = frozenset(
    {
        "uuid",
        "id",
        "labels",
        "scope",
        "kind",
        "type",
        "session_id",
        "user_id",
        "source",
        "created_at",
        "last_accessed",
        "rel_type",
    }
)


def redact_text(value: object, *, reveal: bool = False) -> object:
    """Return ``MASK`` for non-empty text when not revealing; passthrough otherwise.

    Non-string or empty values pass through unchanged (nothing to hide).
    """
    if reveal:
        return value
    if isinstance(value, str) and value.strip():
        return MASK
    return value


def redact_mapping(
    row: dict,
    *,
    reveal: bool = False,
    fields: frozenset[str] = REDACTED_FIELDS,
) -> dict:
    """Return a shallow copy of ``row`` with configured free-text fields masked.

    Structural fields are left intact. Nested dict/list values under a redacted
    field key are masked wholesale (they are memory text if the key is redacted).
    """
    if reveal:
        return row
    out = dict(row)
    for key in list(out.keys()):
        if key in fields:
            out[key] = redact_text(out[key], reveal=False)
    return out


def redact_rows(
    rows: list[dict],
    *,
    reveal: bool = False,
    fields: frozenset[str] = REDACTED_FIELDS,
) -> list[dict]:
    """Apply :func:`redact_mapping` to each row in a list."""
    if reveal:
        return rows
    return [redact_mapping(r, reveal=False, fields=fields) for r in rows]


# --- log-line redaction ----------------------------------------------------
#
# A server.log line looks like:
#   2026-07-12 14:36:27,333 - menhir.services.correlation_service - INFO - <message>
# The prefix (timestamp - logger - LEVEL - ) is structural and kept. The message
# body can embed memory content (entity/episode names, content previews). We mask
# the free-text parts of the body while preserving key=value metrics and uuids.

_LOG_PREFIX = re.compile(
    r"^(?P<prefix>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d+\s*-\s*[\w.]+\s*-\s*\w+\s*-\s*)(?P<body>.*)$",
    re.DOTALL,
)

# Quoted strings frequently carry memory content.
_QUOTED = re.compile(r"(['\"])(.*?)\1", re.DOTALL)

# Minimum length for a quoted string to be treated as free text worth masking.
_MIN_FREE_TEXT_LEN = 12

# Identifier-ish quoted values that are structural noise, not memory content:
# enum tokens, dict keys, status codes, snake/UPPER identifiers, uuids, numbers.
_IDENTIFIER_LIKE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.:/-]*|\d+|[0-9a-fA-F-]{8,})$"
)


def _is_free_text(value: str) -> bool:
    """True if a quoted value looks like human/memory free text rather than an identifier.

    Free text = long enough AND contains whitespace (a sentence/phrase) AND is not a
    bare identifier/enum/uuid/number. This keeps log structure readable while still
    masking the parts that can carry memory content.
    """
    stripped = value.strip()
    if len(stripped) < _MIN_FREE_TEXT_LEN:
        return False
    if _IDENTIFIER_LIKE.match(stripped):
        return False
    return " " in stripped


def redact_log_line(line: str, *, reveal: bool = False) -> str:
    """Best-effort masking of memory content embedded in a single log line.

    Keeps the structural prefix (timestamp/logger/level) and masks quoted strings that
    look like free text (see :func:`_is_free_text`). Short identifiers, enum tokens,
    dict keys, uuids, and numbers are preserved so the line stays diagnosable.

    Conservative and heuristic -- a display aid for screen-sharing, NOT a hard guarantee
    for arbitrary log text.
    """
    if reveal or not line:
        return line
    m = _LOG_PREFIX.match(line)
    if m:
        prefix, body = m.group("prefix"), m.group("body")
    else:
        prefix, body = "", line

    def _sub(mo: re.Match[str]) -> str:
        quote, value = mo.group(1), mo.group(2)
        if _is_free_text(value):
            return f"{quote}{MASK}{quote}"
        return mo.group(0)

    return prefix + _QUOTED.sub(_sub, body)
