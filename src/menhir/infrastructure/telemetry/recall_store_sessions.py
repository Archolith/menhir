"""Client and session registry telemetry."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from menhir.infrastructure.telemetry.helpers import _utc_now_iso

logger = logging.getLogger(__name__)


class TelemetryClientSessionsMixin:
    def touch_session(
        self, session_id: str, client_id: str = "", client_name: str = ""
    ) -> None:
        """Upsert a session_registry row — sets last_accessed to now.

        Keyed by ``session_id`` so each conversation/window tracks independently.
        """
        if not session_id:
            return
        now = _utc_now_iso()
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_registry
                        (session_id, client_id, client_name, first_accessed, last_accessed)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        client_id     = excluded.client_id,
                        client_name   = excluded.client_name,
                        last_accessed = excluded.last_accessed
                    """,
                    (session_id, client_id, client_name, now, now),
                )
                conn.commit()
        except Exception:
            logger.warning(
                "Failed to touch session registry session_id=%s",
                session_id,
                exc_info=True,
            )

    def get_session_last_accessed(self, session_id: str) -> str | None:
        """Return the ISO timestamp of when this session was last seen, or None."""
        if not session_id:
            return None
        self._ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_accessed FROM session_registry WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row[0] if row else None

    def touch_client(self, client_id: str, client_name: str) -> None:
        """Upsert a client registry row — sets last_accessed to now."""

        if not client_id:
            return
        now = _utc_now_iso()
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO client_registry (client_id, client_name, first_accessed, last_accessed)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(client_id) DO UPDATE SET
                        client_name   = excluded.client_name,
                        last_accessed = excluded.last_accessed
                    """,
                    (client_id, client_name, now, now),
                )
                conn.commit()
        except Exception:
            logger.warning(
                "Failed to touch client registry client_id=%s", client_id, exc_info=True
            )

    def get_client_last_accessed(self, client_id: str) -> str | None:
        """Return the ISO timestamp of when this client was last seen, or None."""

        if not client_id:
            return None
        self._ensure_ready()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_accessed FROM client_registry WHERE client_id = ?",
                (client_id,),
            ).fetchone()
        return row[0] if row else None

    def list_clients(self) -> list[dict[str, Any]]:
        """Return all registered clients ordered by last_accessed desc."""

        self._ensure_ready()
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT client_id, client_name, first_accessed, last_accessed FROM client_registry ORDER BY last_accessed DESC"
            ).fetchall()
        return [dict(row) for row in rows]
