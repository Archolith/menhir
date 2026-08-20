"""Generic, toggleable, replayable audit channels over the telemetry lifecycle_events store.

A single reusable primitive behind every domain audit trail (consolidation, recall, ...), so
"everything should be auditable" is one pattern, not N bespoke modules. Each :class:`AuditChannel`
owns a telemetry ``component`` name, an independent on/off toggle (default OFF), and an ambient
correlation id (contextvars) so nested emit sites join a unit of work without threading an id
through every function. Emission is best-effort and can NEVER raise into the caller, so toggling a
channel changes only WHAT is recorded, never what the code does.

Sink + reader are the telemetry ``lifecycle_events`` store already used by scheduler_trace and the
consolidation audit: rows are ``component=<channel>``, the correlation id rides in the indexed
``episode_uuid`` column, and richer keys ride in ``details``. Read back with :meth:`AuditChannel.read`
/ :meth:`AuditChannel.read_recent`, ``scripts/inspect_audit_trail.py``, or the telemetry reader.

The toggle is sourced from an explicit :meth:`AuditChannel.set_enabled` (bootstrap wires this from
settings) and, as a fallback for scripts/tests that never boot the full service, the channel's
environment variable read at construction.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import uuid
from typing import Any, Optional, Sequence

from menhir.config.settings_helpers import parse_bool_env
from menhir.infrastructure.telemetry import record_lifecycle_event, telemetry_store

logger = logging.getLogger(__name__)

class AuditChannel:
    """One toggleable audit trail for a domain (a telemetry ``component``)."""

    def __init__(self, component: str, env_var: str, corr_prefix: str = "aud") -> None:
        self.component = component
        self._env_var = env_var
        self._corr_prefix = corr_prefix
        self._lock = threading.Lock()
        # CF-19/CF-146: one truthy authority. This module carried its own table that also
        # accepted "on", which parse_bool_env deliberately rejects (SSOT-07) -- so the same
        # value enabled this channel while leaving every settings-driven flag off.
        self._enabled = parse_bool_env(os.getenv(env_var, ""))
        self._current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            f"audit_corr_{component}", default=None
        )

    # -- toggle -------------------------------------------------------------------------------
    def set_enabled(self, value: bool) -> None:
        """Turn this channel on or off (bootstrap wires this from settings)."""
        with self._lock:
            self._enabled = bool(value)

    def is_enabled(self) -> bool:
        return self._enabled

    # -- correlation --------------------------------------------------------------------------
    def new_id(self) -> str:
        return f"{self._corr_prefix}-{uuid.uuid4().hex[:16]}"

    def begin(self, corr_id: Optional[str] = None) -> str:
        """Mark the start of a unit of work and make its id the ambient correlation id.

        Returns the id (minted if not supplied). Cheap and safe even when the channel is off.
        """
        cid = corr_id or self.new_id()
        self._current.set(cid)
        return cid

    def current_id(self) -> Optional[str]:
        return self._current.get()

    # -- emit ---------------------------------------------------------------------------------
    def audit(
        self,
        event: str,
        state: str,
        *,
        corr_id: Optional[str] = None,
        namespace: Optional[str] = None,
        subject_uuid: Optional[str] = None,
        slot: Optional[Sequence[Any]] = None,
        episode_uuid: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record one decision. No-op (and cheap) when the toggle is OFF. Never raises."""
        if not self._enabled:
            return
        try:
            if corr_id is None:
                corr_id = self._current.get()
            payload: dict[str, Any] = {}
            if namespace is not None:
                payload["namespace"] = namespace
            if subject_uuid is not None:
                payload["subject_uuid"] = subject_uuid
            if slot is not None:
                payload["slot"] = list(slot) if isinstance(slot, (tuple, list)) else slot
            if episode_uuid is not None:
                payload["episode_uuid"] = episode_uuid
            if details:
                payload.update(details)
            record_lifecycle_event(
                component=self.component,
                event=str(event),
                state=str(state),
                episode_uuid=corr_id,
                details=payload or None,
            )
        except Exception as exc:  # pragma: no cover - audit must never break the caller
            # CF-110: warning, not debug. The shipped level is INFO, so a debug record here
            # emitted at no sink at all -- an audit trail whose write failures are invisible
            # cannot be used as evidence. The caller is still never broken.
            logger.warning(
                "%s audit emit failed (%s.%s): %s", self.component, event, state, exc
            )

    # -- read ---------------------------------------------------------------------------------
    def read(self, corr_id: str, *, limit: int = 200, store: Any = telemetry_store) -> list[dict[str, Any]]:
        """Every event for one unit of work, oldest-first (replay order)."""
        rows = store.fetch_recent_lifecycle_events(
            limit=max(1, min(limit, 200)), component=self.component, episode_uuid=corr_id
        )
        return list(reversed(rows))

    def read_recent(self, *, limit: int = 200, store: Any = telemetry_store) -> list[dict[str, Any]]:
        """Recent events across all units of work, oldest-first."""
        rows = store.fetch_recent_lifecycle_events(limit=max(1, min(limit, 200)), component=self.component)
        return list(reversed(rows))


# -- channel registry -------------------------------------------------------------------------
# One singleton per auditable domain. Add channels here as more of the system becomes auditable.

#: Recall-side decisions (Step 7 View-authority suppression: intent, candidates, provenance rows,
#: per-gate advisory reasons, final suppressed set). Toggle: MENHIR_PERSONAL_MEMORY_RECALL_AUDIT_ENABLED.
RECALL = AuditChannel("recall_audit", "MENHIR_PERSONAL_MEMORY_RECALL_AUDIT_ENABLED", corr_prefix="rq")

