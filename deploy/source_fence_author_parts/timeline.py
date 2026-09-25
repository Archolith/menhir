"""Timezone-strict timestamp parsing and evidence freshness checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from .base import EVIDENCE_MAX_AGE, SourceFenceError


def _parse_utc(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SourceFenceError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceFenceError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceFenceError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise SourceFenceError("clock must return a timezone-aware datetime")
    return current.astimezone(timezone.utc)


def _assert_fresh(observed: datetime, now: datetime, label: str) -> None:
    if observed > now + timedelta(seconds=60):
        raise SourceFenceError(f"{label} is in the future")
    if observed < now - EVIDENCE_MAX_AGE:
        raise SourceFenceError(f"{label} is stale")
