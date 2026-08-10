"""Typed results for memory ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IngestStatus(str, Enum):
    """Stable status values for the ingestion pipeline."""

    QUEUED = "queued"
    INGESTED = "ingested"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestResult:
    """Lean v1 ingestion result returned by the ingest service."""

    episode_id: str
    status: IngestStatus
    nodes_touched: int
    edges_touched: int
