"""Session context for memory ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4


@dataclass(frozen=True)
class MemorySession:
    """Immutable session context carried through ingest operations."""

    session_id: str
    user_id: str
    started_at: datetime
    client_id: str = ""
    client_name: str = ""


def new_session(
    user_id: str,
    *,
    session_id: str | None = None,
    started_at: datetime | None = None,
    client_id: str = "",
    client_name: str = "",
) -> MemorySession:
    """Create a new session with production defaults and test-friendly overrides."""

    return MemorySession(
        session_id=session_id or str(uuid4()),
        user_id=user_id,
        started_at=started_at or datetime.now(timezone.utc),
        client_id=client_id,
        client_name=client_name,
    )
