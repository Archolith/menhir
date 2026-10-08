"""Event time as the anchored-time resolver knew it (P2 render, MENHIR_ANCHORED_TIME_RENDER).

The persisted ``time_*`` edge properties say HOW a fact's time was known. Rendering uses them so
a speech date that Graphiti defaulted into ``valid_at`` is never shown as an occurrence date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping

#: Resolver outcomes whose closed window is the event time (written, or Graphiti agreed).
_WINDOW_OUTCOMES = frozenset({"written", "graphiti_inside_window"})

#: Edge properties projected for rendering, as ``r.<name> AS <name>``.
EVENT_TIME_PROPERTIES = (
    "time_basis",
    "time_expression",
    "time_kind",
    "time_granularity",
    "time_window_start",
    "time_window_end",
    "time_outcome",
    "time_speech_date",
)


@dataclass(frozen=True)
class EventTime:
    basis: str
    expression: str | None = None
    kind: str | None = None
    granularity: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    outcome: str | None = None
    speech_date: str | None = None

    def render(self, valid_at: str | None) -> str:
        """One human line for the fact's event time. ``valid_at`` is the edge's stored value."""
        source = f" (from '{self.expression}')" if self.expression else ""
        window = _window_text(self.window_start, self.window_end, self.granularity)
        if self.kind == "plan":
            if window:
                return f"planned {window}{source}"
            return f"planned, time unknown{_said(self.speech_date, self.expression)}"
        if window and self.outcome in _WINDOW_OUTCOMES:
            return f"{window}{source}"
        recorded = _utc_date_text(valid_at)
        if recorded and (self.outcome == "graphiti_resolved"
                         or (self.speech_date and recorded != self.speech_date)):
            return f"{recorded}{source}"  # a date Graphiti resolved itself, not the speech date
        return f"event time unknown{_said(self.speech_date, self.expression)}"


def event_time_from_row(row: Mapping[str, Any]) -> EventTime | None:
    """The contract on a projected row, or ``None`` when the edge carries none."""
    basis = row.get("time_basis")
    if not basis:
        return None

    def text(name: str) -> str | None:
        value = row.get(name)
        return str(value) if value is not None else None

    return EventTime(
        basis=str(basis),
        expression=text("time_expression"),
        kind=text("time_kind"),
        granularity=text("time_granularity"),
        window_start=text("time_window_start"),
        window_end=text("time_window_end"),
        outcome=text("time_outcome"),
        speech_date=text("time_speech_date"),
    )


def rendered_event_time(row: Mapping[str, Any]) -> str | None:
    """The rendered event time of a projected row (``valid_at`` as Neo4j toString), or ``None``."""
    contract = event_time_from_row(row)
    if contract is None:
        return None
    valid_at = row.get("valid_at")
    return contract.render(str(valid_at) if valid_at is not None else None)


def _said(speech_date: str | None, expression: str | None) -> str:
    parts = [f"said {speech_date}"] if speech_date else []
    if expression:
        parts.append(f"'{expression}'")
    return f" ({'; '.join(parts)})" if parts else ""


def _window_text(start: str | None, end: str | None, granularity: str | None) -> str | None:
    if not start or not end:
        return None
    if start == end:
        return start if granularity in (None, "day") else f"~{start}"
    if granularity == "month" and start[:7] == end[:7]:
        return f"~{start[:7]}"
    if granularity == "year" and start[:4] == end[:4]:
        return f"~{start[:4]}"
    return f"~{start}..{end}"


def _utc_date_text(value: str | None) -> str | None:
    """Calendar date (UTC) of a stored instant such as Neo4j's ``toString(datetime)``."""
    if not value:
        return None
    text = value.split("[", 1)[0].strip()  # drop a trailing zone id like [UTC]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10] if len(text) >= 10 else None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return date(parsed.year, parsed.month, parsed.day).isoformat()
