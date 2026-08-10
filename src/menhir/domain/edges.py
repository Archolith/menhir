"""Edge schema used by the memory graph."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EdgeType(str, Enum):
    BELONGS_TO = "belongs-to"
    LED_TO = "led-to"
    CAUSED_BY = "caused-by"
    RELATED_TO = "related-to"
    CONTRADICTS = "contradicts"
    PREFERRED_FOR = "prefers-for"


@dataclass
class Edge:
    """Relationship between two memory nodes."""

    source_id: str
    target_id: str
    type: str = EdgeType.RELATED_TO
    weight: float = 1.0
    created_at: datetime | None = None
    last_traversed: datetime | None = None
    source_label: str = "system-derived"
    scope: str = "SESSION"
