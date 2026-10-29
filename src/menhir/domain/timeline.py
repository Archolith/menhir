"""Domain models for `recall_timeline`: on-demand time navigation over memories.

Phase A of .agent/plans/menhir-recall-timeline-plan.md. Pure data + cursor codec;
all graph access lives in the service and query layers. Timeline order is RECORDED
time (episode `valid_at`) per the owner ruling in the plan.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

#: Cursor direction keys. "after" pages forward (ascending), "before" pages backward.
CURSOR_DIRECTIONS = ("after", "before")

#: Schema version byte for the cursor payload. Bumping it invalidates old cursors.
_CURSOR_VERSION = 1


@dataclass(frozen=True)
class TimelineFact:
    """One RELATES_TO fact attached to a timeline episode (world-time basis)."""

    fact: str
    valid_at: str | None
    invalid_at: str | None
    expired_at: str | None
    time_basis: str = "world"


@dataclass(frozen=True)
class TimelineEntry:
    """One episode in a timeline page, in ascending recorded-time order."""

    uuid: str
    recorded_at: str
    created_at: str | None
    session_id: str | None
    source: str | None
    headline: str
    content: str | None
    facts: tuple[TimelineFact, ...]
    is_anchor: bool


@dataclass(frozen=True)
class TypedHistoryEntry:
    """One page entry of a typed (scalar or event) history View."""

    valid_at: str
    value: Any
    operation: str | None
    time_basis: str | None
    quote: str | None
    episode_uuid: str | None


@dataclass(frozen=True)
class TypedHistory:
    """One typed-history View paged beside a subject timeline."""

    kind: str
    view_uuid: str
    label: str
    total: int
    offset: int
    entries: tuple[TypedHistoryEntry, ...]
    prev_offset: int | None
    next_offset: int | None


@dataclass(frozen=True)
class TimelineResult:
    """The `recall_timeline` result: one page of a thread plus typed histories."""

    thread: str
    subject_uuid: str | None
    subject_name: str | None
    entries: tuple[TimelineEntry, ...]
    prev_cursor: str | None
    next_cursor: str | None
    histories: tuple[TypedHistory, ...]
    note: str | None
    time_basis: str = "recorded"


def _namespace_key(namespace: str | None) -> str:
    """The `ns` cursor key binding a cursor to one silo (or all silos when unscoped)."""
    from menhir.domain.namespace import namespace_to_group_ids

    return "|".join(namespace_to_group_ids(namespace) or ["*"])


def encode_cursor(
    key: tuple[str, str | None, str],
    direction: str,
    namespace_key: str,
    subject_uuid: str | None,
) -> str:
    """Encode an order key + paging direction as a tenant/thread-bound opaque cursor.

    `key` is `(valid_at_iso, created_at_iso, uuid)`; `created_at_iso` may be None (the
    epoch coalesce of the total order). The cursor embeds the namespace key and subject
    uuid so `decode_cursor` can refuse cursors that cross tenants or threads.
    """
    if direction not in CURSOR_DIRECTIONS:
        raise ValueError(f"cursor direction must be one of {CURSOR_DIRECTIONS}, got {direction!r}")
    valid_at, created_at, uuid = key
    payload = {
        "v": str(valid_at),
        "c": None if created_at is None else str(created_at),
        "u": str(uuid),
        "d": direction,
        "ns": namespace_key,
        "s": subject_uuid or "",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(
    token: str,
    *,
    namespace_key: str,
    subject_uuid: str | None,
) -> tuple[tuple[str, str | None, str], str]:
    """Decode a cursor token, refusing tampered, malformed, or foreign cursors.

    Returns `((valid_at, created_at, uuid), direction)`. Raises `ValueError` on bad
    base64/JSON, missing keys, an unknown direction, or an `ns`/`s` mismatch -- a cursor
    must never cross tenants or threads.
    """
    if not token or not isinstance(token, str):
        raise ValueError("cursor must be a non-empty token")
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("cursor is not a valid timeline cursor") from exc
    if not isinstance(payload, dict):
        raise ValueError("cursor is not a valid timeline cursor")
    required = ("v", "c", "u", "d", "ns", "s")
    if any(field not in payload for field in required):
        raise ValueError("cursor is missing required keys")
    if payload["d"] not in CURSOR_DIRECTIONS:
        raise ValueError(f"cursor direction must be one of {CURSOR_DIRECTIONS}")
    if payload["ns"] != namespace_key:
        raise ValueError("cursor does not belong to this namespace")
    if payload["s"] != (subject_uuid or ""):
        raise ValueError("cursor does not belong to this thread")
    created_at = payload["c"]
    return (
        (str(payload["v"]), None if created_at is None else str(created_at), str(payload["u"])),
        str(payload["d"]),
    )
