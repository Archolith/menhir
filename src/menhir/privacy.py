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

    Structural fields are left intact. Nested dict/list/tuple values under a
    redacted field key are recursed with their shape preserved, so consumers can
    still iterate items or index into nested structures after redaction.
    """
    if reveal:
        return row
    out = dict(row)
    for key in list(out.keys()):
        if key in fields:
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
# Everything above this line is DISPLAY-time redaction, as the module docstring says:
# it hides content on the way to a human viewer and never changes what is stored.
# What follows is different in kind and is the only part of this module that does.
#
# MCP tool arguments were persisted verbatim into `mcp_events.payload_preview` -- the
# first 500 characters of every memory a user submitted, in plaintext, in the sidecar
# database. That is the absence of an at-rest control rather than a broken one, so the
# fix belongs at the write boundary.
#
# The policy is an ALLOWLIST and it is deliberately inverted relative to REDACTED_FIELDS
# above. Display-time redaction can name the content fields because the surfaces are
# known and finite. A tool argument is neither: 92 distinct parameter names across 54
# tools today, and the next tool added is not going to update a denylist. So a string
# survives only if its key is named here as structural; anything else is masked. A new
# content-bearing parameter is then private by default instead of leaked by default.

#: Argument names whose STRING values are structural and safe to persist. Derived by
#: enumerating every `endpoint()` signature under `mcp/tools/`; identifiers, enums,
#: namespaces and selectors, never free text or filesystem paths.
TELEMETRY_SAFE_ARG_NAMES: frozenset[str] = frozenset(
    {
        "action",
        "artifact_type",
        "artifact_uuid",
        "bootstrap_scope",
        "candidate_type",
        "client_id",
        "client_name",
        "cluster_id",
        "cursor",
        "document_type",
        "episode_uuid",
        "evidence_strength",
        "expected_old_integrity",
        "from_commit",
        "from_status",
        "group_id",
        "keep_uuid",
        "kind",
        "namespace",
        "new_uuid",
        "node_uuid",
        "observed_integrity",
        "old_uuid",
        "operation",
        "preset",
        "priority",
        "project",
        "query_type",
        "reader_id",
        "recall_id",
        "relation",
        "remove_uuid",
        "repository",
        "session_id",
        "source",
        "source_uuid",
        "state",
        "status",
        "structure_project",
        "target_uuid",
        "tier",
        "to_status",
        "turn_evidence_uuid",
        "type",
        "user_id",
        "uuid",
        "workspace",
    }
)


def redact_payload_for_storage(value: object, *, key: str | None = None) -> object:
    """Mask free text in a telemetry payload before it is written to the sidecar.

    Non-string scalars (int, float, bool, None) are structural by type and pass through,
    so a preview still shows the SHAPE of a call -- which argument names were supplied,
    which limits and flags were set -- without its content. Strings survive only under a
    key in :data:`TELEMETRY_SAFE_ARG_NAMES`. Containers are walked, and a string inside a
    list inherits the list's key, so ``notes=["..."]`` is masked as a whole.

    Note the asymmetry with :func:`redact_text`: that one passes empty strings through
    because there is nothing to hide at display time. Here an empty string is masked like
    any other unlisted string, because the value of this function is that its output does
    not depend on the content it was given.
    """
    if isinstance(value, dict):
        return {k: redact_payload_for_storage(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload_for_storage(v, key=key) for v in value]
    if isinstance(value, str):
        if key is not None and key in TELEMETRY_SAFE_ARG_NAMES:
            return value
        return MASK
    return value


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
