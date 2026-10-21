"""Thresholds and result types for the correlation service (facade split).

Moved verbatim from ``correlation_service.py`` so the facade module stays under
the file-size budget. ``correlation_service`` re-exports every public symbol
defined here, so existing import sites keep working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Below this threshold, no correlation action is taken.
CORRELATION_RELATED_THRESHOLD = 0.70

#: Above this threshold, pairs are flagged for LLM review (conflict path).
CORRELATION_CONFLICT_THRESHOLD = 0.85

#: Above this threshold, pairs are considered near-duplicates and merged.
CORRELATION_MERGE_THRESHOLD = 0.95


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CorrelationResult:
    """Outcome of a single correlation check."""

    action: str  # "none" | "related" | "conflict" | "merged"
    source_uuid: str
    target_uuid: str
    similarity: float
    details: dict[str, Any] | None = None


@dataclass
class CorrelationBatchResult:
    """Aggregate outcome of a batch correlation check."""

    checked: int = 0
    related: int = 0
    conflicts: int = 0
    merged: int = 0
    skipped: int = 0
    results: list[CorrelationResult] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []
