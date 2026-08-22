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

**TWO TIERS, AND THEY ARE NOT EQUALLY STRONG (CF-96).** One setting governs both, so the
difference is easy to miss:

- :func:`redact_mapping` / :func:`redact_rows` are **field-exact**. They mask by KEY against
  :data:`REDACTED_FIELDS`, so every value under a memory-bearing field is masked regardless of
  its shape. This is what the explorer UI uses.
- :func:`redact_log_line` is **heuristic, and bounded by what it can see**. It masks only QUOTED
  spans in an already-rendered string, so memory content interpolated through an unquoted
  ``%s`` passes through untouched. Short quoted values pass too, by design -- the free-text
  floor is :data:`_MIN_FREE_TEXT_LEN` characters.

The durable fix for the second tier is field-aware structured logging, where memory-bearing
fields are marked before rendering rather than recovered from the finished string. Until then the
rule for producers is: **do not put memory content into a log line**. Log the uuid instead -- it
is structural, it is never masked, and it identifies the node for anyone debugging. CF-96's
demonstrated leak (`services/correlation_service.py`, judge votes) was fixed that way, not by
teaching this regex another shape.
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

# Structural fields that MUST survive redaction so the view stays usable. This
# set is ENFORCED: in redact_mapping (which redact_rows delegates to), a field
# listed here is never masked, even when the caller's `fields` deny-list names it.
# Structural wins over the deny-list (see the collision guard in redact_mapping).
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


def _redact_value(value: object) -> object:
    """Recursively mask non-empty text, preserving container shape.

    ``str`` (non-empty) -> ``MASK``; ``list``/``tuple`` -> same type with each
    element recursed; ``dict`` -> same keys with each value recursed (keys are
    structural, never masked); anything else (int, None, bool, empty string) ->
    unchanged, matching :func:`redact_text`'s passthrough.
    """
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return redact_text(value, reveal=False)


def redact_mapping(
    row: dict,
    *,
    reveal: bool = False,
    fields: frozenset[str] = REDACTED_FIELDS,
) -> dict:
    """Return a shallow copy of ``row`` with configured free-text fields masked.

    Structural fields are left intact -- a field in ``STRUCTURAL_FIELDS`` is never
    masked, even if it also appears in the caller's ``fields`` deny-list. Nested
    dict/list/tuple values under a redacted field key are recursed with their shape
    preserved, so consumers can still iterate items or index into nested structures
    after redaction.
    """
    if reveal:
        return row
    out = dict(row)
    for key in list(out.keys()):
        if key in fields and key not in STRUCTURAL_FIELDS:
            out[key] = _redact_value(out[key])
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


# --- at-rest redaction for persisted telemetry payloads --------------------
#
# This module is DISPLAY-time redaction, as the docstring above says. The at-rest control for
# `mcp_events.payload_preview` deliberately does NOT live here: it is
# `infrastructure/telemetry/helpers._redact_telemetry_value`, applied inside `_preview_of` so
# every writer of that column gets it without opting in.
#
# A duplicate of it briefly lived here (CF-167). It was removed on merging the CF-165 E2E closure
# work, which had arrived at the same control independently and with a property this one lacked:
# an allowlisted KEY is not enough, the VALUE must also be identifier-shaped. Without that, a
# caller passing `namespace="<prose>"` had the prose retained because the key was on the list.
# Two implementations of one rule is how they drift; the stricter one won.


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

# Quoted strings frequently carry memory content. The opening quote must sit at a
# span boundary (start of string, or preceded by whitespace or a structural
# separator) so a mid-word apostrophe -- an ordinary English possessive -- cannot
# open a span. Two fixed-width lookbehinds are required because Python rejects a
# variable-width lookbehind.
_QUOTED = re.compile(r"(?:(?<=^)|(?<=[\s=(){}\[\],:]))(['\"])(.*?)\1", re.DOTALL)

# Minimum length for a quoted string to be treated as free text worth masking.
_MIN_FREE_TEXT_LEN = 12

# Key names whose assigned value is a credential regardless of how short or identifier-like it
# looks. Matched against the tail of the text preceding a ``=``/``:``, so ``api_key``,
# `"password"`, and ``X-Auth-Token`` all hit. REDACTED_FIELDS covers the mapping path; this is
# the log-line equivalent and the two are deliberately kept in the same shape.
_SECRET_KEY_TAIL = re.compile(
    r"(?:^|[\s,{\[(\"'])[\w.\-]*"
    r"(?:passwd|password|secret|token|api[_\-]?key|apikey|auth|credential|bearer|"
    r"private[_\-]?key|session[_\-]?id|cookie|signature)"
    r"[\w.\-]*[\"']?$",
    re.IGNORECASE,
)

# Identifier-ish quoted values that are structural noise, not memory content:
# enum tokens, dict keys, status codes, snake/UPPER identifiers, uuids, numbers.
_IDENTIFIER_LIKE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_.:/-]*|\d+|[0-9a-fA-F-]{8,})$"
)


def _is_secret_slot(body: str, start: int) -> bool:
    """True if the quote at ``start`` closes a ``key=`` / ``key:`` assignment.

    CF-24's registered reproducer is ``password='hunter2' token='abc123'``. Both values are
    too short and too identifier-like for :func:`_is_free_text`, which is tuned for memory
    content -- a credential is precisely the thing that does NOT look like a sentence. Length
    heuristics cannot separate a secret from an enum token, so this asks a structural question
    instead: what is the value assigned to?
    """
    prefix = body[:start].rstrip()
    if not prefix or prefix[-1] not in "=:":
        return False
    key = _SECRET_KEY_TAIL.search(prefix[:-1].rstrip())
    return key is not None


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

    Keeps the structural prefix (timestamp/logger/level) and masks two disjoint classes:
    quoted strings that look like free text (:func:`_is_free_text`), and quoted values
    assigned to a credential-shaped key (:func:`_is_secret_slot`). Short identifiers, enum
    tokens, dict keys, uuids, and numbers are otherwise preserved so the line stays
    diagnosable.

    Conservative and heuristic for MEMORY CONTENT -- a display aid for screen-sharing, not a
    hard guarantee for arbitrary prose. The credential path is narrower and stricter: it is
    keyed on the assignment target rather than on what the value looks like, because a secret
    is indistinguishable from an enum token by shape alone (CF-24).
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
        # Two independent reasons to mask, and they catch opposite shapes: free text is long
        # and sentence-like, a credential is short and token-like. CF-24's reproducer needs the
        # second, which is why the length gate alone left both of its secrets in the clear.
        if _is_free_text(value) or _is_secret_slot(body, mo.start()):
            return f"{quote}{MASK}{quote}"
        return mo.group(0)

    return prefix + _QUOTED.sub(_sub, body)
