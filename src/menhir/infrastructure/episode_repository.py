"""Episode repository facade over lifecycle, maintenance, and stamping helpers."""

from __future__ import annotations

from dataclasses import dataclass

from menhir.infrastructure.episode_lifecycle import (
    EpisodeLifecycleRepository,
    _PENDING_EPISODE_TOKEN_PATTERN,
    is_context_window_error_text,
    is_recoverable_context_window_error,
)
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository
from menhir.infrastructure.episode_stamping import EpisodeStampingRepository, PolicyStampResult
from menhir.infrastructure.neo4j import Neo4jRepository

__all__ = [
    "EpisodeRepository",
    "PolicyStampResult",
    "is_context_window_error_text",
    "is_recoverable_context_window_error",
    "_PENDING_EPISODE_TOKEN_PATTERN",
]


@dataclass
class EpisodeRepository(
    EpisodeLifecycleRepository,
    EpisodeMaintenanceRepository,
    EpisodeStampingRepository,
):
    """Compatibility facade for all episode-related graph operations."""

    neo4j: Neo4jRepository
