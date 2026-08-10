"""Dataclass models for memory and lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class MemoryType(str, Enum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"
    PREFERENCE = "PREFERENCE"
    IDENTITY = "IDENTITY"
    TEMPORAL = "TEMPORAL"
    SPATIAL = "SPATIAL"


class NodeScope(str, Enum):
    CANDIDATE = "CANDIDATE"
    SESSION = "SESSION"
    PERSISTENT = "PERSISTENT"
    PROMOTED = "PROMOTED"


class FreshnessState(str, Enum):
    ACTIVE = "ACTIVE"
    COMPRESSED = "COMPRESSED"
    GONE = "GONE"


class ProcessingState(str, Enum):
    """Episode processing lifecycle state."""

    PENDING = "PENDING"
    ENRICHING = "ENRICHING"
    READY = "READY"
    FAILED = "FAILED"


class ConflictStatus(str, Enum):
    """Conflict resolution state attached to paired memory versions."""

    PENDING_LLM_REVIEW = "pending_llm_review"  # similarity flagged, awaiting LLM confirmation
    UNRESOLVED = "unresolved"                   # LLM confirmed genuine contradiction
    RESOLVED = "resolved"
    AUTO_RESOLVED = "auto-resolved"
    FALSE_POSITIVE = "false_positive"           # LLM cleared, not a real contradiction


@dataclass
class MemoryNode:
    """In-memory representation of a memory node."""

    id: str
    content: str
    type: MemoryType = MemoryType.SEMANTIC
    scope: NodeScope = NodeScope.SESSION
    freshness: FreshnessState = FreshnessState.ACTIVE
    source: str | None = None
    source_confidence: float | None = None
    user_flagged: bool = False
    bootstrap_scope: str | None = None
    created_at: datetime | None = None
    last_accessed: datetime | None = None
    sharpness: float = 0.0
    original_content: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    edge_count: int = 0
    conflict_group_id: str | None = None
    conflict_status: ConflictStatus | None = None
    conflict_created_at: datetime | None = None
    emotions: list[dict[str, Any]] = field(default_factory=list)
